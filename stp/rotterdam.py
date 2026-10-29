"""Rotterdam hinterland case of Qu et al. (2019), rebuilt from Tables 2-4 and A.12-A.14.

Corrections applied (each verified against the paper's own reported costs):
  C1  Rail v0006 (Tilburg-Venlo) variable cost = 22.62 EUR/TEU, not the 22.16 printed in Table 2.
      With 22.62 the base case variable cost is exactly 10,476 (Sec. 6.2); with 22.16 it is 10,453.
  C2  Table A.12 (STP re-plan) omits a row: 50 trucks carry S5 PoR->Dordrecht to feed barge v0005.
      Without it flow conservation fails for S5; with it C^c = 11,332.2 and C^f = 1,770 exactly
      as reported in Table 9.
Interpretation choices (documented, not in the paper's tables):
  I1  Table 2 "Travel time" = pure run time k_a; loading/unloading = 1 h (LCS) / 0.5 h (truck);
      Table 4 "Start" = start of loading, "End" = end of unloading. This reproduces every
      start/end pair in Table 4.
  I2  Each truck lane has a fleet of 500 one-TEU trucks (v0007-v0506 etc.), fixed cost 15 EUR/truck.
  I3  Cancellation fee psi = 0.5 f for pre-planned LCS (Sec. 6.1).
  I4  Barge buffer phi = 3 h; rail shifts in 0.5 h steps up to 1 h (Sec. 4.3: "every time unit phi, e.g. 30 or
      60 min", k_max*phi = 1 h). 0.5 h fits Tables 5-8 better than 1 h (50 vs 49 rows cost-equal) and keeps Table 9.
"""
from __future__ import annotations

import copy

from .instance import Instance, Service, Leg, TruckLane, Shipment, default_transshipment
from .plan import Plan, TruckMove

LCS_LOAD = 1.0
TRUCK_LOAD = 0.5

# id, mode, i, j, capacity, run time, fixed, variable, planned START (loading begins) from Table 4
_LCS = [
    ("v0001", "barge", "PoR", "Dordrecht", 120, 2, 60, 2.45, 7),
    ("v0002", "rail", "PoR", "Tilburg", 100, 2, 30, 30.16, 10),
    ("v0003", "rail", "PoR", "Nijmegen", 60, 2, 30, 30.16, 14),
    ("v0004", "barge", "Dordrecht", "Nijmegen", 120, 5, 60, 4.29, 15),
    ("v0005", "barge", "Dordrecht", "Venlo", 120, 9, 60, 6.73, 12),
    ("v0006", "rail", "Tilburg", "Venlo", 100, 1, 30, 22.62, 15),   # C1
]
# id, i, j, run time, variable cost  (fixed 15 EUR/truck, 1 TEU/truck, 500 trucks)
_TRUCK = [
    ("T_PoR_Utrecht", "PoR", "Utrecht", 1.0, 61.96),
    ("T_PoR_Dordrecht", "PoR", "Dordrecht", 0.5, 30.98),
    ("T_Utrecht_Nijmegen", "Utrecht", "Nijmegen", 1.0, 61.96),
    ("T_Dordrecht_Tilburg", "Dordrecht", "Tilburg", 1.0, 30.98),
    ("T_Tilburg_Venlo", "Tilburg", "Venlo", 1.0, 30.98),
    ("T_Nijmegen_Venlo", "Nijmegen", "Venlo", 1.0, 30.98),
]
# Table 3
_SHIP = [("S1", "Utrecht", 50), ("S2", "Dordrecht", 50), ("S3", "Tilburg", 50),
         ("S4", "Nijmegen", 100), ("S5", "Venlo", 100)]


def build_instance() -> Instance:
    services = [
        Service(id=v, mode=m, legs=[Leg(i, j, rt, c, planned_dep=start + LCS_LOAD)], fixed_cost=f,
                capacity=cap, load_time=LCS_LOAD, unload_time=LCS_LOAD, cancel_cost=0.5 * f,
                preplanned=True)
        for v, m, i, j, cap, rt, f, c, start in _LCS
    ]
    trucks = [TruckLane(id=t, i=i, j=j, run_time=rt, var_cost=c, fixed_cost=15.0, max_trucks=500,
                        load_time=TRUCK_LOAD, unload_time=TRUCK_LOAD)
              for t, i, j, rt, c in _TRUCK]
    shipments = [Shipment(id=s, o="PoR", d=d, n=n, alpha=7, beta=18, gamma=24, rho_early=0.5, rho_late=1.5)
                 for s, d, n in _SHIP]
    return Instance(
        name="rotterdam", terminals=["PoR", "Utrecht", "Nijmegen", "Dordrecht", "Tilburg", "Venlo"],
        services=services, trucks=trucks, shipments=shipments,
        transshipment=default_transshipment(23.89, 1.0, truck_truck_cost=0.0, truck_truck_time=0.0),
        barge_buffer=3.0, rail_step=0.5, rail_max_shift=1.0, horizon_start=0.0, horizon_end=24.0,
        has_timetable=True, source="Qu et al. 2019, Tables 2-4 (with corrections C1-C2)",
        notes=[__doc__.strip()],
    )


