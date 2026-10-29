"""Instance schema for synchromodal replanning (Qu et al., 2019, TR-E 131).

Times are in hours on a single-day clock. Conventions follow the paper:
  * y_dep  = departure time of a leg, AFTER loading has finished
  * y_arv  = arrival time of a leg, BEFORE unloading starts
  * delivery time t = y_arv + unload_time   (Table 4 "End time")
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

MODES = ("barge", "rail", "truck")


@dataclass
class Leg:
    i: str
    j: str
    run_time: float          # k_a (pure travel time)
    var_cost: float          # c_a (EUR/TEU)
    planned_dep: Optional[float] = None  # pi_a^dep (after loading); None = no pre-plan


@dataclass
class Service:
    """A pre-designed long-haul capacity service (LCS): barge or rail, possibly multi-stop."""
    id: str
    mode: str
    legs: List[Leg]
    fixed_cost: float        # f_v
    capacity: float          # u_v (TEU)
    load_time: float         # h^+ (per stop)
    unload_time: float       # h^- (per stop)
    cancel_cost: float = 0.0  # psi_v (charged when a pre-planned service is not used)
    preplanned: bool = False

    @property
    def itinerary(self) -> List[str]:
        return [self.legs[0].i] + [l.j for l in self.legs]


@dataclass
class TruckLane:
    """A truck lane i->j served by a homogeneous truck fleet (1 TEU / truck, flexible departure)."""
    id: str
    i: str
    j: str
    run_time: float
    var_cost: float
    fixed_cost: float        # per truck used
    max_trucks: int
    load_time: float = 0.5
    unload_time: float = 0.5
    capacity_per_truck: float = 1.0


@dataclass
class Shipment:
    id: str
    o: str
    d: str
    n: float                 # volume (TEU)
    alpha: float             # release time at origin
    beta: float              # due time at destination
    gamma: float             # latest delivery time (maximum latency)
    rho_early: float         # EUR / TEU / h
    rho_late: float          # EUR / TEU / h


@dataclass
class Instance:
    name: str
    terminals: List[str]
    services: List[Service]
    trucks: List[TruckLane]
    shipments: List[Shipment]
    # transshipment[(from_mode, to_mode)] = (cost EUR/TEU, time h)
    transshipment: Dict[Tuple[str, str], Tuple[float, float]]
    # rescheduling flexibility (constraints 9a/9b)
    barge_buffer: float = 3.0     # phi^dep for barge: y_dep <= pi + phi
    rail_step: float = 1.0        # rail: y_dep = pi + k*step
    rail_max_shift: float = 1.0   # k_max * step
    horizon_start: float = 0.0
    horizon_end: float = 24.0
    has_timetable: bool = False   # True if planned_dep is given for LCS legs (replanning case)
    source: str = ""
    notes: List[str] = field(default_factory=list)

    # ---------- helpers ----------
    def service(self, sid: str) -> Service:
        return next(s for s in self.services if s.id == sid)

    def truck(self, tid: str) -> TruckLane:
        return next(t for t in self.trucks if t.id == tid)

    def shipment(self, sid: str) -> Shipment:
        return next(s for s in self.shipments if s.id == sid)

    def trans(self, m_from: str, m_to: str) -> Tuple[float, float]:
        return self.transshipment[(m_from, m_to)]

    # ---------- (de)serialisation ----------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["transshipment"] = [
            {"from": a, "to": b, "cost": c, "time": t} for (a, b), (c, t) in self.transshipment.items()
        ]
        return d

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)

    @staticmethod
    def from_dict(d: dict) -> "Instance":
        d = dict(d)
        d["services"] = [
            Service(**{**s, "legs": [Leg(**l) for l in s["legs"]]}) for s in d["services"]
        ]
        d["trucks"] = [TruckLane(**t) for t in d["trucks"]]
        d["shipments"] = [Shipment(**s) for s in d["shipments"]]
        d["transshipment"] = {(t["from"], t["to"]): (t["cost"], t["time"]) for t in d["transshipment"]}
        return Instance(**d)

    @staticmethod
    def load(path: str) -> "Instance":
        with open(path, encoding="utf-8") as f:
            return Instance.from_dict(json.load(f))

    def summary(self) -> str:
        return (f"{self.name}: {len(self.terminals)} terminals, {len(self.services)} LCS "
                f"({sum(len(s.legs) for s in self.services)} legs), {len(self.trucks)} truck lanes, "
                f"{len(self.shipments)} shipments ({sum(s.n for s in self.shipments):g} TEU)")


def default_transshipment(cost=23.89, time=1.0, truck_truck_cost=0.0, truck_truck_time=0.0):
    t = {(a, b): (cost, time) for a in MODES for b in MODES}
    t[("truck", "truck")] = (truck_truck_cost, truck_truck_time)
    return t
