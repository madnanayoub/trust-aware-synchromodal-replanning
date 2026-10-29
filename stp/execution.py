"""Plan-on-belief / execute-on-truth simulator with recourse.

Timeline of one operational day:
  t0 (7:00)   the LSP re-plans with its BELIEF about today's disturbances (stage 1). Trucks in that plan are
              booked at normal rates; LCS rescheduling is announced.
  t_r         the first moment a belief/truth discrepancy becomes physically observable:
                release s:   cargo appears at alpha + true delay, or fails to appear at alpha + believed delay
                lcs_delay v: the vehicle is (or is not) ready to load at pi - load + min(believed, true)
                volume s:    counted at the gate when s is released
              The full truth becomes known at t_r (optimistic for recourse - favours report-trusting policies).
  stage 2     STP re-solved on the TRUE instance. Everything whose loading started before t_r is frozen (only
              the cargo physically available is on board). Trucks booked in stage 1 are prepaid (sunk if
              unused); trucks beyond the booking are hired on the spot market at a premium.
Realised cost = cost of the executed (stage-2) plan + unused booked trucks + spot premium.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .evaluate import evaluate
from .instance import Instance
from .model import STPModel, Options
from .plan import Plan, TruckMove
from .scenarios import apply_events

SPOT_PREMIUM = 0.5     # spot trucks cost 50% more (fixed + variable) than trucks booked at t0
T0 = 7.0


@dataclass
class Outcome:
    policy: str
    realized_cost: float
    plan_cost: float              # cost the stage-1 plan expected (under the belief)
    reveal_time: Optional[float]
    recourse: bool
    booked_trucks: int
    spot_trucks: int
    unused_booked: int
    late_teuh: float
    early_teuh: float
    unserved_teu: float
    churn_teu: float              # TEU moved between stage-1 and executed plan (operational turbulence)
    solve_s: float
    feasible: bool
    violations: List[str] = field(default_factory=list)


def _canon(events):
    return {(k, t): round(m, 4) for k, t, m in events}


def reveal_time(base: Instance, true_events, belief_events) -> Optional[float]:
    tr, bl = _canon(true_events), _canon(belief_events)
    times = []
    for key in set(tr) | set(bl):
        kind, target = key
        t, b = tr.get(key, 0.0), bl.get(key, 0.0)
        if abs(t - b) < 1e-6:
            continue
        if kind == "release":
            times.append(base.shipment(target).alpha + min(t, b))
        elif kind == "lcs_delay":
            v = base.service(target)
            times.append(v.legs[0].planned_dep - v.load_time + min(t, b))
        elif kind == "volume":
            times.append(base.shipment(target).alpha + tr.get(("release", target), 0.0))
    return max(T0, min(times)) if times else None


def _truck_counts(plan: Plan) -> Dict[str, int]:
    c = defaultdict(int)
    for t in plan.trucks:
        c[t.lane] += int(round(t.teu))
    return dict(c)


def _freeze(true_inst: Instance, plan: Plan, t_r: float):
    """Decisions started before t_r, truncated to what is physically possible under the truth."""
    budget = {s.id: s.n for s in true_inst.shipments}
    at_node = defaultdict(float)             # (shipment, node) -> TEU delivered there by frozen moves
    events = []
    for (sid, k), dep in plan.lcs_dep.items():
        svc = true_inst.service(sid)
        if dep - svc.load_time < t_r - 1e-9:
            events.append((dep, "L", (sid, k), svc.legs[k].i, svc.legs[k].j, svc.load_time,
                           dict(plan.lcs_flow.get((sid, k), {}))))
    for tm in plan.trucks:
        lane = true_inst.truck(tm.lane)
        if tm.dep - lane.load_time < t_r - 1e-9:
            events.append((tm.dep, "T", tm, lane.i, lane.j, lane.load_time, {tm.shipment: tm.teu}))
    frozen_lcs, frozen_trucks = {}, []
    for dep, typ, key, i, j, load, flows in sorted(events, key=lambda e: e[0]):
        kept = {}
        for s, q in flows.items():
            sh = true_inst.shipment(s)
            if i == sh.o:
                ok = sh.alpha + load <= dep + 1e-9
                q2 = float(round(min(q, budget[s]))) if ok else 0.0
                budget[s] -= q2
            else:
                q2 = float(round(min(q, at_node[(s, i)])))
                at_node[(s, i)] -= q2
            if q2 > 1e-9:
                kept[s] = q2
                at_node[(s, j)] += q2
        if typ == "L":
            frozen_lcs[key] = {"dep": dep, "flows": kept}
        else:
            for s, q in kept.items():
                frozen_trucks.append(TruckMove(lane=key.lane, shipment=s, teu=q, dep=dep))
    return frozen_lcs, frozen_trucks


def realized_cost(inst: Instance, plan: Plan, booked: Dict[str, int], premium: float):
    ev = evaluate(inst, plan)
    used = _truck_counts(plan)
    unused = spot = 0
    extra = 0.0
    for l in inst.trucks:
        b, u = booked.get(l.id, 0), used.get(l.id, 0)
        unused += max(0, b - u)
        spot += max(0, u - b)
        extra += l.fixed_cost * max(0, b - u) + premium * (l.fixed_cost + l.var_cost) * max(0, u - b)
    return ev, ev.total + extra, unused, spot


def _flows(plan):
    f = defaultdict(float)
    for (sid, k), d in plan.lcs_flow.items():
        for s, q in d.items():
            f[("L", sid, k, s)] += q
    for t in plan.trucks:
        f[("T", t.lane, t.shipment)] += t.teu
    return f


def execute(base: Instance, base_plan: Plan, true_events, belief_events, policy: str,
            backend: str = "auto", premium: float = SPOT_PREMIUM, stage1_plan: Plan = None) -> Outcome:
    t_start = time.time()
    # ---- stage 1: plan on belief
    if stage1_plan is not None:
        p1 = stage1_plan
    elif not belief_events:
        p1 = base_plan
    else:
        bi, un = apply_events(base, belief_events)
        r1 = STPModel(bi, Options(backend=backend, anchor_plan=base_plan, allow_unserved=True,
                                  unavailable_lcs=un)).solve()
        p1 = r1.plan
    bi, _ = apply_events(base, belief_events)
    plan_cost = evaluate(bi, p1).total
    booked = _truck_counts(p1)
    # ---- truth
    ti, un_t = apply_events(base, true_events)
    t_r = reveal_time(base, true_events, belief_events)
    if t_r is None:
        ev, cost, unused, spot = realized_cost(ti, p1, booked, premium)
        p2, recourse = p1, False
    else:
        flcs, ftr = _freeze(ti, p1, t_r)
        r2 = STPModel(ti, Options(backend=backend, anchor_plan=p1, allow_unserved=True, unavailable_lcs=un_t,
                                  frozen_lcs=flcs, frozen_trucks=ftr, booked_trucks=booked,
                                  spot_premium=premium)).solve()
        if r2.plan is None:
            raise RuntimeError(f"stage-2 infeasible ({r2.status}) for {policy} {true_events} / {belief_events}")
        p2, recourse = r2.plan, True
        ev, cost, unused, spot = realized_cost(ti, p2, booked, premium)
    f1, f2 = _flows(p1), _flows(p2)
    churn = sum(abs(f1.get(k, 0) - f2.get(k, 0)) for k in set(f1) | set(f2)) / 2
    return Outcome(policy=policy, realized_cost=round(float(cost), 2), plan_cost=round(float(plan_cost), 2),
                   reveal_time=t_r, recourse=recourse, booked_trucks=sum(booked.values()), spot_trucks=spot,
                   unused_booked=unused, late_teuh=round(ev.late_teu_h, 2), early_teuh=round(ev.early_teu_h, 2),
                   unserved_teu=round(ev.unserved_teu, 2), churn_teu=round(churn, 2),
                   solve_s=round(time.time() - t_start, 2), feasible=ev.feasible, violations=ev.violations)
