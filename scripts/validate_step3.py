"""Regression test for step 3 (model reproduces the paper). Run: python -m scripts.validate_step3 [--backend cplex]"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp.evaluate import evaluate
from stp.instance import Instance
from stp.model import STPModel, Options

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "instances")
CASES = [("rotterdam", "STP", 15960.9), ("rotterdam_S4S5_late2h", "STP", 17261.8),
         ("rotterdam_S4S5_late2h", "N-STP", 20043.0), ("rotterdam_S4S5_late2h", "R-STP", 19078.0)]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    a = ap.parse_args()
    ok = True
    for name, var, exp in CASES:
        inst = Instance.load(f"{D}/{name}.json")
        r = STPModel(inst, Options(variant=var, backend=a.backend)).solve()
        ev = evaluate(inst, r.plan)
        good = abs(r.objective - exp) < 0.05 and abs(ev.total - exp) < 0.05 and ev.feasible
        ok &= good
        print(f"[{'PASS' if good else 'FAIL'}] {name:22s} {var:6s} {r.backend:5s} MIP={r.objective:.1f} "
              f"eval={ev.total:.1f} paper={exp}")
    print("ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
