"""Compare our Tables 5-8 replay with the paper; writes results/step4_comparison.json."""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stp import rotterdam
from stp.evaluate import evaluate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOL = {"reroute_TEU": 0.5, "resched_vh": 0.26, "dCf_pct": 0.6, "dCc_pct": 0.6, "dCt_pct": 0.6, "cancelled_LCS": 0.5,
       "extra_trucks": 0.5, "dEarly_TEUh": 0.6, "dLate_TEUh": 0.6}


def implied_cost(p, b):
    """Total cost implied by a paper row: base components scaled by the reported % changes, plus penalties
    from the reported TEU*h changes (all shipments have rho- = 0.5, rho+ = 1.5)."""
    return (b.fixed * (1 + p["dCf_pct"] / 100) + b.variable * (1 + p["dCc_pct"] / 100)
            + b.transshipment * (1 + p["dCt_pct"] / 100)
            + 0.5 * (b.early_teu_h + p["dEarly_TEUh"]) + 1.5 * (b.late_teu_h + p["dLate_TEUh"]))


def main():
    rows = json.load(open(f"{ROOT}/results/step4_paper_scenarios.json", encoding="utf-8"))
    b = evaluate(rotterdam.build_instance(), rotterdam.base_plan())
    match = {k: 0 for k in TOL}
    out, cheaper, equal, dearer = [], [], [], []
    for r in rows:
        o = r["ours"]["STP"]
        m = {k: abs(o[k] - r["paper"][k]) <= t for k, t in TOL.items()}
        for k, v in m.items():
            match[k] += v
        imp = implied_cost(r["paper"], b)
        gap = o["total_cost"] - imp
        cls = "equal" if abs(gap) <= 25 else ("ours_cheaper" if gap < 0 else "paper_cheaper")
        {"equal": equal, "ours_cheaper": cheaper, "paper_cheaper": dearer}[cls].append(r["id"])
        out.append({"id": r["id"], "table": r["table"], "ours_cost": o["total_cost"], "paper_implied_cost": round(imp, 1),
                    "gap": round(gap, 1), "class": cls, "kpi_match": m, "all_kpis_match": all(m.values()),
                    "paper_consistent": r["paper_consistent"],
                    "variants": {v: r["ours"][v]["total_cost"] for v in r["ours"]}})
    n = len(rows)
    summary = {"rows": n, "kpi_match_counts": match,
               "rows_all_9_kpis_match": sum(x["all_kpis_match"] for x in out),
               "cost_equal": len(equal), "ours_cheaper": cheaper, "paper_cheaper": dearer,
               "variant_mean_increase_vs_base_pct": {
                   v: round(sum(100 * (x["variants"][v] - b.total) / b.total for x in out) / n, 2)
                   for v in ("STP", "N-STP", "R-STP")},
               "stp_strictly_better_than": {
                   v: sum(x["variants"]["STP"] < x["variants"][v] - 0.5 for x in out) for v in ("N-STP", "R-STP")},
               "stp_never_worse": all(x["variants"]["STP"] <= min(x["variants"].values()) + 0.5 for x in out)}
    json.dump({"summary": summary, "rows": out}, open(f"{ROOT}/results/step4_comparison.json", "w", encoding="utf-8"),
              indent=1)
    print(json.dumps(summary, indent=1))
    for x in out:
        if x["class"] != "equal":
            print(f"  {x['id']:28s} ours={x['ours_cost']:9.1f} paper-implied={x['paper_implied_cost']:9.1f} "
                  f"gap={x['gap']:8.1f} {x['class']} paper_consistent={x['paper_consistent']}")


if __name__ == "__main__":
    main()
