"""Regression test for step 4. Run: python -m scripts.validate_step4 [--backend cplex]"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.evaluate import evaluate
from stp.execution import execute
from stp.kpi import kpis
from stp.model import STPModel, Options
from stp.scenarios import apply_events

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    a = ap.parse_args()
    B, P = rotterdam.build_instance(), rotterdam.base_plan()
    ok = True

    def check(name, got, exp, tol=0.05):
        global ok
        good = abs(got - exp) <= tol
        ok &= good
        print(f"[{'PASS' if good else 'FAIL'}] {name:52s} got={got:10.2f} expected={exp}")

    # 4a: scenario replay reproduces paper rows exactly (all 9 comparable KPIs)
    for ev, exp in [([["lcs_delay", "v0001", 1.0]], {"resched_vh": 1, "dEarly_TEUh": -30, "dLate_TEUh": 50}),
                    ([["lcs_delay", "v0002", 1.5]], {"dCf_pct": 55.68, "extra_trucks": 50, "cancelled_LCS": 1,
                                                     "dEarly_TEUh": -335, "dLate_TEUh": 250}),
                    ([["volume", "S4", 0.2]], {"reroute_TEU": 40, "dCf_pct": 22.73, "dCt_pct": 14.28,
                                               "dEarly_TEUh": -140, "dLate_TEUh": 80})]:
        inst, un = apply_events(B, ev)
        r = STPModel(inst, Options(backend=a.backend, anchor_plan=P, unavailable_lcs=un)).solve()
        k = kpis(inst, r.plan, B, P)
        for key, v in exp.items():
            check(f"{ev[0][1]}{ev[0][2]:+g} {key} (paper)", k[key], v, 0.6)
    # Table 9 through the scenario module
    inst, _ = apply_events(B, [["release", "S4", 2], ["release", "S5", 2]])
    r = STPModel(inst, Options(backend=a.backend)).solve()
    check("Table 9 STP via scenarios", evaluate(inst, r.plan).total, 17261.8)
    # 4b: execution simulator, hand-checked values
    t9 = [["release", "S4", 2], ["release", "S5", 2]]
    check("oracle on Table 9 case", execute(B, P, t9, t9, "o", a.backend).realized_cost, 17261.8)
    check("ignore on Table 9 case (= oracle + 30 spot-truck premium)",
          execute(B, P, t9, [], "i", a.backend).realized_cost, 17261.8 + 30 * (15 + 30.98) * 0.5)
    check("no events, no reports", execute(B, P, [], [], "n", a.backend).realized_cost, 15960.9)
    print("ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
