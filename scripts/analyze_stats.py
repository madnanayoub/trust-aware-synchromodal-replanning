"""Inferential statistics of the main Rotterdam comparison (Section 6, Table 5).

    python -m scripts.analyze_stats        -> results/stats_revision.json

Design
* Unit of analysis: the run (40 days); a run's value is its mean daily realised cost. Regret differences equal cost
  differences because both policies face the same days and the same oracle.
* The same ten seeds (44, 55, 66, 101-107) are used at every dishonesty level, and a seed fixes both the stakeholder
  profiles and the daily events. Runs that share a seed are therefore dependent across levels.
* Primary hypotheses (family of two, Holm-adjusted): averaged over the five levels, the framework has a lower mean
  regret than (H1) trust-all and (H2) evidence-first trust-all. Test: exact two-sided Wilcoxon signed-rank test on the
  ten seed-level differences (each the mean over the five levels of the paired difference), i.e. a test blocked by
  seed with ten independent observations.
* Secondary hypotheses: the same two comparisons at each of the five levels (ten paired runs each), Holm-adjusted
  within each comparison (family of five).
* Effect sizes: mean paired saving (other policy minus framework, EUR/day; positive = framework better) with a 95%
  percentile bootstrap interval (10,000 resamples of runs, or of seeds for the pooled result), and the matched-pairs
  rank-biserial correlation r = (R+ - R-) / (R+ + R-).
* Zero differences (identical run means, |d| <= 1e-6 EUR) are discarded before ranking (Wilcoxon's convention);
  p values are exact (scipy.stats.wilcoxon, method="exact", no ties among the non-zero differences).
"""
import json
import os
import random
import statistics

from scipy.stats import rankdata, wilcoxon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
B = 10000
EPS = 1e-6


def run_means(group):
    rows = json.load(open(f"{ROOT}/results/audit/{group}.json", encoding="utf-8"))
    return {(r["tag"], r["rho"], r["seed"], r["policy"]): statistics.fmean(d["cost"] for d in r["days"]) for r in rows}


def holm(ps):
    """Holm step-down adjusted p values (same order as input; None kept)."""
    idx = sorted((p, i) for i, p in enumerate(ps) if p is not None)
    m = len(idx)
    adj = [None] * len(ps)
    run = 0.0
    for k, (p, i) in enumerate(idx):
        run = max(run, min(1.0, (m - k) * p))
        adj[i] = run
    return adj


def test(d):
    nz = [x for x in d if abs(x) > EPS]
    out = {"n": len(d), "zeros": len(d) - len(nz), "better": sum(x > EPS for x in d), "worse": sum(x < -EPS for x in d)}
    if len(nz) >= 1:
        ranks = rankdata([abs(x) for x in nz])
        rp = sum(r for r, x in zip(ranks, nz) if x > 0)
        rm = sum(r for r, x in zip(ranks, nz) if x < 0)
        out["rank_biserial"] = round((rp - rm) / (rp + rm), 3)
        ties = len(set(abs(x) for x in nz)) < len(nz)
        out["p"] = float(wilcoxon(nz, method="approx" if ties else "exact").pvalue)
        out["p_method"] = "approx" if ties else "exact"
    return out


def boot_mean(d, seed=0):
    rng = random.Random(seed)
    n = len(d)
    xs = sorted(statistics.fmean(d[rng.randrange(n)] for _ in range(n)) for _ in range(B))
    return [round(xs[int(0.025 * B)], 1), round(xs[int(0.975 * B) - 1], 1)]


def compare(M, other, tag="base"):
    seeds = sorted({k[2] for k in M if k[0] == tag and k[3] == "ours"})
    res = {"levels": {}}
    for lev in LEVELS:
        d = [M[(tag, lev, s, other)] - M[(tag, lev, s, "ours")] for s in seeds]
        t = test(d)
        t.update({"saving_mean": round(statistics.fmean(d), 1), "saving_ci": boot_mean(d)})
        res["levels"][str(lev)] = t
    adj = holm([res["levels"][str(l)].get("p") for l in LEVELS])
    for l, a in zip(LEVELS, adj):
        res["levels"][str(l)]["p_holm"] = a
    # pooled, blocked by seed: one value per seed = mean over the five levels of the paired difference
    blk = [statistics.fmean(M[(tag, l, s, other)] - M[(tag, l, s, "ours")] for l in LEVELS) for s in seeds]
    t = test(blk)
    t.update({"saving_mean": round(statistics.fmean(blk), 1), "saving_ci": boot_mean(blk), "unit": "seed",
              "seed_values": [round(x, 1) for x in blk]})
    # for reference only: the naive test that treats all 50 runs as independent
    naive = [M[(tag, l, s, other)] - M[(tag, l, s, "ours")] for l in LEVELS for s in seeds]
    t["naive_50_runs_p"] = test(naive).get("p")
    res["pooled_seed_blocked"] = t
    return res


def main():
    M = run_means("main10")
    out = {"design": __doc__.strip(), "comparisons": {}}
    for other in ("trust_all", "evidence_first"):
        out["comparisons"][f"ours_vs_{other}"] = compare(M, other)
    adj = holm([out["comparisons"][f"ours_vs_{o}"]["pooled_seed_blocked"]["p"] for o in ("trust_all", "evidence_first")])
    for o, a in zip(("trust_all", "evidence_first"), adj):
        out["comparisons"][f"ours_vs_{o}"]["pooled_seed_blocked"]["p_holm"] = a
    json.dump(out, open(f"{ROOT}/results/stats_revision.json", "w", encoding="utf-8"), indent=1)
    for k, v in out["comparisons"].items():
        print(k)
        for l, t in v["levels"].items():
            print(f"  rho={l}: saving {t['saving_mean']} {t['saving_ci']}  {t['better']}/{t['worse']}  "
                  f"p={t.get('p', float('nan')):.3g} ({t.get('p_method')}) holm={t.get('p_holm') or float('nan'):.3g} "
                  f"r={t.get('rank_biserial')}")
        t = v["pooled_seed_blocked"]
        print(f"  pooled by seed: saving {t['saving_mean']} {t['saving_ci']} {t['better']}/{t['worse']} of {t['n']} seeds "
              f"p={t['p']:.3g} holm={t['p_holm']:.3g} r={t['rank_biserial']} (naive 50-run p={t['naive_50_runs_p']:.3g})")


if __name__ == "__main__":
    main()
