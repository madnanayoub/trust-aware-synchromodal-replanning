"""Step 2: build clean JSON instances + reference plans, and audit the 4TU reported solutions.

Usage:  python -m scripts.build_instances      (from the synchro_bench folder)
"""
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp.evaluate import evaluate
from stp.load_4tu import load_workbook
from stp import rotterdam

RAW = os.environ.get("STP_RAW", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_raw"))
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

ORDER = ["7nodes", "8nodes", "9nodes", "10nodes", "10nodes_6S", "10nodes_7S", "10nodes_8S", "10nodes_9S",
         "10nodes_10S", "10nodes_15S", "line_ntw", "ring_ntw", "star_ntw", "tree_ntw", "fully_connected"]


def main():
    os.makedirs(f"{OUT}/instances", exist_ok=True)
    os.makedirs(f"{OUT}/reference_solutions", exist_ok=True)
    audit = {"rotterdam": {}, "4tu": {}}

    # ---------------- Rotterdam case
    inst = rotterdam.build_instance()
    inst.save(f"{OUT}/instances/rotterdam.json")
    base = rotterdam.base_plan()
    base.save(f"{OUT}/reference_solutions/rotterdam_base.json")
    ev = evaluate(inst, base)
    audit["rotterdam"]["base"] = {"ours": ev.row(), "paper": {**base.meta["paper"], "total": 15961},
                                  "violations": ev.violations}
    inst9 = rotterdam.late_release(inst, {"S4": 2, "S5": 2})
    inst9.save(f"{OUT}/instances/rotterdam_S4S5_late2h.json")
    for k, p in rotterdam.table9_plans().items():
        p.save(f"{OUT}/reference_solutions/rotterdam_S4S5_late2h_{k}.json")
        ev = evaluate(inst9, p)
        audit["rotterdam"][f"table9_{k}"] = {"ours": ev.row(), "paper": p.meta["paper"],
                                             "violations": ev.violations}

    # ---------------- 4TU theoretical instances
    for name in ORDER:
        path = f"{RAW}/{name}.xlsx"
        inst, sol, rep, issues = load_workbook(path, name)
        inst.save(f"{OUT}/instances/{name}.json")
        sol.save(f"{OUT}/reference_solutions/{name}_reported.json")
        ev = evaluate(inst, sol, check_timetable=False)
        flow_viol = [v for v in ev.violations if v.startswith(("flow conservation", "capacity"))]
        audit["4tu"][name] = {
            "summary": inst.summary(),
            "issues": issues,
            "reported": rep,
            "recomputed_from_reported_flows": {"variable": round(float(ev.variable), 2),
                                               "fixed_excl_cancel": round(float(ev.fixed), 2)},
            "reported_flow_violations": flow_viol,
            "reported_solution_consistent": not flow_viol,
        }
    with open(f"{OUT}/audit.json", "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=1)
    return audit


if __name__ == "__main__":
    a = main()
    for k, v in a["rotterdam"].items():
        print(f"ROTTERDAM {k:14s} ours={v['ours']['total']:>9} paper={v['paper'].get('total')}  "
              f"feasible={v['ours']['feasible']}")
    for k, v in a["4tu"].items():
        r = v["reported"]
        print(f"{k:16s} {v['summary'].split(': ')[1]}")
        print(f"   reported var={r.get('variable')} fixed={r.get('fixed')} total={r.get('total')} | "
              f"recomputed var={v['recomputed_from_reported_flows']['variable']} "
              f"fixed={v['recomputed_from_reported_flows']['fixed_excl_cancel']} | "
              f"consistent={v['reported_solution_consistent']} ({len(v['reported_flow_violations'])} flow viol.)")
        for i in v["issues"]:
            print("   -", i)
