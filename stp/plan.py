"""Solution (plan) representation shared by all methods: STP / N-STP / R-STP / ours."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

LegKey = Tuple[str, int]   # (service_id, leg_index)


@dataclass
class TruckMove:
    lane: str          # TruckLane.id
    shipment: str
    teu: float
    dep: float         # departure after loading


@dataclass
class Plan:
    name: str
    # x_a^s for LCS legs: {(service, leg_idx): {shipment: TEU}}
    lcs_flow: Dict[LegKey, Dict[str, float]] = field(default_factory=dict)
    # y_a^dep for LCS legs (after loading). Arrival = dep + run_time unless overridden.
    lcs_dep: Dict[LegKey, float] = field(default_factory=dict)
    lcs_arr_override: Dict[LegKey, float] = field(default_factory=dict)
    trucks: List[TruckMove] = field(default_factory=list)
    unserved: Dict[str, float] = field(default_factory=dict)   # TEU not transported (soft demand)
    stranded: Dict[str, float] = field(default_factory=dict)   # "S4@Dordrecht" -> TEU left at a terminal
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def add_lcs(self, sid: str, leg: int, shipment: str, teu: float):
        self.lcs_flow.setdefault((sid, leg), {})
        self.lcs_flow[(sid, leg)][shipment] = self.lcs_flow[(sid, leg)].get(shipment, 0.0) + teu

    def leg_load(self, key: LegKey) -> float:
        return sum(self.lcs_flow.get(key, {}).values())

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lcs_flow": [{"service": k[0], "leg": k[1], "flows": v} for k, v in self.lcs_flow.items()],
            "lcs_dep": [{"service": k[0], "leg": k[1], "dep": v} for k, v in self.lcs_dep.items()],
            "lcs_arr_override": [{"service": k[0], "leg": k[1], "arr": v}
                                 for k, v in self.lcs_arr_override.items()],
            "trucks": [asdict(t) for t in self.trucks],
            "unserved": self.unserved,
            "stranded": self.stranded,
            "meta": self.meta,
        }

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        p = Plan(d["name"], meta=d.get("meta", {}))
        for e in d["lcs_flow"]:
            p.lcs_flow[(e["service"], e["leg"])] = dict(e["flows"])
        for e in d["lcs_dep"]:
            p.lcs_dep[(e["service"], e["leg"])] = e["dep"]
        for e in d.get("lcs_arr_override", []):
            p.lcs_arr_override[(e["service"], e["leg"])] = e["arr"]
        p.trucks = [TruckMove(**t) for t in d["trucks"]]
        p.unserved = dict(d.get("unserved", {}))
        p.stranded = dict(d.get("stranded", {}))
        return p

    @staticmethod
    def load(path: str) -> "Plan":
        with open(path, encoding="utf-8") as f:
            return Plan.from_dict(json.load(f))