# ----------------------------------------------------------------------------- reference plans
def _plan(name, lcs_rows, truck_rows, meta=None) -> Plan:
    """lcs_rows: (service, START, {ship: teu});  truck_rows: (lane, START, ship, teu)."""
    p = Plan(name, meta=meta or {})
    for sid, start, flows in lcs_rows:
        p.lcs_dep[(sid, 0)] = start + LCS_LOAD
        for s, q in flows.items():
            p.add_lcs(sid, 0, s, q)
    for lane, start, s, q in truck_rows:
        p.trucks.append(TruckMove(lane=lane, shipment=s, teu=q, dep=start + TRUCK_LOAD))
    return p


def base_plan() -> Plan:
    """Table 4 - original plan, no disturbance. Paper: C = 15,961."""
    return _plan("base (Table 4)", [
        ("v0001", 7, {"S2": 30, "S4": 40, "S5": 50}),
        ("v0002", 10, {"S3": 50, "S5": 50}),
        ("v0003", 14, {"S4": 60}),
        ("v0004", 15, {"S4": 40}),
        ("v0005", 12, {"S5": 50}),
        ("v0006", 15, {"S5": 50}),
    ], [
        ("T_PoR_Utrecht", 16, "S1", 50),
        ("T_PoR_Dordrecht", 16.5, "S2", 20),
    ], meta={"paper_total": 15961, "paper": {"fixed": 1320, "variable": 10476, "transshipment": 3345,
                                              "early": 205, "late": 615}})


def late_release(inst: Instance, delays: dict) -> Instance:
    """Scenario helper: shipments released later, e.g. {'S4': 2, 'S5': 2}."""
    inst = copy.deepcopy(inst)
    for s in inst.shipments:
        if s.id in delays:
            s.alpha += delays[s.id]
    inst.name += "+" + ",".join(f"{k}+{v}h" for k, v in delays.items())
    return inst


def table9_plans() -> dict:
    """Appendix A: S4 and S5 released at 9:00 instead of 7:00."""
    stp = _plan("STP (Table A.12 + omitted truck row)", [
        ("v0001", 10, {"S2": 50, "S4": 40}),
        ("v0002", 10, {"S3": 50, "S5": 50}),
        ("v0003", 14, {"S4": 60}),
        ("v0004", 15, {"S4": 40}),
        ("v0005", 12, {"S5": 50}),
        ("v0006", 15, {"S5": 50}),
    ], [
        ("T_PoR_Utrecht", 16, "S1", 50),
        ("T_PoR_Dordrecht", 9, "S5", 50),     # C2: row missing from Table A.12
    ], meta={"paper": {"total": 17261.8, "fixed": 1770, "variable": 11332.2, "transshipment": 3344.6,
                       "early": 200, "late": 615}})
    nstp = _plan("N-STP (Table A.13)", [
        ("v0001", 10, {"S4": 100}),
        ("v0002", 11, {"S3": 50}),
        ("v0004", 15, {"S4": 100}),
        ("v0005", 12, {"S5": 100}),
    ], [
        ("T_PoR_Utrecht", 16, "S1", 50),
        ("T_PoR_Dordrecht", 16.5, "S2", 50),
        ("T_PoR_Dordrecht", 9, "S5", 100),
    ], meta={"paper": {"total": 20043, "fixed": 3240, "variable": 10600, "transshipment": 4778,
                       "early": 75, "late": 1350}})
    rstp = _plan("R-STP (Table A.14)", [
        ("v0001", 7, {"S2": 50}),
        ("v0002", 10, {"S3": 50, "S5": 50}),
        ("v0003", 14, {"S4": 60}),
        ("v0004", 15, {"S4": 40}),
        ("v0005", 12, {"S5": 50}),
        ("v0006", 15, {"S5": 50}),
    ], [
        ("T_PoR_Utrecht", 16, "S1", 50),
        ("T_PoR_Dordrecht", 9, "S4", 40),
        ("T_PoR_Dordrecht", 9, "S5", 50),
    ], meta={"paper": {"total": 19078, "fixed": 2370, "variable": 12473.4, "transshipment": 3344.6,
                       "early": 275, "late": 615}})
    return {"STP": stp, "N-STP": nstp, "R-STP": rstp}
