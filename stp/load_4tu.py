"""Loader for the 4TU.ResearchData theoretical instances of Qu et al. (2019)
(doi:10.4121/uuid:ce696560-ef05-460d-86f2-39cfefc26416), i.e. Tables 10-11 of the paper.

Every deviation from the raw workbook is appended to `Instance.notes` and to the issue log
returned by `load_workbook`, so the audit report can list exactly what was changed and why.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

import openpyxl

from .instance import Instance, Service, Leg, TruckLane, Shipment, default_transshipment
from .plan import Plan, TruckMove

KEY = re.compile(r'<"(T\d+)" "(T\d+)" "(\w+)" (\d+)>')

# Defaults used ONLY when a workbook lacks the column; values are those listed in every
# workbook that does have them (9nodes + the five topology files) and in the paper (Sec. 6.1).
DEFAULT_CAPACITY_BY_SEQ = {1: 120, 2: 100, 3: 60, 4: 120, 5: 120, 6: 100}
DEFAULT_CAPACITY_BY_MODE = {"barge": 120, "rail": 100}
DEFAULT_RUN_BY_MODE = {"barge": 2.0, "rail": 1.5, "truck": 1.0}
DEFAULT_LOAD = {"barge": 1.0, "rail": 1.0, "truck": 0.5}
TRUCK_FLEET = 500  # paper Sec. 6.1/Table 2: 500 one-TEU trucks per lane

# Instance-specific decisions (see reports/step2_data_audit.md)
SHIPMENT_LIMIT = {"10nodes": 5}  # Sheet2 solved S1-S5 only; Table 10 row "10 nodes / 5 shipments"
# (instance, shipment) -> (bad terminal, replacement, justification)
DEST_FIX = {("8nodes", "S4"): ("T9", "T8", "the reported Sheet2 flows deliver S4 at T8, the last terminal"),
            ("8nodes", "S5"): ("T8", "T7", "T8 is unreachable from T4 on any arc; the reported Sheet2 flows "
                                           "deliver S5 at T7")}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rows(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _input_sheet(wb):
    for name in ("network and shipmnts", "Sheet1"):
        if name in wb.sheetnames:
            return wb[name]
    return wb.worksheets[0]


def _find_label(rows, label):
    lab = label.lower()
    for r in rows:
        for j, c in enumerate(r):
            if isinstance(c, str) and c.strip().lower() == lab:
                return r, j
    return None, None


def load_workbook(path: str, name: str) -> Tuple[Instance, Plan, dict, List[str]]:
    """Returns (instance, reported_solution_plan, reported_costs, issues)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    rows = [r for r in _rows(_input_sheet(wb)) if any(c is not None for c in r)]
    issues: List[str] = []

    terminals = [str(c).strip() for c in rows[0][1:] if c]
    header = [str(h).strip().lower() if h else "" for h in rows[1]]

    def col(*cands):
        for c in cands:
            for j, h in enumerate(header):
                if h == c.lower():
                    return j
        return None

    c_fixed = col("fixe cost", "fixed cost")
    c_var = [col("variable cost arc1", "variable cost"), col("variable cost arc 2", "vairable cost (arc 2)"),
             col("variable cost arc  3")]
    if c_var[1] is None and c_var[0] is not None:
        c_var[1] = c_var[0] + 1                          # unlabeled 2nd variable-cost column
    c_run = [col("pure run time arc 1"), col("pure run time arc 2"), col("pure run t arc 3")]
    c_load, c_unload = col("loading time"), col("unloading t")
    c_cap = col("capacity", "cpacity")
    if c_run[0] is None:
        issues.append("Input sheet has no run-time / loading-time columns -> mode defaults "
                      f"{DEFAULT_RUN_BY_MODE}, load/unload {DEFAULT_LOAD} (values from the 9-node file "
                      "and the 10-node Sheet2 'RunT' column).")
    if c_cap is None:
        issues.append(f"Input sheet has no capacity column -> capacity by sequence number "
                      f"{DEFAULT_CAPACITY_BY_SEQ}, else by mode {DEFAULT_CAPACITY_BY_MODE}.")

    # ---------------- services and truck lanes
    services, trucks, raw_svc = [], [], []
    for r in rows[2:]:
        lab = str(r[1]).strip().lower() if r[1] else ""
        if not (lab.startswith("vehicle") or lab.startswith("truck group")):
            continue
        mode = str(r[3]).strip().lower()
        legs = [str(r[4 + k]).strip() for k in range(3) if r[4 + k]]
        fixed = _f(r[c_fixed])
        if lab.startswith("truck group"):
            gi = int(lab.split()[-1])
            i, j = legs[0].split("-")
            rt = _f(r[c_run[0]]) if c_run[0] is not None else None
            ld = _f(r[c_load]) if c_load is not None else None
            seq = str(r[2])
            trucks.append(TruckLane(id=f"TG{gi}", i=i, j=j, run_time=rt or DEFAULT_RUN_BY_MODE["truck"],
                                    var_cost=_f(r[c_var[0]]), fixed_cost=fixed, max_trucks=TRUCK_FLEET,
                                    load_time=ld or DEFAULT_LOAD["truck"],
                                    unload_time=(_f(r[c_unload]) if c_unload is not None else None)
                                    or ld or DEFAULT_LOAD["truck"]))
            if "--" in seq or (seq.count("-") == 1 and int(seq.split("-")[1]) - int(seq.split("-")[0]) < 10):
                raw_svc.append(("trucks5", gi, seq))
            continue
        seq = int(r[2])
        raw_svc.append(("lcs", seq, mode))
        lg = []
        for k, leg in enumerate(legs):
            i, j = leg.split("-")
            vc = _f(r[c_var[k]]) if c_var[k] is not None else None
            if vc is None:
                vc = _f(r[c_var[0]])
                if k > 0:
                    issues.append(f"vehicle {seq}: no variable cost for leg {k+1} -> reuse leg-1 value {vc}")
            rt = _f(r[c_run[k]]) if c_run[k] is not None else None
            if rt is None:
                rt = _f(r[c_run[0]]) if c_run[0] is not None else None
                rt = rt if rt is not None else DEFAULT_RUN_BY_MODE.get(mode, 2.0)
            lg.append(Leg(i, j, rt, vc))
        ld = _f(r[c_load]) if c_load is not None else None
        ul = _f(r[c_unload]) if c_unload is not None else None
        if c_load is not None and ul is None:
            issues.append(f"vehicle {seq}: unloading time blank -> set equal to loading time")
        cap = _f(r[c_cap]) if c_cap is not None else None
        if cap is None:
            cap = DEFAULT_CAPACITY_BY_SEQ.get(seq) or DEFAULT_CAPACITY_BY_MODE[mode]
        ld = ld if ld is not None else DEFAULT_LOAD[mode]
        services.append(Service(id=f"v{seq}", mode=mode, legs=lg, fixed_cost=fixed, capacity=cap,
                                load_time=ld, unload_time=ul if ul is not None else ld))
    if any(x[0] == "trucks5" for x in raw_svc):
        issues.append("Truck groups numbered as 5 arcs (e.g. '11--15'), but Sheet2 shows single truck arcs "
                      "carrying up to 100 TEU with fixed cost 15/TEU -> modelled as 500 one-TEU trucks per lane "
                      "(identical cost structure, as in paper Sec. 6.1).")

    # ---------------- transshipment matrix
    trans = default_transshipment()
    _, jt = _find_label(rows, "transshipment")
    if jt is not None:
        start = next(k for k, r in enumerate(rows) if r[jt] and str(r[jt]).strip().lower() == "transshipment")
        for r in rows[start + 1:start + 10]:
            a, b = str(r[jt]).strip().lower(), str(r[jt + 1]).strip().lower().replace("trauck", "truck")
            if a in ("barge", "rail", "truck") and b in ("barge", "rail", "truck"):
                trans[(a, b)] = (_f(r[jt + 2]), _f(r[jt + 3]))

    # ---------------- shipments
    def vals(label):
        r, j = _find_label(rows, label)
        return [c for c in r[j + 1:] if c is not None and str(c).strip() != ""]
    vol = vals("Volume")
    nS = len(vol)
    lim = SHIPMENT_LIMIT.get(name)
    if lim and lim < nS:
        issues.append(f"Sheet1 lists {nS} shipments but Sheet2 (and Table 10) correspond to {lim}; "
                      f"using S1-S{lim}.")
        nS = lim
    rel, due, lat = vals("release time"), vals("due time"), vals("maximum latency")
    re_, rl = vals("early peanlty"), vals("late penalty")
    org, dst = vals("Origin"), vals("Destination")
    shipments = [Shipment(id=f"S{k+1}", o=str(org[k]), d=str(dst[k]), n=float(vol[k]), alpha=float(rel[k]),
                          beta=float(due[k]), gamma=float(lat[k]), rho_early=float(re_[k]), rho_late=float(rl[k]))
                 for k in range(nS)]
    for s in shipments:
        fix = DEST_FIX.get((name, s.id))
        if fix and s.d == fix[0]:
            issues.append(f"{s.id}: destination {fix[0]} invalid in the {len(terminals)}-terminal network; "
                          f"set to {fix[1]} ({fix[2]})")
            s.d = fix[1]
        for t in (s.o, s.d):
            if t not in terminals:
                issues.append(f"{s.id}: terminal {t} is not in the network ({len(terminals)} terminals) "
                              "-> instance flagged INVALID for this shipment")
    # reachability ignoring time (any LCS leg or truck lane may be used)
    adj = defaultdict(set)
    for r in rows[2:]:
        lab = str(r[1]).strip().lower() if r[1] else ""
        if lab.startswith(("vehicle", "truck group")):
            for k in range(3):
                if r[4 + k]:
                    a, b = str(r[4 + k]).strip().split("-")
                    adj[a].add(b)
    for s in shipments:
        seen, stack = {s.o}, [s.o]
        while stack:
            for b in adj[stack.pop()]:
                if b not in seen:
                    seen.add(b); stack.append(b)
        if s.d not in seen:
            issues.append(f"{s.id}: destination {s.d} unreachable from {s.o} -> instance INFEASIBLE as given")

    inst = Instance(name=name, terminals=terminals, services=services, trucks=trucks, shipments=shipments,
                    transshipment=trans, has_timetable=False, source=f"4TU workbook {path.split('/')[-1]}")

    # ---------------- reported solution (Sheet2): mode labels, flows, costs
    plan, reported = _parse_solution(wb, inst, issues)
    _reconcile_modes(wb, inst, issues, runtime_defaulted=c_run[0] is None)
    inst.notes = list(issues)
    return inst, plan, reported, issues


