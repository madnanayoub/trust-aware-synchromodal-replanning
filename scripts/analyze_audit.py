"""Run-level analysis of the additional experiments (results/audit/*.json) -> results/audit_summary.json.

    python -m scripts.analyze_audit

Inference is made at the level of runs (one run = one 40-day stream), because the days of a run share a reputation
history: per run we take the mean daily cost; per dishonesty level we report the mean over runs with a 95% bootstrap
interval over runs (10,000 resamples; for results pooled over levels the resampling unit is the seed, because runs that
share a seed are dependent) and the two-sided Wilcoxon signed-rank test on paired run means (see analyze_stats.py for the
seed-blocked pooled tests, Holm adjustment and effect sizes).
"""
import json
import os
import random
import statistics
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scipy.stats import wilcoxon  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
B = 10000


def load(g):
    p = f"{ROOT}/results/audit/{g}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


def run_means(runs):
    """{(tag, rho, seed, policy): run record with 'mean' cost and per-day list}"""
    out = {}
    for r in runs:
        r["mean"] = statistics.fmean(d["cost"] for d in r["days"])
        out[(r["tag"], r["rho"], r["seed"], r["policy"])] = r
    return out


def boot(vals, fn=statistics.fmean, seed=0, keys=None):
    """Percentile bootstrap of fn(vals); with `keys` (level, seed) the resampling unit is the seed."""
    rng = random.Random(seed)
    n = len(vals)
    xs = sorted(fn([vals[i] for i in (block_idx(keys, rng) if keys else [rng.randrange(n) for _ in range(n)])])
                for _ in range(B))
    return round(xs[int(0.025 * B)], 1), round(xs[int(0.975 * B) - 1], 1)


def wil(a, b):
    d = [x - y for x, y in zip(a, b)]
    nz = [x for x in d if abs(x) > 1e-9]
    if len(nz) < 1:
        return None
    try:
        return float(f"{wilcoxon(nz).pvalue:.3g}")
    except ValueError:
        return None


def block_idx(keys, rng):
    """Resample seeds with replacement and keep all levels of each drawn seed (runs sharing a seed are dependent)."""
    seeds = sorted({s for _, s in keys})
    by = defaultdict(list)
    for i, (_, s) in enumerate(keys):
        by[s].append(i)
    out = []
    for _ in seeds:
        out += by[seeds[rng.randrange(len(seeds))]]
    return out


def voi(ig, pol, orc):
    den = statistics.fmean(ig) - statistics.fmean(orc)
    return 100 * (statistics.fmean(ig) - statistics.fmean(pol)) / den if den else float("nan")


def voi_ci(ig, pol, orc, seed=0, keys=None):
    """Percentile bootstrap; with `keys` (level, seed) the resampling unit is the seed (block bootstrap)."""
    rng = random.Random(seed)
    n = len(ig)
    vals = []
    for _ in range(B):
        idx = block_idx(keys, rng) if keys else [rng.randrange(n) for _ in range(n)]
        v = voi([ig[i] for i in idx], [pol[i] for i in idx], [orc[i] for i in idx])
        if v == v:
            vals.append(v)
    vals.sort()
    return round(vals[int(0.025 * len(vals))], 1), round(vals[int(0.975 * len(vals)) - 1], 1)


def table(M, tag, policies, levels=LEVELS, ref_tag="base", ref=None, compare=("ours", "trust_all")):
    """Per-level and pooled run-level statistics for `policies` under setting `tag`.
    Oracle and ignore come from `ref_tag` (they do not use sensors) unless present under `tag`."""
    ref = ref or M
    out = {}
    for lev in list(levels) + ["all"]:
        lv = levels if lev == "all" else [lev]
        seeds = sorted({k[2] for k in M if k[0] == tag and k[1] in lv})
        keys = [(r, s) for r in lv for s in seeds]

        def get(pol, r, s):
            for t, src in ((tag, M), (ref_tag, ref)):
                if (t, r, s, pol) in src:
                    return src[(t, r, s, pol)]["mean"]
            return None
        orc = [get("oracle", r, s) for r, s in keys]
        ig = [get("ignore", r, s) for r, s in keys]
        ok = [i for i, (o, g) in enumerate(zip(orc, ig)) if o is not None and g is not None]
        row = {"runs": len(ok)}
        for p in policies:
            v = [get(p, r, s) for r, s in keys]
            idx = [i for i in ok if v[i] is not None]
            if not idx:
                continue
            o, g, pv = [orc[i] for i in idx], [ig[i] for i in idx], [v[i] for i in idx]
            reg = [a - b for a, b in zip(pv, o)]
            bk = [keys[i] for i in idx] if lev == "all" else None
            row[p] = {"runs": len(idx), "voi": round(voi(g, pv, o), 1), "voi_ci": voi_ci(g, pv, o, keys=bk),
                      "regret": round(statistics.fmean(reg), 1), "regret_ci": boot(reg, keys=bk)}
        a, b = compare
        if a in row and b in row:
            va = [get(a, r, s) for r, s in keys]
            vb = [get(b, r, s) for r, s in keys]
            pairs = [(x, y) for x, y in zip(va, vb) if x is not None and y is not None]
            row[f"{a}_vs_{b}"] = {"runs_better": sum(x < y - 1e-6 for x, y in pairs),
                                  "runs_worse": sum(x > y + 1e-6 for x, y in pairs),
                                  "wilcoxon_p": wil([x for x, _ in pairs], [y for _, y in pairs])}
        out[str(lev)] = row
    return out


