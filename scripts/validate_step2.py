"""Regression test for step 2. Run:  python -m scripts.validate_step2"""
import glob
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp.evaluate import evaluate
from stp.instance import Instance
from stp.plan import Plan

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

CASES = [  # (instance, plan, expected components from the paper)
    ("rotterdam", "rotterdam_base",
     dict(fixed=1320, variable=10476, transshipment=3345, early=205, late=615, total=15961)),
    ("rotterdam_S4S5_late2h", "rotterdam_S4S5_late2h_STP",
     dict(fixed=1770, variable=11332.2, transshipment=3344.6, early=200, late=615, total=17261.8)),
    ("rotterdam_S4S5_late2h", "rotterdam_S4S5_late2h_N-STP",
     dict(fixed=3240, variable=10600, transshipment=4778, early=75, late=1350, total=20043)),
    ("rotterdam_S4S5_late2h", "rotterdam_S4S5_late2h_R-STP",
     dict(fixed=2370, variable=12473.4, transshipment=3344.6, early=275, late=615, total=19078)),
]
TOL = 0.5  # paper reports rounded euros in Sec. 6.2


def main():
    ok = True
    for i_name, p_name, exp in CASES:
        inst = Instance.load(f"{D}/instances/{i_name}.json")          # also tests JSON round-trip
        plan = Plan.load(f"{D}/reference_solutions/{p_name}.json")
        ev = evaluate(inst, plan)
        got = ev.row()
        bad = {k: (got[k], v) for k, v in exp.items() if abs(got[k] - v) > TOL}
        status = "PASS" if (not bad and ev.feasible) else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] {p_name:32s} total={got['total']:>9}  expected={exp['total']}"
              + (f"  mismatches={bad}" if bad else "") + ("" if ev.feasible else f"  {ev.violations}"))
    n = 0
    for f in sorted(glob.glob(f"{D}/instances/*.json")):
        inst = Instance.load(f)
        n += 1
        assert all(s.o in inst.terminals and s.d in inst.terminals for s in inst.shipments), f
    print(f"[PASS] {n} instances load from JSON with valid terminals")
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
