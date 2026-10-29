"""Turn a 4TU planning instance into a REPLANNING case like Rotterdam (step 6c).

The 4TU workbooks are planning-from-scratch problems (no timetable, no original plan). A replanning case needs both.
Construction (documented choices):
  1. Solve the STP from scratch (no disturbance) -> optimal plan P0. P0 is the "original plan" of the day.
  2. Every service used in P0 becomes pre-planned: its leg departures in P0 become the timetable pi, cancelling it
     costs 0.5 f (as in the paper's Rotterdam case).
  3. Services unused in P0 stay available but not pre-planned (no cancellation fee), with a nominal timetable:
     first leg departs 8:00 after loading, later legs depart on arrival.
  4. Rescheduling rules as in Rotterdam: barge pi <= y <= pi + 3 h; rail y = pi + k*0.5 h, k*0.5 <= 1 h.
  5. Trucks in P0 are the trucks booked in the original plan.
Validation: P0 must be feasible under the new timetable with unchanged cost, and re-solving the undisturbed case must
return the same optimum.
"""
from __future__ import annotations

import copy
import json
import os

from .evaluate import evaluate
from .instance import Instance
from .model import STPModel, Options
from .plan import Plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOMINAL_FIRST_DEP = 8.0


def make_replanning_case(name: str, backend: str = "auto", cache: bool = True):
    out_dir = f"{ROOT}/data/cases"
    ipath, ppath = f"{out_dir}/{name}_replan.json", f"{out_dir}/{name}_replan_base_plan.json"
    if cache and os.path.exists(ipath) and os.path.exists(ppath):
        return Instance.load(ipath), Plan.load(ppath)
    inst = Instance.load(f"{ROOT}/data/instances/{name}.json")
    r = STPModel(inst, Options(backend=backend, time_limit=600)).solve()
    if r.plan is None or r.plan.unserved or r.plan.stranded:
        raise RuntimeError(f"{name}: no complete original plan ({r.status})")
    p0 = r.plan
    case = copy.deepcopy(inst)
    case.name = f"{name}_replan"
    used = {sid for (sid, k) in p0.lcs_flow if p0.leg_load((sid, k)) > 1e-9}
    for v in case.services:
        if v.id in used:
            v.preplanned, v.cancel_cost = True, 0.5 * v.fixed_cost
            deps = [p0.lcs_dep[(v.id, k)] for k in range(len(v.legs))]
            loaded = [p0.leg_load((v.id, k)) > 1e-9 for k in range(len(v.legs))]
            # empty legs keep a coherent journey: leading empty legs arrive when the next leg leaves,
            # trailing empty legs leave on arrival (the solver leaves empty legs at arbitrary bounds)
            for k in range(len(v.legs) - 2, -1, -1):
                if not loaded[k] and loaded[k + 1]:
                    deps[k] = deps[k + 1] - v.legs[k].run_time
            for k in range(1, len(v.legs)):
                if not loaded[k]:
                    deps[k] = deps[k - 1] + v.legs[k - 1].run_time
            for k, leg in enumerate(v.legs):
                leg.planned_dep = deps[k]
                p0.lcs_dep[(v.id, k)] = deps[k]
        else:
            v.preplanned, v.cancel_cost = False, 0.0
            t = NOMINAL_FIRST_DEP
            for leg in v.legs:
                leg.planned_dep = t
                t = t + leg.run_time
    case.has_timetable = True
    case.barge_buffer, case.rail_step, case.rail_max_shift = 3.0, 0.5, 1.0
    case.notes = list(case.notes) + [__doc__.strip()]
    p0.name = f"original plan of {case.name}"
    # validation
    ev = evaluate(case, p0)
    if not ev.feasible:
        raise RuntimeError(f"{name}: original plan infeasible under its own timetable: {ev.violations[:3]}")
    if abs(ev.total - r.objective) > 0.5:
        raise RuntimeError(f"{name}: cost changed {r.objective} -> {ev.total}")
    r2 = STPModel(case, Options(backend=backend, anchor_plan=p0)).solve()
    ev2 = evaluate(case, r2.plan)
    if ev2.total > ev.total + 0.5:
        raise RuntimeError(f"{name}: replanning the undisturbed case is worse than the original plan")
    os.makedirs(out_dir, exist_ok=True)
    case.save(ipath)
    p0.meta.update({"cost": round(ev.total, 2), "used_services": sorted(used), "trucks": ev.trucks_used})
    p0.save(ppath)
    return case, p0