def paired(M, tag, a, b, levels=LEVELS):
    out = {}
    for lev in list(levels) + ["all"]:
        lv = levels if lev == "all" else [lev]
        ks = sorted({(k[1], k[2]) for k in M if k[0] == tag and k[1] in lv and k[3] == a})
        pairs = [(M[(tag, r, s, a)]["mean"], M[(tag, r, s, b)]["mean"]) for r, s in ks if (tag, r, s, b) in M]
        if pairs:
            out[str(lev)] = {"runs": len(pairs), "better": sum(x < y - 1e-6 for x, y in pairs),
                             "worse": sum(x > y + 1e-6 for x, y in pairs),
                             "mean_diff": round(statistics.fmean(x - y for x, y in pairs), 1),
                             "wilcoxon_p": wil([x for x, _ in pairs], [y for _, y in pairs])}
    return out


def kpis(M, tag, policies):
    out = {}
    for p in policies:
        rows = [d for k, r in M.items() if k[0] == tag and k[3] == p for d in r["days"]]
        if not rows:
            continue
        out[p] = {f: round(statistics.fmean(d[f] for d in rows if f in d), 3)
                  for f in ("spot", "unused", "unserved", "late", "churn") if any(f in d for d in rows)}
        out[p]["days"] = len(rows)
    return out


def claims(M, tag="base"):
    out = {}
    for lev in LEVELS:
        rows = [d for k, r in M.items() if k[0] == tag and k[1] == lev and k[3] == "ours" for d in r["days"]]
        if rows and "acted_true" in rows[0]:
            out[str(lev)] = {f: sum(d[f] for d in rows) for f in ("n_true", "acted_true", "acted_false", "n_refuted",
                                                                 "n_rejected", "n_not_understood")}
            out[str(lev)]["days"] = len(rows)
    return out


def calibration(M, tag="base"):
    prof = defaultdict(list)
    for k, r in M.items():
        if k[0] == tag and k[3] == "ours" and "reputation" in r:
            for sid, v in r["reputation"].items():
                prof[r["profiles"][sid]].append(v["p_real"])
    rel = prof.get("reliable", [])
    unrel = [x for p, v in prof.items() if p != "reliable" for x in v]
    auc = None
    if rel and unrel:
        auc = sum((a > b) + 0.5 * (a == b) for a in rel for b in unrel) / (len(rel) * len(unrel))
    return {"by_profile": {p: {"n": len(v), "mean": round(statistics.fmean(v), 3),
                               "share_above_0.85": round(sum(x >= 0.85 for x in v) / len(v), 3)}
                           for p, v in prof.items()},
            "auc": round(auc, 3) if auc is not None else None}


def tamper(M):
    out = {}
    for lev in LEVELS:
        rs = [r for k, r in M.items() if k[0] == "base" and k[1] == lev and k[3] == "attack_ledger"]
        if rs:
            out[str(lev)] = {"runs": len(rs), "detected": sum(r.get("tamper_detected", 0) for r in rs),
                             "ledgers_valid": all(r.get("ledger", {}).get("valid", True) for r in rs)}
    return out


