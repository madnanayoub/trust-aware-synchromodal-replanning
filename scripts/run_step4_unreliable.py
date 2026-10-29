"""Step 4b: unreliable-information benchmark - baselines.

    python -m scripts.run_step4_unreliable --backend cplex
Generates the scenario streams (data/unreliable/*.json, reused by step 5) and evaluates
  oracle      plans on the true events (the paper's 'full deterministic information' assumption holds)
  trust_all   plans on every stakeholder report as-is (the paper's model fed with real-world messages)
  ignore      plans on no reports; reacts only when a disturbance becomes observable
"""
import argparse
import json
import os
import sys
import time
from dataclasses import asdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.execution import execute
from stp.uncertainty import generate_stream, reported_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--seeds", type=int, nargs="*", default=[11, 22, 33])
    ap.add_argument("--levels", type=float, nargs="*", default=LEVELS)
    ap.add_argument("--out", default="results/step4_unreliable_baselines.json")
    args = ap.parse_args()
    base, bplan = rotterdam.build_instance(), rotterdam.base_plan()
    os.makedirs(f"{ROOT}/data/unreliable", exist_ok=True)
    os.makedirs(f"{ROOT}/results", exist_ok=True)
    rows = []
    t0 = time.time()
    for rho in args.levels:
        for seed in args.seeds:
            stakeholders, days = generate_stream(base, args.days, rho, seed)
            json.dump({"dishonesty": rho, "seed": seed, "stakeholders": [asdict(s) for s in stakeholders],
                       "days": [d.to_dict() for d in days]},
                      open(f"{ROOT}/data/unreliable/stream_rho{rho:.2f}_seed{seed}.json", "w", encoding="utf-8"),
                      indent=1)
            for d in days:
                beliefs = {"oracle": d.true_events, "trust_all": reported_events(d), "ignore": []}
                rec = {"rho": rho, "seed": seed, "day": d.day, "true_events": d.true_events,
                       "n_reports": len(d.reports),
                       "report_types": [r.report_type for r in d.reports], "n_omitted": len(d.omitted)}
                for pol, bel in beliefs.items():
                    o = execute(base, bplan, d.true_events, bel, pol, backend=args.backend)
                    rec[pol] = asdict(o)
                rows.append(rec)
            print(f"rho={rho:.2f} seed={seed} done ({time.time() - t0:.0f}s)", flush=True)
            json.dump(rows, open(os.path.join(ROOT, args.out), "w", encoding="utf-8"))
    summarize(rows)


def summarize(rows):
    pols = ["oracle", "trust_all", "ignore"]
    print(f"\n{'rho':>5} {'days':>5} " + " ".join(f"{p + ' cost':>16}" for p in pols)
          + f" {'trust regret':>13} {'ignore regret':>14} {'trust>ignore':>13}")
    for rho in sorted({r["rho"] for r in rows}):
        rs = [r for r in rows if r["rho"] == rho]
        mean = lambda p, k="realized_cost": sum(r[p][k] for r in rs) / len(rs)
        reg_t = mean("trust_all") - mean("oracle")
        reg_i = mean("ignore") - mean("oracle")
        worse = sum(r["trust_all"]["realized_cost"] > r["ignore"]["realized_cost"] + 0.5 for r in rs)
        print(f"{rho:5.2f} {len(rs):5d} " + " ".join(f"{mean(p):16.1f}" for p in pols)
              + f" {reg_t:13.1f} {reg_i:14.1f} {worse:>8d}/{len(rs)}")


if __name__ == "__main__":
    main()
