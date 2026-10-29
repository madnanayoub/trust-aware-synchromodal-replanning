"""STP replanning MIP of Qu et al. (2019) in docplex, plus the N-STP and R-STP reference variants.

Formulation notes (mapping to the paper; see reports/step3_model.md):
  x[a,s]          (1)-(4)   integer TEU of shipment s on LCS leg a; z[v] service-used binaries
  truck copies              x[l,s,c]: TEU of s on truck lane l (copy c), 1 TEU per truck, own departure
  stay[v,k,s]               volume that stays aboard service v at its k-th stop (no transshipment)
  n[u,w,s], e[u,w]  (5)-(8) explicit transshipment volume / binary from unloading event u to loading
                            event w at a terminal; cost g(mode_u, mode_w) per TEU (21); timing (13)
  y[a]            (9)-(12)  departure after loading, on a grid of `dt` hours
                            barge: pi <= y <= pi + phi;  rail: y = pi + k*step, k in 0..kmax;
                            truck: free;  no timetable (4TU instances): free within horizon
  delivery        (14)-(17) t = y + run + unload for every event delivering s at d^s;  t <= gamma
  early/late      (24)-(27) slot linearisation with slot width `dt`; exact because all times are on
                            the same grid, so MIP objective == evaluator cost
Variants:
  N-STP  x[a,s] in {0, n^s}  (shipment travels as one unit; Appendix B)
  R-STP  LCS departures fixed to the timetable (y = pi); trucks keep flexible departure (Sec. 7.2)
"""
from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from docplex.mp.model import Model

from .instance import Instance
from .plan import Plan, TruckMove


@dataclass
class Options:
    variant: str = "STP"            # STP | N-STP | R-STP
    dt: float = 0.5                 # time grid / penalty slot width (h)
    truck_copies: int = 1           # independent departure groups per (lane, shipment)
    time_limit: float = 300.0
    mip_gap: float = 0.0
    backend: str = "auto"           # auto | cplex | highs
    threads: int = field(default_factory=lambda: int(os.environ.get("STP_THREADS", "0")))  # 0 = solver default
    # replanning: legs whose decisions are frozen {(sid, k): {"dep": t, "flows": {s: q}}}
    frozen_lcs: Dict = field(default_factory=dict)
    frozen_trucks: List[TruckMove] = field(default_factory=list)   # trucks already on the road (fixed)
    # truck commitments: trucks booked earlier are prepaid (sunk); trucks beyond the booking are spot-hired
    booked_trucks: Dict[str, int] = field(default_factory=dict)     # lane -> prepaid trucks
    spot_premium: float = 0.0        # extra fraction on (fixed + variable) cost of spot trucks
    unavailable_lcs: List[str] = field(default_factory=list)   # broken-down services
    allow_unserved: bool = False     # soft demand: keep the model feasible, penalise TEU left behind
    unserved_penalty: float = 1000.0  # EUR/TEU (must match evaluate.UNSERVED_PENALTY for comparisons)
    # tie-breaking towards an existing plan (does not change the optimum by more than eps * total deviation)
    anchor_plan: Optional[Plan] = None
    anchor_eps: float = 1e-3          # EUR per TEU of flow deviation and per hour of departure shift
    verbose: bool = False


@dataclass
class Result:
    plan: Optional[Plan]
    status: str
    objective: Optional[float]
    bound: Optional[float]
    gap: Optional[float]
    seconds: float
    backend: str
    size: dict
    breakdown: dict = field(default_factory=dict)