def networks():
    out = {}
    for case in ("10nodes_10S", "9nodes"):
        p = f"{ROOT}/results/step6c_{case}.json"
        if not os.path.exists(p):
            continue
        runs = json.load(open(p, encoding="utf-8"))
        rm = defaultdict(dict)
        for r in runs:
            for pol in ("oracle", "trust_all", "ignore", "ours", "no_evidence", "no_reputation"):
                rm[(r["rho"], r["seed"])][pol] = statistics.fmean(d[pol] for d in r["days"])
        res = {}
        for lev in LEVELS + ["all"]:
            ks = [k for k in rm if lev == "all" or k[0] == lev]
            row = {"runs": len(ks)}
            for pol in ("trust_all", "ignore", "ours", "no_evidence", "no_reputation"):
                reg = [rm[k][pol] - rm[k]["oracle"] for k in ks]
                row[pol] = {"regret": round(statistics.fmean(reg), 1),
                            "regret_ci": boot(reg, keys=ks if lev == "all" else None)}
            a = [rm[k]["ours"] for k in ks]
            b = [rm[k]["trust_all"] for k in ks]
            row["ours_vs_trust_all"] = {"runs_better": sum(x < y - 1e-6 for x, y in zip(a, b)),
                                        "runs_worse": sum(x > y + 1e-6 for x, y in zip(a, b)),
                                        "wilcoxon_p": wil(a, b)}
            res[str(lev)] = row
        out[case] = res
    return out


def main():
    S = {}
    main10 = run_means(load("main10"))
    pols = ["trust_all", "ignore", "sensor_only", "evidence_first", "no_evidence", "no_lookahead", "ours",
            "attack_ledger", "attack_mutable"]
    S["main10"] = table(main10, "base", pols)
    S["main10_pairs"] = {f"{a}_vs_{b}": paired(main10, "base", a, b) for a, b in
                         (("ours", "trust_all"), ("ours", "evidence_first"), ("ours", "sensor_only"),
                          ("evidence_first", "trust_all"), ("ours", "no_lookahead"), ("no_lookahead", "evidence_first"),
                          ("attack_mutable", "attack_ledger"))}
    S["main10_kpis"] = kpis(main10, "base", ["oracle", "trust_all", "ignore", "sensor_only", "evidence_first", "ours"])
    S["main10_kpis_by_rho"] = {str(l): kpis({k: v for k, v in main10.items() if k[1] == l}, "base",
                                            ["oracle", "trust_all", "ignore", "sensor_only", "evidence_first", "ours"])
                               for l in LEVELS}
    S["claims"] = claims(main10)
    S["calibration"] = calibration(main10)
    S["tamper"] = tamper(main10)
    S["runtime_s_per_day"] = {p: round(statistics.fmean(r["seconds"] / len(r["days"]) for k, r in main10.items()
                                                        if k[3] == p and r["seconds"]), 2)
                              for p in ("ours", "evidence_first", "no_lookahead", "no_evidence", "sensor_only")
                              if any(k[3] == p and r["seconds"] for k, r in main10.items())}

    role = run_means(load("role"))
    if role:
        S["role"] = table(role, "base", ["trust_all", "evidence_first", "ours"])
        S["role_pairs"] = {"ours_vs_evidence_first": paired(role, "base", "ours", "evidence_first")}
        S["role_calibration"] = calibration(role)

    sens = run_means(load("sensors"))
    if sens:
        S["sensors"] = {t: table(sens, t, ["sensor_only", "evidence_first", "ours"], ref=main10, ref_tag="base",
                                 compare=("ours", "evidence_first"))
                        for t in sorted({k[0] for k in sens})}

    la = run_means(load("lookahead"))
    if la:
        S["lookahead"] = {t: table(la, t, ["ours"], ref=main10, ref_tag="base", compare=("ours", "ours"))
                          for t in sorted({k[0] for k in la})}
    S["base_3seeds"] = table({k: v for k, v in main10.items() if k[2] in (44, 55, 66)}, "base",
                             ["trust_all", "sensor_only", "evidence_first", "ours"])

    par = run_means(load("params"))
    if par:
        S["params"] = {t: table(par, t, ["trust_all", "evidence_first", "ours"], ref=main10, ref_tag="base",
                                compare=("ours", "trust_all"))
                       for t in sorted({k[0] for k in par})}
    S["networks"] = networks()
    json.dump(S, open(f"{ROOT}/results/audit_summary.json", "w", encoding="utf-8"), indent=1)

    m = S["main10"]
    print("main10 VoI (run-level):")
    for lev, row in m.items():
        print(f"  {lev:>5} runs={row['runs']:3d} " + " ".join(
            f"{p}={row[p]['voi']:.1f}" for p in pols if p in row))


if __name__ == "__main__":
    main()