def _reconcile_modes(wb, inst: Instance, issues: List[str], runtime_defaulted: bool = False):
    """Sheet1 vs Sheet2 mode labels. Rule: Sheet2 wins only if all its labels for that vehicle agree."""
    if "Sheet2" not in wb.sheetnames:
        return
    labels = defaultdict(list)
    for r in _rows(wb["Sheet2"]):
        for c in r:
            m = KEY.match(c) if isinstance(c, str) else None
            if m and m.group(3) != "truck":
                labels[int(m.group(4))].append(m.group(3))
    for svc in inst.services:
        seq = int(svc.id[1:])
        lab = Counter(labels.get(seq, []))
        if not lab or set(lab) == {svc.mode}:
            continue
        if len(lab) == 1:
            new = next(iter(lab))
            issues.append(f"{svc.id}: Sheet1 mode '{svc.mode}' but every Sheet2 label says '{new}' "
                          f"(fixed {svc.fixed_cost:g}, var {svc.legs[0].var_cost:g}) -> mode set to '{new}'")
            svc.mode = new
            if runtime_defaulted:
                for leg in svc.legs:
                    leg.run_time = DEFAULT_RUN_BY_MODE[new]
        else:
            issues.append(f"{svc.id}: Sheet2 labels inconsistent {dict(lab)}; keeping Sheet1 mode '{svc.mode}'")


