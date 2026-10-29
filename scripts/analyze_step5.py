"""Analyse step-5 runs against the step-4 baselines (same days). Writes results/step5_summary.json."""
import json
import os
import random
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from scipy.stats import wilcoxon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def boot_ci(x, n=2000, seed=0):
    rng = random.Random(seed)
    m = [statistics.fmean(rng.choices(x, k=len(x))) for _ in range(n)]
    m.sort()
    return round(m[int(0.025 * n)], 1), round(m[int(0.975 * n)], 1)


def auc(pos, neg):
    """P(score of a reliable stakeholder > score of an unreliable one)."""
    if not pos or not neg:
        return None
    s = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return round(s / (len(pos) * len(neg)), 3)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev", choices=["dev", "test"])
    a = ap.parse_args()
    bfile = "step4_unreliable_baselines.json" if a.set == "dev" else "step4_unreliable_baselines_test.json"
    base = json.load(open(f"{ROOT}/results/{bfile}", encoding="utf-8"))
    runs = json.load(open(f"{ROOT}/results/step5_runs{'' if a.set == 'dev' else '_test'}.json", encoding="utf-8"))
    B = {(r["rho"], r["seed"], r["day"]): r for r in base}
    V = {}
    for run in runs:
        for d in run["days"]:
            V[(run["variant"], run["rho"], run["seed"], d["day"])] = d["realized_cost"]
    variants = sorted({r["variant"] for r in runs}, key=lambda v: ["ours", "no_evidence", "no_reputation",
                                                                   "no_lookahead", "attack_ledger",
                                                                   "attack_mutable"].index(v))
    levels = sorted({r["rho"] for r in base})
    summary = {"by_rho": {}, "pooled": {}}
    pols = ["oracle", "trust_all", "ignore"] + variants

    def cost(p, k):
        return B[k][p]["realized_cost"] if p in ("oracle", "trust_all", "ignore") else V[(p,) + k]

    for rho in levels + ["all"]:
        keys = [k for k in B if (rho == "all" or k[0] == rho) and all((v,) + k in V for v in variants)]
        if not keys:
            continue
        row = {"days": len(keys)}
        o = statistics.fmean(cost("oracle", k) for k in keys)
        i = statistics.fmean(cost("ignore", k) for k in keys)
        for p in pols:
            c = [cost(p, k) for k in keys]
            reg = [cost(p, k) - cost("oracle", k) for k in keys]
            m = statistics.fmean(c)
            row[p] = {"mean_cost": round(m, 1), "regret": round(m - o, 1), "regret_ci95": boot_ci(reg),
                      "voi_captured_pct": round(100 * (i - m) / (i - o), 1) if i > o else None}
        diff = [cost("ours", k) - cost("trust_all", k) for k in keys]
        nz = [x for x in diff if abs(x) > 1e-6]
        row["ours_vs_trust_all"] = {"better_days": sum(x < -0.5 for x in diff), "worse_days": sum(x > 0.5 for x in diff),
                                    "mean_saving": round(-statistics.fmean(diff), 1),
                                    "wilcoxon_p": float(f"{wilcoxon(nz).pvalue:.2e}") if len(nz) > 5 else None}
        diff_i = [cost("ours", k) - cost("ignore", k) for k in keys]
        nzi = [x for x in diff_i if abs(x) > 1e-6]
        row["ours_vs_ignore"] = {"better_days": sum(x < -0.5 for x in diff_i), "worse_days": sum(x > 0.5 for x in diff_i),
                                 "mean_saving": round(-statistics.fmean(diff_i), 1),
                                 "wilcoxon_p": float(f"{wilcoxon(nzi).pvalue:.2e}") if len(nzi) > 5 else None}
        summary["by_rho" if rho != "all" else "pooled"][str(rho)] = row

    # trust calibration (end-of-stream reputation of 'ours')
    pos, neg, by_profile = [], [], {}
    for run in runs:
        if run["variant"] != "ours":
            continue
        for sid, prof in run["profiles"].items():
            rep = run["reputation"].get(sid)
            if not rep or rep["n"] == 0:
                continue
            by_profile.setdefault(prof, []).append(rep["p_real"])
            (pos if prof == "reliable" else neg).append(rep["p_real"])
    summary["trust_calibration"] = {"mean_p_real": {k: round(statistics.fmean(v), 3) for k, v in by_profile.items()},
                                    "n": {k: len(v) for k, v in by_profile.items()},
                                    "auc_reliable_vs_unreliable": auc(pos, neg)}
    # ledger overhead and tampering
    led = [r["ledger"] for r in runs if r["variant"] == "ours"]
    summary["ledger"] = {"streams": len(led), "days_per_stream": len(runs[0]["days"]),
                         "mean_blocks": round(statistics.fmean(l["blocks"] for l in led), 1),
                         "mean_entries": round(statistics.fmean(l["entries"] for l in led), 1),
                         "mean_kB": round(statistics.fmean(l["bytes"] for l in led) / 1024, 1),
                         "max_verify_ms": max(l["verify_ms"] for l in led), "all_valid": all(l["valid"] for l in led)}
    att = [r for r in runs if r["variant"] == "attack_ledger"]
    summary["tampering"] = {"attempts_detected": sum(r["tamper_detected"] for r in att),
                            "streams_with_strategic_attackers": sum(
                                any(p == "strategic" for p in r["profiles"].values()) for r in att)}
    # explanations
    ex = [d for r in runs if r["variant"] == "ours" for d in r["days"]]
    summary["explanations"] = {"n": len(ex), "faithful_pct": round(100 * sum(d["faithful"] for d in ex) / len(ex), 1),
                               "mean_words": round(statistics.fmean(len(d["explanation"].split()) for d in ex), 1)}
    summary["runtime"] = {"mean_s_per_day": {v: round(statistics.fmean(
        r["seconds"] / len(r["days"]) for r in runs if r["variant"] == v), 2) for v in variants}}
    json.dump(summary, open(f"{ROOT}/results/step5_summary{'' if a.set == 'dev' else '_test'}.json", "w",
                            encoding="utf-8"), indent=1)

    print(f"{'rho':>5} " + " ".join(f"{p:>15}" for p in pols))
    for rho, row in list(summary["by_rho"].items()) + list(summary["pooled"].items()):
        print(f"{rho:>5} " + " ".join(f"{str(row[p]['voi_captured_pct']) + '%':>15}" for p in pols)
              + f"   ours vs trust_all: +{row['ours_vs_trust_all']['better_days']}/-{row['ours_vs_trust_all']['worse_days']}"
              f" p={row['ours_vs_trust_all']['wilcoxon_p']}")
    for k in ("trust_calibration", "ledger", "tampering", "explanations", "runtime"):
        print(k, summary[k])


if __name__ == "__main__":
    main()