class STPModel:
    def __init__(self, inst: Instance, opt: Options = Options()):
        self.inst, self.opt = inst, opt
        self.m = Model(name=f"{opt.variant}_{inst.name}".replace("+", "p").replace(",", "_"))
        self._build()

    # ------------------------------------------------------------------ helpers
    def _grid(self, lo, hi):
        dt = self.opt.dt
        return math.ceil(lo / dt - 1e-9), math.floor(hi / dt + 1e-9)

    def _dep_range(self, ev):
        return self.dep_lo[ev], self.dep_hi[ev]

    # ------------------------------------------------------------------ build
    def _build(self):
        inst, opt, m = self.inst, self.opt, self.m
        dt, T0, T1 = opt.dt, inst.horizon_start, inst.horizon_end
        BIGT = T1 + 24.0
        S = inst.shipments
        self.svc = {v.id: v for v in inst.services}
        self.lane = {l.id: l for l in inst.trucks}

        # ---------- events: LCS legs and truck copies
        self.legs = [(v.id, k) for v in inst.services for k in range(len(v.legs))]
        self.ev_i, self.ev_j, self.ev_mode, self.ev_run = {}, {}, {}, {}
        self.ev_load, self.ev_unload = {}, {}
        self.dep_lo, self.dep_hi = {}, {}
        self.y, self.ky = {}, {}
        for (vid, k) in self.legs:
            v, leg = self.svc[vid], self.svc[vid].legs[k]
            ev = ("L", vid, k)
            self.ev_i[ev], self.ev_j[ev], self.ev_mode[ev], self.ev_run[ev] = leg.i, leg.j, v.mode, leg.run_time
            self.ev_load[ev], self.ev_unload[ev] = v.load_time, v.unload_time
            pi = leg.planned_dep
            if inst.has_timetable and pi is not None:
                if opt.variant == "R-STP":
                    lo = hi = pi
                elif v.mode == "rail":
                    lo, hi = pi, pi + inst.rail_max_shift
                else:  # barge (and any other LCS)
                    lo, hi = pi, pi + inst.barge_buffer
            else:
                lo, hi = T0 + v.load_time, T1
            if (vid, k) in opt.frozen_lcs:
                lo = hi = opt.frozen_lcs[(vid, k)]["dep"]
            self.dep_lo[ev], self.dep_hi[ev] = lo, hi
            name = f"y_{vid}_{k}"
            if lo == hi:
                self.y[ev] = lo
            elif inst.has_timetable and pi is not None and v.mode == "rail" and opt.variant != "R-STP":
                kk = m.integer_var(0, int(round(inst.rail_max_shift / inst.rail_step)), name=f"k_{vid}_{k}")
                self.ky[ev] = kk
                self.y[ev] = pi + inst.rail_step * kk
            else:
                g0, g1 = self._grid(lo, hi)
                kk = m.integer_var(g0, g1, name=f"g_{vid}_{k}")
                self.ky[ev] = kk
                self.y[ev] = dt * kk

        self.tcopies = []
        for l in inst.trucks:
            for s in S:
                for c in range(opt.truck_copies):
                    ev = ("T", l.id, s.id, c)
                    self.tcopies.append(ev)
                    self.ev_i[ev], self.ev_j[ev], self.ev_mode[ev], self.ev_run[ev] = l.i, l.j, "truck", l.run_time
                    self.ev_load[ev], self.ev_unload[ev] = l.load_time, l.unload_time
                    lo = max(T0 + l.load_time, s.alpha + l.load_time) if l.i == s.o else T0 + l.load_time
                    hi = s.gamma - l.unload_time - l.run_time
                    self.dep_lo[ev], self.dep_hi[ev] = lo, hi
                    if lo > hi + 1e-9:
                        self.y[ev] = None
                        continue
                    g0, g1 = self._grid(lo, hi)
                    kk = m.integer_var(g0, g1, name=f"g_{l.id}_{s.id}_{c}")
                    self.ky[ev] = kk
                    self.y[ev] = dt * kk

        self.frozen_tcopies = {}
        for idx, tm in enumerate(opt.frozen_trucks):
            l = self.lane[tm.lane]
            ev = ("T", l.id, tm.shipment, f"f{idx}")
            self.tcopies.append(ev)
            self.ev_i[ev], self.ev_j[ev], self.ev_mode[ev], self.ev_run[ev] = l.i, l.j, "truck", l.run_time
            self.ev_load[ev], self.ev_unload[ev] = l.load_time, l.unload_time
            self.dep_lo[ev] = self.dep_hi[ev] = tm.dep
            self.y[ev] = tm.dep
            self.frozen_tcopies[ev] = tm.teu

        def arr(ev):
            return self.y[ev] + self.ev_run[ev]

        # ---------- flow variables
        self.x = {}
        for s in S:
            for ev in [("L",) + l for l in self.legs] + [t for t in self.tcopies if t[2] == s.id]:
                i, j = self.ev_i[ev], self.ev_j[ev]
                ub = s.n if ev[0] == "T" else min(s.n, self.svc[ev[1]].capacity)
                dead = (j == s.o or i == s.d or self.y[ev] is None
                        or (ev[0] == "L" and ev[1] in opt.unavailable_lcs)
                        or self.dep_lo[ev] + self.ev_run[ev] > s.gamma)    # cannot be useful
                if dead:
                    continue
                nm = f"x_{'_'.join(map(str, ev[1:3] if ev[0]=='L' else ev[1:]))}_{s.id}" if ev[0] == "L" \
                    else f"x_{ev[1]}_{ev[3]}_{s.id}"
                if opt.variant == "N-STP":
                    chi = m.binary_var(name="chi" + nm[1:])
                    self.x[(ev, s.id)] = s.n * chi
                else:
                    self.x[(ev, s.id)] = m.integer_var(0, ub, name=nm)
        X = lambda ev, sid: self.x.get((ev, sid), 0)

        # frozen LCS flows
        for (vid, k), fr in opt.frozen_lcs.items():
            for s in S:
                q = fr["flows"].get(s.id, 0.0)
                key = (("L", vid, k), s.id)
                if key in self.x:
                    m.add_constraint(self.x[key] == q, ctname=f"frz_{vid}_{k}_{s.id}")
                elif q > 0:
                    raise ValueError(f"frozen flow {vid},{k},{s.id} not representable")

        for ev, q in self.frozen_tcopies.items():
            key = (ev, ev[2])
            if key not in self.x:
                raise ValueError(f"frozen truck {ev} cannot carry {ev[2]}")
            m.add_constraint(self.x[key] == q, ctname=f"frzT_{ev[1]}_{ev[3]}")

        # ---------- service usage, capacity (2)(4)
        self.z = {}
        for v in inst.services:
            if v.id in opt.unavailable_lcs:
                self.z[v.id] = 0
                continue
            self.z[v.id] = m.binary_var(name=f"z_{v.id}")
            for k in range(len(v.legs)):
                ev = ("L", v.id, k)
                load = m.sum(X(ev, s.id) for s in S)
                m.add_constraint(load <= v.capacity * self.z[v.id], ctname=f"cap_{v.id}_{k}")
            # itinerary dwell (12)
            for k in range(1, len(v.legs)):
                a, b = ("L", v.id, k - 1), ("L", v.id, k)
                m.add_constraint(self.y[b] >= arr(a), ctname=f"dwell_{v.id}_{k}")
        for l in inst.trucks:
            m.add_constraint(m.sum(X(t, t[2]) for t in self.tcopies if t[1] == l.id) <= l.max_trucks,
                             ctname=f"fleet_{l.id}")

        # ---------- stays, origin loads, deliveries, transfers
        self.stay, self.orig, self.deliv, self.n, self.e = {}, {}, {}, {}, {}
        unl = {}  # node -> events arriving
        lod = {}  # node -> events departing
        for ev in [("L",) + l for l in self.legs] + self.tcopies:
            if self.y[ev] is None:
                continue
            unl.setdefault(self.ev_j[ev], []).append(ev)
            lod.setdefault(self.ev_i[ev], []).append(ev)

        for s in S:
            for (vid, k) in self.legs:
                if k == 0:
                    continue
                a, b = ("L", vid, k - 1), ("L", vid, k)
                node = self.ev_i[b]
                if (a, s.id) in self.x and (b, s.id) in self.x and node not in (s.o, s.d):
                    self.stay[(vid, k, s.id)] = m.continuous_var(0, s.n, name=f"st_{vid}_{k}_{s.id}")

        def compatible(u, w):
            if u[0] == "L" and w[0] == "L" and u[1] == w[1]:
                return False           # same vehicle -> 'stay', not transshipment
            g, r = inst.trans(self.ev_mode[u], self.ev_mode[w])
            earliest = self.dep_lo[u] + self.ev_run[u] + self.ev_unload[u] + r + self.ev_load[w]
            return earliest <= self.dep_hi[w] + 1e-9

        for s in S:
            for node in inst.terminals:
                if node in (s.o, s.d):
                    continue
                for u in unl.get(node, []):
                    if (u, s.id) not in self.x:
                        continue
                    for w in lod.get(node, []):
                        if (w, s.id) not in self.x or not compatible(u, w):
                            continue
                        ekey = (u, w) if (u[0] == "L" and w[0] == "L") else (u, w, s.id)
                        if ekey not in self.e:
                            nm = "e_" + "_".join(str(p) for q in ekey[:2] for p in q[1:]) + \
                                 ("" if len(ekey) == 2 else "_" + s.id)
                            self.e[ekey] = m.binary_var(name=nm)
                            g, r = inst.trans(self.ev_mode[u], self.ev_mode[w])
                            m.add_constraint(self.y[w] - arr(u) >= self.ev_unload[u] + r + self.ev_load[w]
                                             - BIGT * (1 - self.e[ekey]), ctname="tt" + nm[1:])   # (13)
                        nv = m.continuous_var(0, s.n, name=f"n{len(self.n)}")
                        self.n[(u, w, s.id)] = nv
                        m.add_constraint(nv <= s.n * self.e[ekey])                                  # (8)

        self.b_or, self.b_dl = {}, {}
        self.unserved = {}
        self.strand = {}
        n_in, n_out = {}, {}
        for (u, w, sid), nv in self.n.items():
            n_in.setdefault((w, sid), []).append(nv)
            n_out.setdefault((u, sid), []).append(nv)
        for s in S:
            # conservation per event (1)(5)(6)
            for ev in [("L",) + l for l in self.legs] + [t for t in self.tcopies if t[2] == s.id]:
                if (ev, s.id) not in self.x:
                    continue
                xv = self.x[(ev, s.id)]
                i, j = self.ev_i[ev], self.ev_j[ev]
                # --- departure side at i
                inflow = []
                if ev[0] == "L" and ev[2] > 0 and (ev[1], ev[2], s.id) in self.stay:
                    inflow.append(self.stay[(ev[1], ev[2], s.id)])
                if i == s.o:
                    o = m.continuous_var(0, s.n, name=f"or_{len(self.orig)}")
                    self.orig[(ev, s.id)] = o
                    inflow.append(o)
                    bo = m.binary_var(name=f"bo_{len(self.b_or)}")
                    self.b_or[(ev, s.id)] = bo
                    m.add_constraint(o <= s.n * bo)
                    m.add_constraint(self.y[ev] >= s.alpha + self.ev_load[ev] - BIGT * (1 - bo))      # (10)
                inflow += n_in.get((ev, s.id), [])
                m.add_constraint(xv == m.sum(inflow))
                # --- arrival side at j
                outflow = []
                if ev[0] == "L":
                    nxt = (ev[1], ev[2] + 1, s.id)
                    if nxt in self.stay:
                        outflow.append(self.stay[nxt])
                if j == s.d:
                    dv = m.continuous_var(0, s.n, name=f"dl_{len(self.deliv)}")
                    self.deliv[(ev, s.id)] = dv
                    outflow.append(dv)
                outflow += n_out.get((ev, s.id), [])
                if opt.allow_unserved and j != s.d:
                    # cargo stranded at an intermediate terminal (no feasible onward connection)
                    st = m.continuous_var(0, s.n, name=f"strand_{len(self.strand)}")
                    self.strand[(ev, s.id)] = st
                    outflow.append(st)
                m.add_constraint(xv == m.sum(outflow))
            un = 0
            if opt.allow_unserved:
                un = m.integer_var(0, s.n, name=f"un_{s.id}")
                self.unserved[s.id] = un
            m.add_constraint(m.sum(o for (ev, sid), o in self.orig.items() if sid == s.id) + un == s.n,
                             ctname=f"src_{s.id}")
            m.add_constraint(m.sum(d for (ev, sid), d in self.deliv.items() if sid == s.id) + un
                             + m.sum(v for (ev, sid), v in self.strand.items() if sid == s.id) == s.n,
                             ctname=f"snk_{s.id}")

        # ---------- delivery windows and penalties (14)-(17), (24)-(27)
        early_terms, late_terms = [], []
        self.slots = 0
        for (ev, sid), dv in self.deliv.items():
            s = inst.shipment(sid)
            bd = m.binary_var(name=f"bd_{len(self.b_dl)}")
            self.b_dl[(ev, sid)] = bd
            m.add_constraint(dv <= s.n * bd)
            t = arr(ev) + self.ev_unload[ev]
            m.add_constraint(t <= s.gamma + BIGT * (1 - bd))                                          # (15)
            t_lo = self.dep_lo[ev] + self.ev_run[ev] + self.ev_unload[ev]
            t_hi = min(s.gamma, self.dep_hi[ev] + self.ev_run[ev] + self.ev_unload[ev])
            nE = max(0, math.ceil((s.beta - t_lo) / dt - 1e-9))
            nL = max(0, math.ceil((t_hi - s.beta) / dt - 1e-9))
            for kind, nslot, rho, sign in (("E", nE, s.rho_early, -1), ("L", nL, s.rho_late, 1)):
                if nslot == 0:
                    continue
                lam = [m.binary_var(name=f"lm{kind}_{len(self.b_dl)}_{q}") for q in range(nslot)]
                qv = [m.continuous_var(0, s.n, name=f"q{kind}_{len(self.b_dl)}_{q}") for q in range(nslot)]
                self.slots += nslot
                for q in range(1, nslot):
                    m.add_constraint(lam[q] <= lam[q - 1])
                dev = (s.beta - t) if sign < 0 else (t - s.beta)
                m.add_constraint(dev <= dt * m.sum(lam) + BIGT * (1 - bd))
                for q in range(nslot):
                    m.add_constraint(qv[q] >= dv - s.n * (1 - lam[q]))
                (early_terms if kind == "E" else late_terms).append(rho * dt * m.sum(qv))

        # ---------- objective (18)-(23)
        Cf = m.sum(v.fixed_cost * self.z[v.id] + (v.cancel_cost if v.preplanned else 0) * (1 - self.z[v.id])
                   for v in inst.services if v.id not in opt.unavailable_lcs) \
            + sum(v.cancel_cost for v in inst.services if v.id in opt.unavailable_lcs and v.preplanned) \
            + self._truck_fixed(X)
        Cc = m.sum(self.svc[ev[1]].legs[ev[2]].var_cost * xv if ev[0] == "L"
                   else self.lane[ev[1]].var_cost * xv for (ev, sid), xv in self.x.items())
        Ct = m.sum(inst.trans(self.ev_mode[u], self.ev_mode[w])[0] * nv for (u, w, sid), nv in self.n.items())
        Ce, Cl = m.sum(early_terms), m.sum(late_terms)
        Cu = opt.unserved_penalty * (m.sum(self.unserved.values()) + m.sum(self.strand.values()))
        self.kpi = {"fixed": Cf, "variable": Cc, "transshipment": Ct, "early": Ce, "late": Cl, "unserved": Cu}
        Ca = self._anchor_terms() if opt.anchor_plan is not None else 0
        m.minimize(Cf + Cc + Ct + Ce + Cl + Cu + Ca)

    def _truck_fixed(self, X):
        """Truck fixed cost. Without commitments: f * trucks. With bookings B_l and spot premium p:
        f*B_l (sunk) + f*(1+p)*spot_l + p*c_l*spot_l, spot_l >= used_l - B_l (trucks hired last-minute)."""
        m, opt = self.m, self.opt
        if not opt.booked_trucks and opt.spot_premium == 0:
            return m.sum(self.lane[t[1]].fixed_cost * X(t, t[2]) for t in self.tcopies)
        terms = []
        self.spot = {}
        for l in self.inst.trucks:
            used = m.sum(X(t, t[2]) for t in self.tcopies if t[1] == l.id)
            B = opt.booked_trucks.get(l.id, 0)
            sp = m.integer_var(0, l.max_trucks, name=f"spot_{l.id}")
            self.spot[l.id] = sp
            m.add_constraint(sp >= used - B)
            terms.append(l.fixed_cost * B + (l.fixed_cost * (1 + opt.spot_premium)
                                             + opt.spot_premium * l.var_cost) * sp)
        return m.sum(terms)

    def _anchor_terms(self):
        """eps * (|x - x_anchor| summed over arcs + |y - y_anchor| over LCS legs): picks, among
        cost-equivalent optima, the plan closest to the anchor (the plan currently in execution)."""
        m, ap, eps = self.m, self.opt.anchor_plan, self.opt.anchor_eps
        terms = []
        for (ev, sid), xv in self.x.items():
            if ev[0] != "L":
                continue
            xb = ap.lcs_flow.get((ev[1], ev[2]), {}).get(sid, 0.0)
            d = m.continuous_var(0, name=f"anc_{ev[1]}_{ev[2]}_{sid}")
            m.add_constraint(d >= xv - xb)
            m.add_constraint(d >= xb - xv)
            terms.append(d)
        tb = {}
        for t in ap.trucks:
            tb[(t.lane, t.shipment)] = tb.get((t.lane, t.shipment), 0.0) + t.teu
        for l in self.inst.trucks:
            for s in self.inst.shipments:
                xs = [xv for (ev, sid), xv in self.x.items() if ev[0] == "T" and ev[1] == l.id and sid == s.id]
                xb = tb.get((l.id, s.id), 0.0)
                if not xs and xb == 0:
                    continue
                d = m.continuous_var(0, name=f"anc_{l.id}_{s.id}")
                m.add_constraint(d >= m.sum(xs) - xb)
                m.add_constraint(d >= xb - m.sum(xs))
                terms.append(d)
        for (sid, k), yb in ap.lcs_dep.items():
            ev = ("L", sid, k)
            if ev in self.y and not isinstance(self.y[ev], (int, float)):
                d = m.continuous_var(0, name=f"ancy_{sid}_{k}")
                m.add_constraint(d >= self.y[ev] - yb)
                m.add_constraint(d >= yb - self.y[ev])
                terms.append(d)
        return eps * m.sum(terms)

    # ------------------------------------------------------------------ solve
    def size(self):
        st = self.m.statistics
        return {"vars": st.number_of_variables, "binary": st.number_of_binary_variables,
                "integer": st.number_of_integer_variables, "continuous": st.number_of_continuous_variables,
                "constraints": st.number_of_constraints}

    def solve(self) -> Result:
        backend = self.opt.backend
        if backend in ("auto", "cplex"):
            try:
                return self._solve_cplex()
            except Exception as ex:          # missing runtime / community-edition size limit
                if backend == "cplex":
                    raise
                if self.opt.verbose:
                    print(f"[CPLEX unavailable: {str(ex)[:120]}] -> HiGHS")
        return self._solve_highs()

    def _solve_cplex(self) -> Result:
        m = self.m
        m.parameters.timelimit = self.opt.time_limit
        m.parameters.mip.tolerances.mipgap = self.opt.mip_gap
        if self.opt.threads:
            m.parameters.threads = self.opt.threads
        t0 = time.time()
        sol = m.solve(log_output=self.opt.verbose)
        secs = time.time() - t0
        details = m.solve_details
        if sol is None:
            if "limit" in str(details.status).lower() and "size" in str(details.status).lower():
                raise RuntimeError(details.status)
            return Result(None, str(details.status), None, None, None, secs, "cplex", self.size())
        return self._result(lambda e: float(e) if isinstance(e, (int, float)) else e.solution_value,
                            str(details.status), sol.objective_value, details.best_bound, details.mip_relative_gap,
                            secs, "cplex")

    def _solve_highs(self) -> Result:
        import highspy
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "model.lp")
            self.m.export_as_lp(path)
            h = highspy.Highs()
            h.setOptionValue("output_flag", self.opt.verbose)
            h.setOptionValue("time_limit", float(self.opt.time_limit))
            h.setOptionValue("mip_rel_gap", max(self.opt.mip_gap, 1e-9))
            if self.opt.threads:
                h.setOptionValue("threads", self.opt.threads)
            h.readModel(path)
            t0 = time.time()
            h.run()
            secs = time.time() - t0
            status = h.modelStatusToString(h.getModelStatus())
            info = h.getInfo()
            if info.primal_solution_status != 2:
                return Result(None, status, None, None, None, secs, "highs", self.size())
            lp = h.getLp()
            vals = dict(zip(lp.col_names_, h.getSolution().col_value))
        values = {v.name: vals.get(v.name, 0.0) for v in self.m.iter_variables()}

        from docplex.mp.dvar import Var

        def ev(e):
            if isinstance(e, (int, float)):
                return float(e)
            if isinstance(e, Var):
                return values.get(e.name, 0.0)
            if hasattr(e, "iter_terms"):
                tot = float(getattr(e, "constant", 0.0))
                for var, coef in e.iter_terms():
                    tot += coef * values.get(var.name, 0.0)
                return tot
            return values.get(e.name, 0.0)
        obj = ev(self.m.objective_expr)
        return self._result(ev, status, obj, info.mip_dual_bound, info.mip_gap, secs, "highs")

    def _result(self, val, status, obj, bound, gap, secs, backend) -> Result:
        v = val
        plan = Plan(f"{self.opt.variant} on {self.inst.name}", meta={"variant": self.opt.variant})
        for (ev, sid), xv in self.x.items():
            q = float(round(v(xv)))            # x is integer TEU: remove solver tolerance (e.g. 29.999997)
            if q < 1e-6:
                continue
            if ev[0] == "L":
                plan.add_lcs(ev[1], ev[2], sid, q)
            else:
                plan.trucks.append(TruckMove(lane=ev[1], shipment=sid, teu=q, dep=round(v(self.y[ev]), 6)))
        for ev in [("L",) + l for l in self.legs]:
            if plan.leg_load((ev[1], ev[2])) > 1e-6 or any(plan.leg_load((ev[1], k)) > 1e-6
                                                          for k in range(len(self.svc[ev[1]].legs))):
                plan.lcs_dep[(ev[1], ev[2])] = round(v(self.y[ev]), 6)
        for sid, u in self.unserved.items():
            q = round(v(u), 6)
            if q > 1e-6:
                plan.unserved[sid] = q
        for (ev, sid), st in self.strand.items():
            q = round(v(st), 6)
            if q > 1e-6:
                key = f"{sid}@{self.ev_j[ev]}"
                plan.stranded[key] = plan.stranded.get(key, 0.0) + q
        brk = {k: round(v(e), 4) for k, e in self.kpi.items()}
        plan.meta.update({"objective": obj, "breakdown": brk, "status": status, "backend": backend})
        return Result(plan, status, obj, bound, gap, secs, backend, self.size(), brk)


def solve(inst: Instance, **kw) -> Result:
    return STPModel(inst, Options(**kw)).solve()
