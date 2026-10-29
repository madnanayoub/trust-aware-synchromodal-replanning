"""Disturbance scenarios (paper Sec. 6.4-6.6) and decision freezing for replanning.

Event encoding (JSON-friendly lists):
  ["release",   shipment, hours]     shipment released `hours` later than alpha  (Table 5)
  ["lcs_delay", service,  hours]     LCS cannot depart before pi + hours          (Table 6)
                                     -> its timetable is shifted; rescheduling flexibility (9a)/(9b)
                                        then applies around the shifted time
  ["volume",    shipment, fraction]  volume n^s * (1 + fraction), rounded to TEU (Table 7)
  ["cancel",    service,  0]         service broken down / unavailable (extension)
Mixed events = several events in one list (Table 8).
"""
from __future__ import annotations

import copy
from typing import Dict, List, Sequence, Tuple

from .instance import Instance
from .plan import Plan

Event = Sequence  # [kind, target, magnitude]


def apply_events(inst: Instance, events: List[Event]) -> Tuple[Instance, List[str]]:
    """Return a disturbed copy of the instance and the list of unavailable services."""
    inst = copy.deepcopy(inst)
    unavailable = []
    tags = []
    for kind, target, mag in events:
        if kind == "release":
            inst.shipment(target).alpha += mag
            tags.append(f"{target}+{mag:g}h")
        elif kind == "lcs_delay":
            for leg in inst.service(target).legs:
                if leg.planned_dep is not None:
                    leg.planned_dep += mag
            tags.append(f"{target}+{mag:g}h")
        elif kind == "volume":
            s = inst.shipment(target)
            s.n = float(round(s.n * (1 + mag)))
            tags.append(f"{target}{mag:+.0%}")
        elif kind == "cancel":
            unavailable.append(target)
            tags.append(f"{target}x")
        else:
            raise ValueError(f"unknown event kind {kind}")
    inst.name = f"{inst.name}[{','.join(tags)}]"
    return inst, unavailable


def frozen_before(inst: Instance, plan: Plan, t: float) -> Dict:
    """Decisions already executed at replanning time t: LCS legs whose loading has started
    (dep - load_time < t) keep their departure and loads (paper Sec. 5.2, 'fixing part of variables')."""
    frozen = {}
    for (sid, k), dep in plan.lcs_dep.items():
        svc = inst.service(sid)
        if dep - svc.load_time < t - 1e-9:
            frozen[(sid, k)] = {"dep": dep, "flows": dict(plan.lcs_flow.get((sid, k), {}))}
    return frozen
