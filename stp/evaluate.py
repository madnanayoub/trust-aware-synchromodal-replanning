"""Independent plan evaluator implementing the STP cost function (eqs. 18-23) and
feasibility checks for constraints (1)-(17) of Qu et al. (2019).

It is solver-independent on purpose: every method (STP, N-STP, R-STP, ours) is scored by
the same code, so cost comparisons cannot be biased by differing model formulations.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
from scipy.optimize import linprog

from .instance import Instance
from .plan import Plan

EPS = 1e-6


@dataclass
class Evaluation:
    fixed: float = 0.0          # C^f (incl. cancellation fees)
    variable: float = 0.0       # C^c
    transshipment: float = 0.0  # C^t
    early: float = 0.0          # C^e
    late: float = 0.0           # C^l
    unserved: float = 0.0       # penalty for demand not transported (soft-demand runs only)
    unserved_teu: float = 0.0
    trucks_used: int = 0
    lcs_used: List[str] = field(default_factory=list)
    lcs_cancelled: List[str] = field(default_factory=list)
    transshipped_teu: float = 0.0
    early_teu_h: float = 0.0
    late_teu_h: float = 0.0
    deliveries: List[dict] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.fixed + self.variable + self.transshipment + self.early + self.late + self.unserved

    @property
    def feasible(self) -> bool:
        return not self.violations

    def row(self) -> dict:
        return {"total": round(float(self.total), 2), "fixed": round(float(self.fixed), 2),
                "variable": round(float(self.variable), 2), "transshipment": round(float(self.transshipment), 2),
                "early": round(float(self.early), 2), "late": round(float(self.late), 2),
                "unserved_TEU": round(float(self.unserved_teu), 2),
                "trucks": self.trucks_used, "transshipped_TEU": round(float(self.transshipped_teu), 2),
                "feasible": self.feasible}


def _leg_times(inst: Instance, plan: Plan, key) -> Tuple[float, float]:
    svc = inst.service(key[0])
    dep = plan.lcs_dep.get(key)
    if dep is None:
        return None, None
    arr = plan.lcs_arr_override.get(key, dep + svc.legs[key[1]].run_time)
    return dep, arr


UNSERVED_PENALTY = 1000.0   # EUR/TEU, only used when a plan declares unserved demand


def evaluate(inst: Instance, plan: Plan, check_timetable: bool = True,
             unserved_penalty: float = UNSERVED_PENALTY) -> Evaluation:
    ev = Evaluation()
    V = ev.violations
    ships = {s.id: s for s in inst.shipments}

    # ------------------------------------------------------------ fixed + variable (19)(20)
    for svc in inst.services:
        keys = [(svc.id, k) for k in range(len(svc.legs))]
        load = [plan.leg_load(k) for k in keys]
        used = any(l > EPS for l in load)
        if used:
            ev.fixed += svc.fixed_cost
            ev.lcs_used.append(svc.id)
        elif svc.preplanned:
            ev.fixed += svc.cancel_cost
            ev.lcs_cancelled.append(svc.id)
        for k, key in enumerate(keys):
            ev.variable += load[k] * svc.legs[k].var_cost
            if load[k] > svc.capacity + EPS:                           # (2)
                V.append(f"capacity {svc.id} leg{k}: {load[k]:g} > {svc.capacity:g}")
            if used and plan.lcs_dep.get(key) is None and load[k] > EPS:
                V.append(f"missing departure time for {svc.id} leg{k}")
        # dwell / itinerary consistency (12)
        if used:
            for k in range(1, len(svc.legs)):
                d0, a0 = _leg_times(inst, plan, keys[k - 1])
                d1, _ = _leg_times(inst, plan, keys[k])
                if a0 is not None and d1 is not None and d1 + EPS < a0:
                    V.append(f"{svc.id}: leg{k} departs {d1:g} before leg{k-1} arrives {a0:g}")
        # rescheduling flexibility (9a)(9b)
        if check_timetable and inst.has_timetable and used:
            for k, key in enumerate(keys):
                pi, dep = svc.legs[k].planned_dep, plan.lcs_dep.get(key)
                if pi is None or dep is None or (k > 0 and load[k] < EPS):
                    continue
                shift = dep - pi
                if svc.mode == "barge" and shift > inst.barge_buffer + EPS:
                    V.append(f"{svc.id} leg{k} shifted {shift:g}h > barge buffer {inst.barge_buffer:g}h")
                if svc.mode == "rail":
                    steps = shift / inst.rail_step
                    if shift < -EPS or shift > inst.rail_max_shift + EPS or abs(steps - round(steps)) > 1e-4:
                        V.append(f"{svc.id} leg{k} rail shift {shift:g}h not in "
                                 f"{{0,{inst.rail_step:g},..,{inst.rail_max_shift:g}}}")

    lanes = {t.id: t for t in inst.trucks}
    lane_count = defaultdict(int)
    for m in plan.trucks:
        lane = lanes[m.lane]
        n_tr = int(np.ceil(m.teu / lane.capacity_per_truck - 1e-9))
        lane_count[m.lane] += n_tr
        ev.trucks_used += n_tr
        ev.fixed += n_tr * lane.fixed_cost
        ev.variable += m.teu * lane.var_cost
    for lid, n in lane_count.items():
        if n > lanes[lid].max_trucks:
            V.append(f"truck lane {lid}: {n} trucks > fleet {lanes[lid].max_trucks}")

    # ------------------------------------------------------------ per-shipment events
    # unloaders: (node, mode, teu, ready_time_at_terminal, vehicle_label)
    # loaders:   (node, mode, teu, dep_time_after_loading, load_time, vehicle_label)
    for s in inst.shipments:
        unl, lod = [], []
        net = defaultdict(float)
        for svc in inst.services:
            n_leg = len(svc.legs)
            xs = [plan.lcs_flow.get((svc.id, k), {}).get(s.id, 0.0) for k in range(n_leg)]
            if max(xs) < EPS:
                continue
            for k in range(n_leg + 1):           # stops of the itinerary
                node = svc.legs[k].i if k < n_leg else svc.legs[-1].j
                x_in = xs[k - 1] if k > 0 else 0.0
                x_out = xs[k] if k < n_leg else 0.0
                if x_in - x_out > EPS:            # Delta^- (5)
                    _, arr = _leg_times(inst, plan, (svc.id, k - 1))
                    unl.append([node, svc.mode, x_in - x_out, (arr or 0) + svc.unload_time, svc.id])
                if x_out - x_in > EPS:            # Delta^+ (6)
                    dep, _ = _leg_times(inst, plan, (svc.id, k))
                    lod.append([node, svc.mode, x_out - x_in, dep or 0, svc.load_time, svc.id])
                net[node] += x_in - x_out
        for m in plan.trucks:
            if m.shipment != s.id:
                continue
            lane = lanes[m.lane]
            arr = m.dep + lane.run_time
            unl.append([lane.j, "truck", m.teu, arr + lane.unload_time, m.lane])
            lod.append([lane.i, "truck", m.teu, m.dep, lane.load_time, m.lane])
            net[lane.j] += m.teu
            net[lane.i] -= m.teu

        # flow conservation (1)  (minus any declared unserved volume)
        n_eff = s.n - plan.unserved.get(s.id, 0.0)
        strand = {k.split("@")[1]: q for k, q in plan.stranded.items() if k.split("@")[0] == s.id}
        lost = plan.unserved.get(s.id, 0.0) + sum(strand.values())
        ev.unserved_teu += lost
        ev.unserved += lost * unserved_penalty
        for node in set(net) | {s.o, s.d} | set(strand):
            req = -n_eff if node == s.o else (n_eff - sum(strand.values()) if node == s.d else 0.0)
            req += strand.get(node, 0.0)
            if abs(net[node] - req) > 1e-4:
                V.append(f"flow conservation {s.id}@{node}: net {net[node]:g}, required {req:g}")

        # release (10)
        for node, mode, q, dep, lt, lab in lod:
            if node == s.o and dep + EPS < s.alpha + lt:
                V.append(f"{s.id} loaded on {lab} departing {dep:g} before release {s.alpha:g}+{lt:g}")

        # deliveries (14)-(17)
        for node, mode, q, ready, lab in unl:
            if node != s.d:
                continue
            e, l = max(0.0, s.beta - ready), max(0.0, ready - s.beta)
            ev.early += q * s.rho_early * e
            ev.late += q * s.rho_late * l
            ev.early_teu_h += q * e
            ev.late_teu_h += q * l
            ev.deliveries.append({"shipment": s.id, "vehicle": lab, "teu": q, "time": ready})
            if ready > s.gamma + EPS:                                   # (15)
                V.append(f"{s.id} delivered at {ready:g} > latest {s.gamma:g} via {lab}")

        # transshipment (7)(8)(13)(21): min-cost, time-feasible matching per intermediate node
        nodes = {u[0] for u in unl if u[0] != s.d} | {l[0] for l in lod if l[0] != s.o}
        for node in nodes:
            U = [u for u in unl if u[0] == node]
            L = [l for l in lod if l[0] == node]
            if not U or not L:
                continue
            nu, nl = len(U), len(L)
            c, bounds = [], []
            for u in U:
                for l in L:
                    g, r = inst.trans(u[1], l[1])
                    ok = u[3] + r + l[4] <= l[3] + EPS
                    c.append(g if ok else g + 1e6)      # infeasible pairs heavily penalised
            A_eq, b_eq = [], []
            for a in range(nu):
                row = np.zeros(nu * nl); row[a * nl:(a + 1) * nl] = 1; A_eq.append(row); b_eq.append(U[a][2])
            for b in range(nl):
                row = np.zeros(nu * nl); row[b::nl] = 1; A_eq.append(row); b_eq.append(L[b][2])
            res = linprog(c, A_eq=np.array(A_eq), b_eq=b_eq, bounds=(0, None), method="highs")
            if not res.success:
                V.append(f"transshipment volumes do not balance for {s.id}@{node}")
                continue
            for idx, q in enumerate(res.x):
                if q < EPS:
                    continue
                a, b = divmod(idx, nl)
                g, r = inst.trans(U[a][1], L[b][1])
                ev.transshipment += q * g
                ev.transshipped_teu += q
                if c[idx] >= 1e6:
                    V.append(f"transfer {s.id}@{node} {U[a][4]}->{L[b][5]}: ready {U[a][3]:g}+{r:g}"
                             f"+load {L[b][4]:g} > dep {L[b][3]:g}")
    return ev
