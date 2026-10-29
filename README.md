# Trust-aware synchromodal replanning under unreliable stakeholder reports

Code, data and results for the paper

> Muhammad Adnan Ayub, Ghufran Ahmed, Muhammad Shariq Nadeem, Usama Antuley, Shahbaz Siddiqui and Muneer Ahmad,
> *Trust-aware synchromodal replanning under unreliable stakeholder reports: evidence fusion, ledger-based reputation
> and large language model interpretation*, submitted to *IET Intelligent Transport Systems*.

Archived version: [Zenodo DOI to be added]

The repository contains:

- a re-implementation and validation of the STP / N-STP / R-STP synchromodal replanning model of Qu et al. (2019), with
  an independent cost and feasibility evaluator (`stp/evaluate.py`);
- a benchmark in which disturbances arrive as stakeholder reports of mixed reliability, with partial sensor evidence;
- the trust-aware replanning framework: large language model (LLM) interpretation with guardrails, evidence fusion, a
  permissioned ledger with a Beta reputation contract, an expected-cost lookahead and numerically checked explanations;
- `besu-trust-ledger/`: the reputation contract as a Solidity smart contract, its tests, the generator of a
  4-validator Hyperledger Besu QBFT network, and the replay, scaling and fault-tolerance experiments (Section 7.10);
- all input data, generated report and sensor streams, experiment results and Supplementary Tables S1–S4.

## Software versions used for the paper

| Component | Version |
|---|---|
| Python | 3.13 (3.10 or newer works) |
| docplex / IBM CPLEX | 2.32 / 22.1.2 (academic licence) |
| HiGHS (`highspy`) | 1.15.1 (open-source alternative to CPLEX; same optima) |
| numpy, scipy, openpyxl, cryptography | 2.5, 1.18, 3.1, 50.0 |
| LLM (optional, answers are cached) | Claude Haiku 5.5 and Claude Sonnet 5.5, accessed 15–16 October 2026 |
| Besu deployment | Hyperledger Besu 26.9.0 (Docker), Node.js 22 or 24, Hardhat 2.29.1, Solidity 0.8.28 |

## Installation

```
pip install -r requirements.txt
pip install "cplex==22.1.2.1"                                   # optional; match your CPLEX Studio version
docplex config --upgrade "C:\Program Files\IBM\ILOG\CPLEX_Studio2212"
python -m scripts.check_cplex                                   # expect: FULL CPLEX OK
```

Without CPLEX, add `--backend highs` to every command below.

## Reproducing the paper

```
python run_all.py --stages validate     # regression checks, each prints ALL PASS (about 1 minute)
python run_all.py                       # every experiment in order (CPLEX)
python run_all.py --backend highs       # the same with HiGHS
```

| Paper | Command | Results |
|---|---|---|
| Sec. 3, Table 2, Fig. 1, Table A1 – validation, 4TU instances | `python -m scripts.run_step3`, `python -m scripts.run_step4_paper`, `python -m scripts.compare_step4_paper` | `results/step3_results.json`, `results/step4_paper_scenarios.json`, `results/step4_comparison.json` |
| Sec. 7.1–7.5, Tables 5–10, Figs 3–5 – main comparison, ablations, attack, role-specific behaviour, imperfect sensors, lookahead and parameter sensitivity | `python -m scripts.run_audit --workers 6` (resumable), then `python -m scripts.analyze_audit` and `python -m scripts.analyze_stats` | `results/audit/*.json`, `results/audit_summary.json`, `results/stats_revision.json` |
| Sec. 7.4, Fig. 6 – sensor coverage | `python -m scripts.run_6b_coverage` | `results/step6b_coverage_summary.json` |
| Sec. 7.6, Table 11 – LLM evaluation | `python -m scripts.eval_interpreter` (replays `results/llm_cache/`, no API key needed) | `results/interpreter_*.json` |
| Sec. 7.7–7.8, Table 12, Fig. 7 – 4TU networks, rare high-cost days | `python -m scripts.run_6c_networks` | `results/step6c_*.json` |
| Sec. 7.10, Tables 13–15, Figs 8–10 – Hyperledger Besu deployment | `besu-trust-ledger/` (see its README) | `besu-trust-ledger/results/` |
| Supplementary Tables S1–S4 | `python -m scripts.export_supplement` | `results/supplement/` |

`run_audit` runs groups separately with `--groups main10 role sensors lookahead params`; with `--workers` > 1 each run
uses one solver thread. The ledger traces replayed on Besu are exported with
`python -m scripts.export_chain_trace --rho 0 0.25 0.5 0.75 1 --seeds 44`.

## Random seeds and statistics

- Development set: seeds 11, 22 and 33 (used to build the framework; Supplementary Table S2).
- Test set: seeds 44, 55, 66 and 101–107, each at the five dishonesty levels ρ = 0, 0.25, 0.5, 0.75, 1, giving 50 runs
  of 40 days. Seeds 101–107 were added after the design was frozen. A seed fixes both the stakeholder profiles and the
  daily events, so the same seed at different levels gives dependent runs.
- Role-specific variant, robustness checks: seeds 44, 55 and 66. Larger 4TU networks: seeds 77, 88 and 99.
- `scripts/analyze_stats.py` holds the inferential analysis. The unit is the run; the primary tests compare policies
  averaged over the five levels with one value per seed (exact two-sided Wilcoxon signed-rank test on the ten seed
  means, Holm-adjusted over the two comparisons). Per-level tests are secondary (Holm over the five levels). Intervals are
  95% percentile bootstrap intervals (10,000 resamples; seeds are resampled for pooled results).

## Evaluator

Every reported cost is computed by `stp/evaluate.py`, which checks a plan against the cost equations and constraints of
Qu et al. (2019) independently of the MIP. `results/step3_results.json` (field `mip_vs_eval`) and
`results/step4_paper_scenarios.json` (fields `feasible`, `mip_vs_eval`) record its verdict and the difference between
MIP objective and evaluated cost for all 220 validation solves; the difference is at most the tie-breaking term of €1.

## Layout

```
stp/                library: instances, plans, evaluator, MIP model, scenarios, uncertainty, execution with recourse,
                    ledger, reputation contract, language-model interpreter and explainer, trust-aware planner
scripts/            experiments, analysis and regression tests
data/               instances, published scenario tables, generated report and sensor streams, held-out messages
data_raw/           the 15 original 4TU workbooks
results/            experiment results and supplementary tables
besu-trust-ledger/  smart contract, Besu network and blockchain experiments
run_all.py          runs all stages in order
```

## Data

4TU instances: Qu, W., Rezaei, J., Maknoon, Y. and Tavasszy, L. (2019), 4TU.ResearchData,
doi:10.4121/uuid:ce696560-ef05-460d-86f2-39cfefc26416. The Rotterdam case is rebuilt from the tables of
Qu et al. (2019), *Transportation Research Part E*, 131, 308–328, with the two data corrections listed in
`results/supplement/S1_notes.txt`.

## Licence

Code: MIT (see `LICENSE`). Data derived from the 4TU dataset remain under that dataset's licence.
