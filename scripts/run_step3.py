"""Step 3: solve STP / N-STP / R-STP on the Rotterdam case and all 4TU instances.

    python -m scripts.run_step3 --backend cplex --time-limit 600      # on a machine with full CPLEX
    python -m scripts.run_step3 --backend highs                        # open-source fallback

Writes results/step3_results.json and results/plans/*.json; every plan is re-scored by the
independent evaluator and the MIP objective must match it.
"""
import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp.evaluate import evaluate
from stp.instance import Instance
from stp.model import STPModel, Options

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TU = ["7nodes", "8nodes", "9nodes", "10nodes", "10nodes_6S", "10nodes_7S", "10nodes_8S", "10nodes_9S",
      "10nodes_10S", "10nodes_15S", "line_ntw", "ring_ntw", "star_ntw", "tree_ntw", "fully_connected"]
PAPER_T9 = {"STP": 17261.8, "N-STP": 20043, "R-STP": 19078}


def run(inst, variant, args, soft):
    t0 = time.time()
    M = STPModel(inst, Options(variant=variant, time_limit=args.time_limit, backend=args.backend,
                               allow_unserved=soft, threads=args.threads, mip_gap=args.gap))
    build = time.time() - t0
    r = M.solve()
    row = {"instance": inst.name, "variant": variant, "backend": r.backend, "status": r.status,
           "size": r.size, "build_s": round(build, 2), "solve_s": round(r.seconds, 2),
           "objective": r.objective, "bound": r.bound, "gap": r.gap, "soft_demand": soft}
    if r.plan:
        ev = evaluate(inst, r.plan)
        row["eval"] = ev.row()
        row["eval_violations"] = ev.violations
        row["mip_vs_eval"] = round(r.objective - ev.total, 4)
        r.plan.save(f"{ROOT}/results/plans/{inst.name}_{variant}.json")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto", choices=["auto", "cplex", "highs"])
    ap.add_argument("--time-limit", type=float, default=300)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--gap", type=float, default=0.0)
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()
    os.makedirs(f"{ROOT}/results/plans", exist_ok=True)
    rows = []
    jobs = [("rotterdam", ["STP"], False), ("rotterdam_S4S5_late2h", ["STP", "N-STP", "R-STP"], False)]
    jobs += [(n, ["STP", "N-STP"], True) for n in TU]
    for name, variants, soft in jobs:
        if args.only and name not in args.only:
            continue
        inst = Instance.load(f"{ROOT}/data/instances/{name}.json")
        for v in variants:
            row = run(inst, v, args, soft)
            if name == "rotterdam_S4S5_late2h":
                row["paper"] = PAPER_T9[v]
            rows.append(row)
            e = row.get("eval", {})
            print(f"{name:22s} {v:6s} {row['backend']:5s} {row['status'][:22]:22s} "
                  f"vars={row['size']['vars']:5d} cons={row['size']['constraints']:5d} "
                  f"obj={row['objective'] if row['objective'] is None else round(row['objective'], 1)} "
                  f"gap={row['gap']} t={row['solve_s']}s unserved={e.get('unserved_TEU')} "
                  f"feasible={e.get('feasible')} mip-eval={row.get('mip_vs_eval')}", flush=True)
            with open(f"{ROOT}/results/step3_results.json", "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
