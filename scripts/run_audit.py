"""Additional experiments: fair baselines, more independent runs, sensor imperfection, simulator mismatch,
parameter sensitivity and role-specific behaviour (Rotterdam case).

    python -m scripts.run_audit --backend cplex --workers 2                 # all groups
    python -m scripts.run_audit --groups main10 --backend cplex             # one group
    python -m scripts.run_audit --backend cplex --workers 6                 # 6 parallel runs, 1 solver thread each

Groups
  main10     10 seeds x 5 dishonesty levels x 40 days. Policies: oracle, trust-all, ignore, sensor-only (acts only on
             sensor records), evidence-first trust-all (sensor records decide, every other report is trusted), the
             full framework, its ablations (no evidence, no lookahead) and the history-rewriting attack.
  role       as main10 (baselines, framework, evidence-first), but strategic carriers hide or understate their
             delays (profile_set="role").
  sensors    test seeds 44/55/66: sensor records wrong with probability 10% / 20% (wrong magnitude or false
             refutation / false confirmation), or delayed past the planning time with probability 25% / 50%.
  lookahead  test seeds: the lookahead plans with a mis-specified spot premium (25% or 100%; true 50%).
  params     test seeds: trust threshold 0.75 / 0.95, Beta prior (1,1) / (6,2), environment spot premium 25% / 100%.
Results: results/audit/<group>.json (one record per run = stream x policy x setting, with per-day costs and KPIs).
"""
import argparse
import copy
import hashlib
import json
import os
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
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
SEEDS10 = [44, 55, 66, 101, 102, 103, 104, 105, 106, 107]
SEEDS3 = [44, 55, 66]
OURS = {"ours": {}, "no_evidence": {"use_evidence": False}, "evidence_first": {"use_reputation": False},
        "no_lookahead": {"lookahead": False}, "attack_ledger": {"tamper_attack": True, "tamper_proof": True},
        "attack_mutable": {"tamper_attack": True, "tamper_proof": False}}
BASE = ["oracle", "trust_all", "ignore", "sensor_only"]


# ------------------------------------------------------------------------------------------------ streams
def stream_path(rho, seed, profile_set="default"):
    d = "unreliable" if profile_set == "default" else f"unreliable_{profile_set}"
    return f"{ROOT}/data/{d}/stream_rho{rho:.2f}_seed{seed}.json"


def ensure_stream(rho, seed, profile_set="default", days=40):
    p = stream_path(rho, seed, profile_set)
    if not os.path.exists(p):
        from stp import rotterdam
        from stp.uncertainty import generate_stream
        os.makedirs(os.path.dirname(p), exist_ok=True)
        sh, ds = generate_stream(rotterdam.build_instance(), days, rho, seed, profile_set)
        json.dump({"dishonesty": rho, "seed": seed, "profile_set": profile_set,
                   "stakeholders": [asdict(s) for s in sh], "days": [d.to_dict() for d in ds]},
                  open(p, "w", encoding="utf-8"), indent=1)
    return p


