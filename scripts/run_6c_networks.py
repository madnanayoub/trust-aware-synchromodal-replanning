"""Step 6c: the unreliable-information experiment on larger 4TU networks.

    python -m scripts.run_6c_networks --backend cplex --workers 2
    python -m scripts.run_6c_networks --cases 10nodes_10S --seeds 77 --levels 1.0 --days 10   # quick check

For every case: build the replanning case (stp/cases.py), generate fresh streams (seeds 77/88/99 by default,
never used before), and run on identical days
  oracle, trust_all, ignore           (step-4 baselines)
  ours, no_evidence, no_reputation    (step-5 approach and its two main ablations)
Writes data/unreliable_<case>/stream_*.json and results/step6c_<case>.json; prints value of information captured.
"""
import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict
from multiprocessing import Pool

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OURS = {"ours": {}, "no_evidence": {"use_evidence": False}, "no_reputation": {"use_reputation": False}}
BASE = ["oracle", "trust_all", "ignore"]


def reported(day: dict):
    b = {}
    for r in sorted(day["reports"], key=lambda r: r["sent_at"]):
        b[(r["kind"], r["target"])] = r["magnitude"]
    return [[k, t, m] for (k, t), m in b.items()]


def job(args):
    case_name, path, variants, backend = args
    from stp.cases import make_replanning_case
    from stp.execution import execute
    from stp.ours import TrustAwarePlanner, Config
    case, p0 = make_replanning_case(case_name, backend=backend)
    stream = json.load(open(path, encoding="utf-8"))
    t0 = time.time()
    days = []
    for d in stream["days"]:
        row = {"day": d["day"], "n_events": len(d["true_events"]), "n_reports": len(d["reports"])}
        for pol, bel in (("oracle", d["true_events"]), ("trust_all", reported(d)), ("ignore", [])):
            row[pol] = execute(case, p0, d["true_events"], bel, pol, backend=backend).realized_cost
        days.append(row)
    extra = {}
    for v in variants:
        T = TrustAwarePlanner(case, p0, stream["stakeholders"], Config(backend=backend, **OURS[v]))
        faithful = 0
        for row, d in zip(days, stream["days"]):
            r = T.run_day(d)
            row[v] = r["realized_cost"]
            faithful += r["explanation_check"]["faithful"]
        extra[v] = {"ledger": T.ledger.stats(), "faithful": faithful, "reputation": T.contract.snapshot()}
    return {"case": case_name, "rho": stream["dishonesty"], "seed": stream["seed"],
            "profiles": {s["id"]: s["profile"] for s in stream["stakeholders"]}, "days": days, "extra": extra,
            "seconds": round(time.time() - t0, 1)}


def summarize(runs, case):
    from scipy.stats import wilcoxon
    pols = BASE + [v for v in OURS if v in runs[0]["days"][0]]
    out = {}
    print(f"\n{case}: value of information captured")
    print("   rho " + " ".join(f"{p:>14}" for p in pols) + "   ours vs trust_all (better/worse, p)")
    for rho in sorted({r["rho"] for r in runs}) + ["all"]:
        rows = [d for r in runs if rho == "all" or r["rho"] == rho for d in r["days"]]
        m = {p: statistics.fmean(x[p] for x in rows) for p in pols}
        voi = {p: round(100 * (m["ignore"] - m[p]) / (m["ignore"] - m["oracle"]), 1) for p in pols}
        diff = [x["ours"] - x["trust_all"] for x in rows]
        nz = [x for x in diff if abs(x) > 1e-6]
        pv = float(f"{wilcoxon(nz).pvalue:.2e}") if len(nz) > 5 else None
        out[str(rho)] = {"days": len(rows), "voi_captured_pct": voi, "mean_cost": {p: round(v, 1) for p, v in m.items()},
                         "regret": {p: round(m[p] - m["oracle"], 1) for p in pols},
                         "ours_better_days": sum(x < -0.5 for x in diff), "ours_worse_days": sum(x > 0.5 for x in diff),
                         "wilcoxon_p": pv}
        print(f"{str(rho):>6} " + " ".join(f"{voi[p]:>13}%" for p in pols)
              + f"   +{out[str(rho)]['ours_better_days']}/-{out[str(rho)]['ours_worse_days']} p={pv}")
    return out


def robustness(runs, threshold=10000.0):
    """Catastrophic days (regret > threshold, i.e. freight left unserved) per policy, and mean / median regret on the
    days on which no policy was catastrophic."""
    days = [d for r in runs for d in r["days"]]
    pols = BASE + [v for v in OURS if v in days[0]]
    cat = {p: {i for i, d in enumerate(days) if d[p] - d["oracle"] > threshold} for p in pols}
    union = set().union(*cat.values())
    kept = [d for i, d in enumerate(days) if i not in union]
    return {"catastrophic_days_(regret>10k)": {p: len(v) for p, v in cat.items()},
            "mean_regret_excluding_those_days": {p: round(statistics.fmean(d[p] - d["oracle"] for d in kept), 1)
                                                 for p in pols},
            "days_kept": len(kept),
            "median_regret": {p: round(statistics.median(d[p] - d["oracle"] for d in days), 1) for p in pols}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--cases", nargs="*", default=["10nodes_10S", "9nodes"])
    ap.add_argument("--seeds", type=int, nargs="*", default=[77, 88, 99])
    ap.add_argument("--levels", type=float, nargs="*", default=[0.0, 0.25, 0.5, 0.75, 1.0])
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--variants", nargs="*", default=list(OURS))
    a = ap.parse_args()
    from stp.cases import make_replanning_case
    from stp.uncertainty import generate_stream
    summary = {}
    for case_name in a.cases:
        case, p0 = make_replanning_case(case_name, backend=a.backend)
        d_out = f"{ROOT}/data/unreliable_{case_name}"
        os.makedirs(d_out, exist_ok=True)
        jobs = []
        for rho in a.levels:
            for seed in a.seeds:
                sh, days = generate_stream(case, a.days, rho, seed)
                path = f"{d_out}/stream_rho{rho:.2f}_seed{seed}.json"
                json.dump({"case": case_name, "dishonesty": rho, "seed": seed,
                           "stakeholders": [asdict(s) for s in sh], "days": [d.to_dict() for d in days]},
                          open(path, "w", encoding="utf-8"), indent=1)
                jobs.append((case_name, path, a.variants, a.backend))
        runs, t0 = [], time.time()
        pool = Pool(a.workers) if a.workers > 1 else None
        for r in (pool.imap_unordered(job, jobs) if pool else map(job, jobs)):
            runs.append(r)
            print(f"{case_name} rho={r['rho']:.2f} seed={r['seed']} done ({r['seconds']}s, total "
                  f"{time.time() - t0:.0f}s)", flush=True)
            json.dump(runs, open(f"{ROOT}/results/step6c_{case_name}.json", "w", encoding="utf-8"))
        if pool:
            pool.close()
        summary[case_name] = {"base_cost": p0.meta["cost"], "shipments": len(case.shipments),
                              "TEU": sum(s.n for s in case.shipments), "terminals": len(case.terminals),
                              "services": len(case.services), "stakeholders": len(runs[0]["profiles"]),
                              "by_rho": summarize(runs, case_name), "robustness": robustness(runs)}
    prev = {}
    p = f"{ROOT}/results/step6c_summary.json"
    if os.path.exists(p):
        prev = json.load(open(p, encoding="utf-8"))
    prev.update(summary)
    json.dump(prev, open(p, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
