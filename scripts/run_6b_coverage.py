"""Step 6b: sensitivity of the trust-aware approach to SENSOR COVERAGE (share of events with an independent record).

The saved test days (seeds 44/55/66) are transformed deterministically: true events, reports and stakeholders stay
identical; only the evidence set changes. For every true event and every false alarm a fixed pseudo-random number
u in [0,1) is derived from (seed, day, kind, target); the event has a sensor record iff u < coverage. Coverage
levels are therefore nested (the 25 % records are a subset of the 50 % records). Records are created from the truth
with the generator's noise model (delays +/-0.25 h or exact, volumes exact; false alarms -> magnitude 0).
Baselines (oracle / trust-all / ignore) do not use evidence and are unchanged.

    python -m scripts.run_6b_coverage --backend cplex --workers 2
"""
import argparse
import copy
import glob
import hashlib
import json
import os
import sys
import time
from multiprocessing import Pool

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
SOURCE = {"release": "terminal_gate", "volume": "weighbridge"}


def _u(*parts):
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:12], 16) / 16 ** 12


def with_coverage(stream: dict, coverage: float, services_mode: dict) -> dict:
    s = copy.deepcopy(stream)
    seed = s["seed"]
    for d in s["days"]:
        ev = []
        truth = {(k, t): m for k, t, m in d["true_events"]}
        false_alarms = {(r["kind"], r["target"]) for r in d["reports"] if (r["kind"], r["target"]) not in truth}
        for (k, t), m in list(truth.items()) + [(fa, 0.0) for fa in sorted(false_alarms)]:
            if _u(seed, d["day"], k, t, "cover") >= coverage:
                continue
            src = SOURCE.get(k) or ("AIS" if services_mode[t] == "barge" else "rail_signalling")
            if k == "volume" or m == 0.0:
                mag = m
            else:
                r = _u(seed, d["day"], k, t, "noise")
                mag = max(0.0, m + (-0.25 if r < 0.25 else (0.25 if r < 0.5 else 0.0)))
            ev.append({"id": f"d{d['day']}_c{len(ev) + 1}", "day": d["day"], "source": src, "kind": k,
                       "target": t, "magnitude": mag})
        d["evidence"] = ev
    return s


def job(args):
    path, coverage, backend = args
    from stp import rotterdam
    from stp.ours import TrustAwarePlanner, Config
    B, P = rotterdam.build_instance(), rotterdam.base_plan()
    modes = {v.id: v.mode for v in B.services}
    stream = with_coverage(json.load(open(path, encoding="utf-8")), coverage, modes)
    T = TrustAwarePlanner(B, P, stream["stakeholders"], Config(backend=backend))
    t0 = time.time()
    days = [T.run_day(d)["realized_cost"] for d in stream["days"]]
    n_ev = sum(len(d["evidence"]) for d in stream["days"])
    n_events = sum(len(d["true_events"]) for d in stream["days"])
    return {"rho": stream["dishonesty"], "seed": stream["seed"], "coverage": coverage, "costs": days,
            "evidence_records": n_ev, "true_events": n_events, "seconds": round(time.time() - t0, 1)}


def summarize(runs):
    import statistics
    base = {(r["rho"], r["seed"], r["day"]): r for r in
            json.load(open(f"{ROOT}/results/step4_unreliable_baselines_test.json", encoding="utf-8"))}
    out = {}
    levels = sorted({r["coverage"] for r in runs})
    rhos = sorted({r["rho"] for r in runs})
    print("value of information captured by OURS, by sensor coverage (rows) and dishonesty (columns); "
          "trust-all for reference")
    print("coverage " + " ".join(f"{r:>7.2f}" for r in rhos) + "      all")
    for cov in levels + ["trust_all"]:
        row = {}
        for rho in rhos + ["all"]:
            sel = [r for r in runs if r["coverage"] == (levels[0] if cov == "trust_all" else cov)
                   and (rho == "all" or r["rho"] == rho)]
            keys = [(r["rho"], r["seed"], d) for r in sel for d in range(len(r["costs"]))]
            o = statistics.fmean(base[k]["oracle"]["realized_cost"] for k in keys)
            i = statistics.fmean(base[k]["ignore"]["realized_cost"] for k in keys)
            if cov == "trust_all":
                p = statistics.fmean(base[k]["trust_all"]["realized_cost"] for k in keys)
            else:
                cost = {(r["rho"], r["seed"], d): c for r in sel for d, c in enumerate(r["costs"])}
                p = statistics.fmean(cost[k] for k in keys)
            row[str(rho)] = round(100 * (i - p) / (i - o), 1)
        out[str(cov)] = row
        print(f"{str(cov):>8} " + " ".join(f"{row[str(r)]:>6}%" for r in rhos) + f"   {row['all']:>5}%")
    actual = {}
    for cov in levels:
        sel = [r for r in runs if r["coverage"] == cov]
        actual[str(cov)] = round(sum(r["evidence_records"] for r in sel) / max(1, sum(r["true_events"] for r in sel)), 3)
    json.dump({"voi_captured_pct": out, "evidence_records_per_true_event": actual},
              open(f"{ROOT}/results/step6b_coverage_summary.json", "w", encoding="utf-8"), indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--levels", type=float, nargs="*", default=LEVELS)
    ap.add_argument("--seeds", type=int, nargs="*", default=[44, 55, 66])
    a = ap.parse_args()
    streams = sorted(f for f in glob.glob(f"{ROOT}/data/unreliable/stream_*.json")
                     if int(f.rsplit("seed", 1)[1].split(".")[0]) in a.seeds)
    jobs = [(s, c, a.backend) for c in a.levels for s in streams]
    out, t0 = [], time.time()
    pool = Pool(a.workers) if a.workers > 1 else None
    for r in (pool.imap_unordered(job, jobs) if pool else map(job, jobs)):
        out.append(r)
        print(f"coverage={r['coverage']:.2f} rho={r['rho']:.2f} seed={r['seed']} ({r['seconds']}s, "
              f"total {time.time() - t0:.0f}s)", flush=True)
        json.dump(out, open(f"{ROOT}/results/step6b_coverage_runs.json", "w", encoding="utf-8"))
    if pool:
        pool.close()
    summarize(out)


if __name__ == "__main__":
    main()
