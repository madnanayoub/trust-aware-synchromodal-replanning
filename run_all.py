"""Run all experiments in order.

    python run_all.py                              # everything, CPLEX
    python run_all.py --stages validate            # regression checks only (about 1 minute)
    python run_all.py --backend highs              # without a CPLEX licence (open-source HiGHS)
    python run_all.py --from step5                 # resume from a stage

Stages: validate, step3, step4, step5, llm, step6b, step6c, audit (see README.md).
The first failing command stops the run.
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TEST = ["--seeds", "44", "55", "66"]


def stages(b, w):
    be = ["--backend", b]
    wk = ["--workers", str(w)]
    return [
        ("validate", ([["scripts.check_cplex"]] if b == "cplex" else []) + [
            ["scripts.validate_step2"], ["scripts.validate_step3", *be], ["scripts.validate_step4", *be],
            ["scripts.validate_step5", *be], ["scripts.test_llm_plumbing"]]),
        ("step3", [["scripts.run_step3", *be, "--time-limit", "600"]]),
        ("step4", [["scripts.run_step4_paper", *be], ["scripts.compare_step4_paper"],
                   ["scripts.run_step4_unreliable", *be],
                   ["scripts.run_step4_unreliable", *be, *TEST, "--out", "results/step4_unreliable_baselines_test.json"]]),
        ("step5", [["scripts.run_step5", *be, *wk],
                   ["scripts.run_step5", *be, *wk, *TEST, "--out", "results/step5_runs_test.json"],
                   ["scripts.analyze_step5", "--set", "dev"], ["scripts.analyze_step5", "--set", "test"],
                   ["scripts.eval_interpreter"]]),
        ("llm", [["scripts.eval_interpreter", "--llm", "anthropic", "--model", "claude-haiku-5-5"],
                 ["scripts.eval_interpreter", "--llm", "anthropic", "--model", "claude-sonnet-5-5", "--heldout-only"],
                 ["scripts.run_step5", *be, "--workers", "1", *TEST, "--variants", "ours", "--llm", "anthropic",
                  "--model", "claude-haiku-5-5", "--llm-explain", "--out", "results/step5_runs_test_llm.json"],
                 ["scripts.compare_llm_step5"]]),
        ("step6b", [["scripts.run_6b_coverage", *be, *wk]]),
        ("step6c", [["scripts.run_6c_networks", *be, *wk]]),
        ("audit", [["scripts.run_audit", *be, *wk], ["scripts.analyze_audit"], ["scripts.analyze_stats"], ["scripts.export_supplement"]]),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="cplex", choices=["cplex", "highs", "auto"])
    ap.add_argument("--workers", type=int, default=2, help="parallel processes for the long runs")
    ap.add_argument("--stages", nargs="*", help="run only these stages")
    ap.add_argument("--from", dest="start", help="start at this stage and run the rest")
    a = ap.parse_args()
    plan = stages(a.backend, a.workers)
    names = [n for n, _ in plan]
    if a.stages:
        bad = set(a.stages) - set(names)
        if bad:
            sys.exit(f"unknown stage(s) {sorted(bad)}; choose from {names}")
        plan = [p for p in plan if p[0] in a.stages]
    elif a.start:
        if a.start not in names:
            sys.exit(f"unknown stage {a.start}; choose from {names}")
        plan = plan[names.index(a.start):]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    for name, cmds in plan:
        print(f"\n=== stage {name} ===", flush=True)
        for c in cmds:
            print(f"$ python -m {' '.join(c)}", flush=True)
            t = time.time()
            r = subprocess.run([sys.executable, "-m", *c], cwd=ROOT, env=env)
            if r.returncode != 0:
                sys.exit(f"FAILED (exit {r.returncode}): python -m {' '.join(c)}")
            print(f"  done in {time.time() - t:.0f}s", flush=True)
    print(f"\nALL STAGES DONE in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
