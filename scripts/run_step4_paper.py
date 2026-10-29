"""Step 4a: replay the disturbance scenarios of Qu et al. (2019) Tables 5-8 with STP, N-STP, R-STP.

    python -m scripts.run_step4_paper --backend cplex
Writes results/step4_paper_scenarios.json (our KPIs next to the paper's for every row).
"""
import argparse
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.evaluate import evaluate
from stp.kpi import kpis, COLS
from stp.model import STPModel, Options
from stp.scenarios import apply_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--variants", nargs="*", default=["STP", "N-STP", "R-STP"])
    ap.add_argument("--table", type=int, nargs="*", default=[5, 6, 7, 8])
    args = ap.parse_args()
    paper = json.load(open(f"{ROOT}/data/paper_tables_5_8.json", encoding="utf-8"))
    base_inst, base = rotterdam.build_instance(), rotterdam.base_plan()
    ev_base = evaluate(base_inst, base)
    out = []
    for row in paper["rows"]:
        if row["table"] not in args.table:
            continue
        inst, unavailable = apply_events(base_inst, row["events"])
        rec = {"id": row["id"], "table": row["table"], "events": row["events"], "paper": row["paper"],
               "paper_consistent": row["paper_consistent_trucks_vs_Cf"], "ours": {}}
        for v in args.variants:
            t0 = time.time()
            r = STPModel(inst, Options(variant=v, backend=args.backend, anchor_plan=base, allow_unserved=True,
                                       unavailable_lcs=unavailable)).solve()
            if r.plan is None:
                rec["ours"][v] = {"status": r.status}
                continue
            ev = evaluate(inst, r.plan)
            k = kpis(inst, r.plan, base_inst, base, ev, ev_base)
            k.update({"status": r.status, "backend": r.backend, "solve_s": round(time.time() - t0, 2),
                      "feasible": ev.feasible, "violations": ev.violations,
                      "objective": r.objective, "mip_vs_eval": round(r.objective - ev.total, 4)})
            rec["ours"][v] = k
        out.append(rec)
        s = rec["ours"].get("STP", {})
        print(f"{row['id']:32s} STP cost={s.get('total_cost')} reroute={s.get('reroute_TEU')}/{row['paper']['reroute_TEU']} "
              f"resched={s.get('resched_vh')}/{row['paper']['resched_vh']} dCf={s.get('dCf_pct')}/{row['paper']['dCf_pct']} "
              f"trucks={s.get('extra_trucks')}/{row['paper']['extra_trucks']} "
              f"dE={s.get('dEarly_TEUh')}/{row['paper']['dEarly_TEUh']} dL={s.get('dLate_TEUh')}/{row['paper']['dLate_TEUh']}"
              + ("" if s.get("feasible", True) else "  INFEASIBLE-EVAL"), flush=True)
    os.makedirs(f"{ROOT}/results", exist_ok=True)
    json.dump(out, open(f"{ROOT}/results/step4_paper_scenarios.json", "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
