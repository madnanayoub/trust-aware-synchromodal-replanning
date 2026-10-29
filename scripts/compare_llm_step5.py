"""Ours with the mock interpreter vs ours with a real LLM, on the test set (same days).

    python -m scripts.compare_llm_step5 --llm-runs results/step5_runs_test_llm.json
"""
import argparse
import json
import os
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-runs", default="results/step5_runs_test_llm.json")
    a = ap.parse_args()
    base = {(r["rho"], r["seed"], r["day"]): r for r in
            json.load(open(f"{ROOT}/results/step4_unreliable_baselines_test.json", encoding="utf-8"))}
    mock = {(r["rho"], r["seed"]): r for r in json.load(open(f"{ROOT}/results/step5_runs_test.json", encoding="utf-8"))
            if r["variant"] == "ours"}
    llm_runs = json.load(open(os.path.join(ROOT, a.llm_runs), encoding="utf-8"))
    llm = {(r["rho"], r["seed"]): r for r in llm_runs if r["variant"] == "ours"}
    name = llm_runs[0].get("interpreter", "llm")
    rows, lines = {}, []
    for rho in sorted({k[0] for k in llm}) + ["all"]:
        keys = [k for k in llm if rho == "all" or k[0] == rho]
        days = [(k, d) for k in keys for d in range(len(llm[k]["days"]))]
        c = lambda f: statistics.fmean(f(k, d) for k, d in days)
        o = c(lambda k, d: base[k + (d,)]["oracle"]["realized_cost"])
        i = c(lambda k, d: base[k + (d,)]["ignore"]["realized_cost"])
        t = c(lambda k, d: base[k + (d,)]["trust_all"]["realized_cost"])
        m = c(lambda k, d: mock[k]["days"][d]["realized_cost"])
        l = c(lambda k, d: llm[k]["days"][d]["realized_cost"])
        voi = lambda x: round(100 * (i - x) / (i - o), 1)
        faithful = [dd["faithful"] for k in keys for dd in llm[k]["days"]]
        rows[str(rho)] = {"trust_all": voi(t), "ours_mock": voi(m), "ours_llm": voi(l), "days": len(days),
                          "llm_explanations_faithful_pct": round(100 * sum(faithful) / len(faithful), 1)}
        lines.append(f"{str(rho):>5} {voi(t):>10}% {voi(m):>10}% {voi(l):>10}%   "
                     f"faithful explanations {rows[str(rho)]['llm_explanations_faithful_pct']}%")
    out = {"llm": name, "value_of_information_captured_pct": rows}
    json.dump(out, open(f"{ROOT}/results/step5_llm_vs_mock.json", "w", encoding="utf-8"), indent=1)
    print(f"interpreter: {name}\n  rho  trust_all   ours_mock    ours_llm")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