def _parse_solution(wb, inst: Instance, issues: List[str]):
    plan = Plan(f"{inst.name} reported CPLEX solution")
    reported = {}
    if "Sheet2" not in wb.sheetnames:
        return plan, reported
    rows = _rows(wb["Sheet2"])
    # shipment columns: header cells s1..sN in the first two rows
    s_cols = {}
    for r in rows[:2]:
        for j, c in enumerate(r):
            if isinstance(c, str) and re.fullmatch(r"[sS]\d+", c.strip()):
                s_cols[j] = "S" + c.strip()[1:]
    lanes_by_ij = {(t.i, t.j): t for t in inst.trucks}
    bad_keys = set()
    for r in rows:
        if not (isinstance(r[0], str) and KEY.match(r[0])):
            continue
        i, j, mode, num = KEY.match(r[0]).groups()
        num = int(num)
        for cj, sid in s_cols.items():
            q = _f(r[cj]) if cj < len(r) else None
            if not q or q < 1e-6:
                continue
            if mode == "truck":
                lane = lanes_by_ij.get((i, j))
                if lane is None:
                    bad_keys.add(r[0]); continue
                plan.trucks.append(TruckMove(lane=lane.id, shipment=sid, teu=q, dep=0.0))
            else:
                svc = next((s for s in inst.services if s.id == f"v{num}"), None)
                k = next((k for k, l in enumerate(svc.legs) if (l.i, l.j) == (i, j)), None) if svc else None
                if k is None:
                    bad_keys.add(r[0]); continue
                plan.add_lcs(svc.id, k, sid, q)
    if bad_keys:
        issues.append(f"Sheet2 flow rows not matching any Sheet1 leg/lane (ignored): {sorted(bad_keys)}")
    n_sol_ship = len(set(s_cols.values()))
    if n_sol_ship and n_sol_ship != len(inst.shipments):
        issues.append(f"Sheet2 has {n_sol_ship} shipment columns vs {len(inst.shipments)} shipments in instance")
    names = {"variable": ("variable cost", "vairable cost", "varialbe cost"),
             "fixed": ("fixed cost", "fixec cost"), "transshipment": ("transshipment cost",),
             "early": ("early penalty", "early peanlty"), "late": ("late penalty",), "total": ("total cost",)}
    for r in rows:
        for j, c in enumerate(r):
            if not isinstance(c, str):
                continue
            for key, alts in names.items():
                if c.strip().lower() in alts and j + 1 < len(r) and _f(r[j + 1]) is not None:
                    reported[key] = _f(r[j + 1])
    return plan, reported
