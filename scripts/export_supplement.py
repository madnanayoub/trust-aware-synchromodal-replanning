"""Write the supplementary tables (CSV / text) to results/supplement/.

    python -m scripts.export_supplement

S1_paper_scenarios.csv   all 62 scenarios of Qu et al. (2019) Tables 5-8: our STP / N-STP / R-STP costs, the cost
                         implied by the paper's KPIs, the gap, its class, and each of the nine KPIs (ours, paper, match)
S2_development_set.csv   value of information captured and regret on the development streams (seeds 11/22/33)
S3_llm_configuration.txt model identifiers, access period, decoding settings and the full interpreter prompt
S4_interpreter_errors.csv every held-out message an interpreter got wrong
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = f"{ROOT}/results/supplement"
KPIS = ["reroute_TEU", "resched_vh", "dCf_pct", "dCc_pct", "dCt_pct", "cancelled_LCS", "extra_trucks",
        "dEarly_TEUh", "dLate_TEUh"]


def load(name):
    return json.load(open(f"{ROOT}/results/{name}", encoding="utf-8"))


# Published rows that report a cheaper plan than our optimum, and why that plan is not attainable (checked with
# stp/evaluate.py: the unchanged original plan violates S2's new release time in T5_S2_+0.5h).
UNATTAINABLE = {"T5_S2_+0.5h": "published solution infeasible: the paper reports no change at all, but the unchanged "
                               "plan loads S2 on v0001 before its new release time"}


def discrepancy(c, r):
    """Source of a difference between our result and the published one."""
    if c["class"] == "equal":
        return "exact" if c["all_kpis_match"] else ("same cost within 25 EUR, different plan (alternative optimum "
                                                    "or rounding of the published percentages)")
    if c["class"] == "ours_cheaper":
        return "published solution not optimal (our optimum is cheaper and feasible under the evaluator)"
    if r["id"] in UNATTAINABLE:
        return UNATTAINABLE[r["id"]]
    return "published KPIs internally inconsistent (extra trucks reported without the matching fixed-cost change)"


def s1():
    comp = {r["id"]: r for r in load("step4_comparison.json")["rows"]}
    rows = load("step4_paper_scenarios.json")
    with open(f"{OUT}/S1_paper_scenarios.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "paper_table", "events", "STP_cost", "N-STP_cost", "R-STP_cost", "paper_implied_cost",
                    "gap_EUR", "class", "discrepancy_type", "all_9_KPIs_match", "STP_feasible_under_evaluator",
                    "STP_mip_minus_evaluator_EUR"]
                   + [f"{k}_{x}" for k in KPIS for x in ("ours", "paper", "match")])
        for r in rows:
            c = comp[r["id"]]
            ours = r["ours"]["STP"]
            w.writerow([r["id"], r["table"], json.dumps(r["events"]), c["variants"]["STP"], c["variants"]["N-STP"],
                        c["variants"]["R-STP"], c["paper_implied_cost"], c["gap"], c["class"], discrepancy(c, r),
                        c["all_kpis_match"], ours.get("feasible"), ours.get("mip_vs_eval")]
                       + [v for k in KPIS for v in (ours.get(k), r["paper"].get(k), c["kpi_match"].get(k))])
    with open(f"{OUT}/S1_notes.txt", "w", encoding="utf-8") as f:
        f.write("""Supplementary Table S1 - notes

Errors in the published input data (corrected in data/cases, confirmed by the paper's own totals):
  1. Rail service v0006 costs 22.62 EUR/TEU, not the 22.16 EUR/TEU printed in Qu et al. (2019), Table 2.
  2. Table A.12 omits a row of 50 trucks carrying S5 from the Port of Rotterdam to Dordrecht; without it, S5 is not
     conserved.

Discrepancy types in S1 (column discrepancy_type):
  exact                       all nine KPIs equal to the published values
  same cost, different plan   total cost within 25 EUR of the published one, but at least one KPI differs: another
                              optimal plan, or rounding of the published percentage changes
  published solution not optimal          our optimum is cheaper and feasible under the independent evaluator
  published KPIs internally inconsistent  e.g. extra trucks reported without the matching change in fixed cost
  published solution infeasible           the reported plan violates the scenario's own data (see the row)

Modelling choices: aggregating the trucks of a shipment on a lane into one integer flow and modelling transshipment
explicitly reproduce all four published optima. Re-solving the three scenarios in which the paper reports a cheaper plan
with two or three independent truck departure groups per lane and shipment gives the same optima, so these
discrepancies do not come from the aggregation.

Evaluator check: for every STP solve, STP_feasible_under_evaluator reports the independent evaluator's feasibility
verdict, and STP_mip_minus_evaluator_EUR the MIP objective minus the evaluated cost (at most the tie-breaking term of
1 EUR, see Appendix B).
""")


def s2():
    S = load("step5_summary.json")
    B = load("step4_unreliable_baselines.json")
    worse = {}
    for r in B:
        worse[str(r["rho"])] = worse.get(str(r["rho"]), 0) + (r["trust_all"]["realized_cost"] > r["ignore"]["realized_cost"] + 1e-6)
    worse["all"] = sum(worse.values())
    with open(f"{OUT}/S2_development_set.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        pols = ["trust_all", "ignore", "ours", "no_evidence", "no_reputation", "no_lookahead"]
        w.writerow(["rho", "days"] + [f"VoI_{p}" for p in pols] + [f"regret_{p}" for p in pols]
                   + ["trust_all_worse_than_ignore_days"])
        for rho, v in list(S["by_rho"].items()) + [("all", S["pooled"]["all"])]:
            w.writerow([rho, v.get("days")] + [v.get(p, {}).get("voi_captured_pct") for p in pols]
                       + [v.get(p, {}).get("regret") for p in pols] + [worse.get(rho)])


def s3():
    from stp.genai import INTERPRET_SYSTEM
    import inspect
    from stp import genai
    expl = getattr(genai, "EXPLAIN_SYSTEM", "")
    with open(f"{OUT}/S3_llm_configuration.txt", "w", encoding="utf-8") as f:
        f.write("Models: claude-haiku-5-5 (interpreter and explainer), claude-sonnet-5-5 (held-out interpretation only)\n"
                "Access: Anthropic Messages API, 15-16 October 2026; answers cached by SHA-256 of (model, sender, text)\n"
                "Decoding: provider defaults (temperature not settable for these models); max_tokens 2000, doubled on\n"
                "truncation up to three attempts; responses that still end truncated are rejected.\n"
                "User message: the network description (shipments: origin, destination, TEU, release time; services:\n"
                "mode, route, loading start time), the sender id and the message text.\n\n"
                "INTERPRETER SYSTEM PROMPT\n" + INTERPRET_SYSTEM + "\n\n")
        if expl:
            f.write("EXPLAINER SYSTEM PROMPT\n" + expl + "\n")


def s4():
    with open(f"{OUT}/S4_interpreter_errors.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["interpreter", "message", "expected", "returned"])
        for tag in ("mock", "anthropic_claude-haiku-5-5", "anthropic_claude-sonnet-5-5"):
            r = load(f"interpreter_{tag}.json")
            for e in r["heldout"]["errors"]:
                w.writerow([tag, e["text"], json.dumps(e["expected"]), json.dumps(e["got"])])


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for fn in (s1, s2, s3, s4):
        fn()
    print("written:", sorted(os.listdir(OUT)))