def _u(*parts):
    """Deterministic uniform number in [0, 1) from the given identifiers."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:12], 16) / 16 ** 12


def transform(stream, noise=0.0, latency=0.0):
    """Imperfect sensors. noise: each record is wrong with this probability (true event: wrong magnitude or a false
    'no change' record, half each; false-alarm check: the record confirms the claimed delay). latency: each record
    arrives after the 7:00 planning time with this probability and is not available to the planner that day."""
    if not noise and not latency:
        return stream
    s = copy.deepcopy(stream)
    for d in s["days"]:
        claimed = {(r["kind"], r["target"]): r["magnitude"] for r in d["reports"]}
        keep = []
        for e in d["evidence"]:
            key = (s["seed"], s["dishonesty"], d["day"], e["kind"], e["target"])
            if latency and _u("lat", *key) < latency:
                continue
            if noise and _u("noise", *key) < noise:
                m = e["magnitude"]
                if abs(m) > 1e-9:
                    if _u("how", *key) < 0.5:
                        e["magnitude"] = 0.0                                   # false refutation
                    elif e["kind"] == "volume":
                        e["magnitude"] = round(m + (0.1 if _u("sgn", *key) < 0.5 else -0.1), 2)
                    else:
                        e["magnitude"] = max(0.5, m + (1.0 if _u("sgn", *key) < 0.5 else -1.0))
                else:
                    e["magnitude"] = claimed.get((e["kind"], e["target"]), 0.5)  # false confirmation
            keep.append(e)
        d["evidence"] = keep
    return s


def reported(day):
    b = {}
    for r in sorted(day["reports"], key=lambda r: r["sent_at"]):
        b[(r["kind"], r["target"])] = r["magnitude"]
    return [[k, t, m] for (k, t), m in b.items()]


def sensor_belief(day):
    out = []
    for e in day["evidence"]:
        m = e["magnitude"]
        if abs(m) > 1e-9:
            out.append([e["kind"], e["target"], m if e["kind"] == "volume" else max(0.5, round(m * 2) / 2)])
    return out


def _acc(belief, truth):
    tk = {(k, t) for k, t, _ in truth}
    bk = {(k, t) for k, t, _ in belief}
    return len(bk & tk), len(bk - tk)


# ------------------------------------------------------------------------------------------------ one run
def job(a):
    group, tag, path, policy, cfg, noise, latency, backend = a
    from stp import rotterdam
    from stp.execution import execute
    from stp.ours import TrustAwarePlanner, Config
    B, P = rotterdam.build_instance(), rotterdam.base_plan()
    stream = transform(json.load(open(path, encoding="utf-8")), noise, latency)
    t0 = time.time()
    rows = []
    prem = cfg.get("premium", 0.5)
    if policy in BASE:
        for d in stream["days"]:
            bel = {"oracle": d["true_events"], "trust_all": reported(d), "ignore": [],
                   "sensor_only": sensor_belief(d)}[policy]
            o = execute(B, P, d["true_events"], bel, policy, backend=backend, premium=prem)
            tp, fp = _acc(bel, d["true_events"])
            rows.append({"day": d["day"], "cost": o.realized_cost, "spot": o.spot_trucks, "unused": o.unused_booked,
                         "unserved": o.unserved_teu, "late": o.late_teuh, "churn": o.churn_teu,
                         "acted_true": tp, "acted_false": fp, "n_true": len(d["true_events"])})
        extra = {}
    else:
        T = TrustAwarePlanner(B, P, stream["stakeholders"], Config(backend=backend, **{**OURS[policy], **cfg}))
        for d in stream["days"]:
            r = T.run_day(d)
            o = r["outcome"]
            tp, fp = _acc(r["belief"], d["true_events"])
            rows.append({"day": d["day"], "cost": r["realized_cost"], "spot": o["spot_trucks"],
                         "unused": o["unused_booked"], "unserved": o["unserved_teu"], "late": o["late_teuh"],
                         "churn": o["churn_teu"], "acted_true": tp, "acted_false": fp,
                         "n_true": len(d["true_events"]), "n_refuted": r["n_refuted"], "n_rejected": r["n_rejected"],
                         "n_not_understood": r["n_not_understood"], "n_certain": r["n_certain"],
                         "n_accepted": r["n_accepted"], "faithful": r["explanation_check"]["faithful"]})
        extra = {"reputation": T.contract.snapshot(), "ledger": T.ledger.stats(), "tamper_detected": T.tamper_detected}
    return {"group": group, "tag": tag, "rho": stream["dishonesty"], "seed": stream["seed"], "policy": policy,
            "cfg": cfg, "noise": noise, "latency": latency,
            "profiles": {s["id"]: s["profile"] for s in stream["stakeholders"]},
            "seconds": round(time.time() - t0, 1), "days": rows, **extra}


def plan_jobs(groups, backend):
    J = []
    for g in groups:
        if g in ("main10", "role"):
            ps = "default" if g == "main10" else "role"
            pols = (BASE + list(OURS)) if g == "main10" else ["oracle", "trust_all", "ignore", "ours", "evidence_first"]
            for rho in LEVELS:
                for seed in (SEEDS10 if g == "main10" else SEEDS3):
                    p = ensure_stream(rho, seed, ps)
                    J += [(g, "base", p, pol, {}, 0.0, 0.0, backend) for pol in pols]
        elif g == "sensors":
            for tag, nz, lt in (("noise10", 0.1, 0.0), ("noise20", 0.2, 0.0), ("latency25", 0.0, 0.25),
                                ("latency50", 0.0, 0.5)):
                for rho in LEVELS:
                    for seed in SEEDS3:
                        p = ensure_stream(rho, seed)
                        J += [(g, tag, p, pol, {}, nz, lt, backend) for pol in ("sensor_only", "evidence_first", "ours")]
        elif g == "lookahead":
            for tag, lp in (("la_premium25", 0.25), ("la_premium100", 1.0)):
                for rho in LEVELS:
                    for seed in SEEDS3:
                        J.append((g, tag, ensure_stream(rho, seed), "ours", {"la_premium": lp}, 0.0, 0.0, backend))
        elif g == "params":
            sets = [("trusted075", {"trusted_p": 0.75}, ["ours"]), ("trusted095", {"trusted_p": 0.95}, ["ours"]),
                    ("prior11", {"prior": [1.0, 1.0]}, ["ours"]), ("prior62", {"prior": [6.0, 2.0]}, ["ours"]),
                    ("premium25", {"premium": 0.25}, ["oracle", "trust_all", "ignore", "evidence_first", "ours"]),
                    ("premium100", {"premium": 1.0}, ["oracle", "trust_all", "ignore", "evidence_first", "ours"])]
            for tag, cfg, pols in sets:
                for rho in LEVELS:
                    for seed in SEEDS3:
                        p = ensure_stream(rho, seed)
                        J += [(g, tag, p, pol, cfg, 0.0, 0.0, backend) for pol in pols]
    return J


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--groups", nargs="*", default=["main10", "role", "sensors", "lookahead", "params"])
    ap.add_argument("--threads", type=int, default=None,
                    help="solver threads per run (default: 1 when --workers > 1, else solver default)")
    a = ap.parse_args()
    # one solver thread per worker avoids oversubscribing the CPU when runs are parallel
    os.environ["STP_THREADS"] = str(a.threads if a.threads is not None else (1 if a.workers > 1 else 0))
    os.makedirs(f"{ROOT}/results/audit", exist_ok=True)
    for g in a.groups:
        out_p = f"{ROOT}/results/audit/{g}.json"
        done = json.load(open(out_p, encoding="utf-8")) if os.path.exists(out_p) else []
        key = lambda r: (r["tag"], r["rho"], r["seed"], r["policy"])
        have = {key(r) for r in done}
        jobs = [j for j in plan_jobs([g], a.backend)
                if (j[1], json.load(open(j[2], encoding="utf-8"))["dishonesty"],
                    json.load(open(j[2], encoding="utf-8"))["seed"], j[3]) not in have]
        # slow policies first so the pool stays busy
        slow = {"no_evidence": 0, "attack_ledger": 1, "attack_mutable": 1, "ours": 2}
        jobs.sort(key=lambda j: slow.get(j[3], 9))
        print(f"{g}: {len(jobs)} runs to do ({len(done)} already done)", flush=True)
        t0 = time.time()
        pool = Pool(a.workers) if a.workers > 1 else None
        for i, r in enumerate(pool.imap_unordered(job, jobs) if pool else map(job, jobs), 1):
            done.append(r)
            if i % 5 == 0 or i == len(jobs):
                json.dump(done, open(out_p, "w", encoding="utf-8"))
            print(f"  [{i}/{len(jobs)}] {g} {r['tag']} {r['policy']:15s} rho={r['rho']:.2f} seed={r['seed']} "
                  f"{sum(d['cost'] for d in r['days']):,.1f} ({r['seconds']}s, total {time.time() - t0:.0f}s)",
                  flush=True)
        json.dump(done, open(out_p, "w", encoding="utf-8"))
        if pool:
            pool.close()


if __name__ == "__main__":
    main()
