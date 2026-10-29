"""Regression test for step 5. Run: python -m scripts.validate_step5 [--backend cplex]"""
import argparse
import copy
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.genai import MockLLM, verify_explanation, validate_event
from stp.ledger import Ledger, Entry
from stp.ours import TrustAwarePlanner, Config
from stp.uncertainty import rotterdam_stakeholders
import random

ok = True


def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {info}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    a = ap.parse_args()
    B, P = rotterdam.build_instance(), rotterdam.base_plan()

    # ledger
    L = Ledger(["carrier"], ["v1", "v2"])
    L.submit("report", "carrier", {"day": 0, "text": "v0001 delayed 1 hour"})
    L.commit(1)
    check("ledger valid after commit", L.verify_chain() is None)
    L.chain[1].entries[0].payload["text"] = "v0001 on time"
    check("rewritten history detected", L.verify_chain() == 1)
    check("non-member rejected", L.submit("report", "mallory", {}) is None)
    check("forged signature rejected", not L.submit_signed(Entry("report", "carrier", {"x": 1}, "00" * 32)))

    # GenAI
    I = MockLLM(B)
    p = validate_event(I.interpret("Barge v0001 (PoR-Dordrecht) departs an hour and a half late due to fog.",
                                   "barge_operator"), B)
    check("interpreter parses a delay message", (p.kind, p.target, p.magnitude) == ("lcs_delay", "v0001", 1.5))
    p = validate_event(I.interpret("Volume update S4: 130 TEU instead of 100 TEU.", "shipper_S4"), B)
    check("interpreter parses a volume message", (p.kind, p.target, p.magnitude) == ("volume", "S4", 0.3))
    check("guardrail rejects unknown ids", validate_event(type(p)("lcs_delay", "v9999", 1.0, 1.0), B).kind is None)
    check("verifier catches a hallucinated number",
          not verify_explanation("Saves 4,321 EUR.", {"cost": 100})["faithful"])

    # planner on constructed days
    sh = [s.__dict__ for s in rotterdam_stakeholders(B, 0.0, random.Random(0))]
    T = TrustAwarePlanner(B, P, sh, Config(backend=a.backend))
    day = {"day": 0, "true_events": [], "evidence": [
        {"id": "e1", "day": 0, "source": "AIS", "kind": "lcs_delay", "target": "v0001", "magnitude": 0.0}],
        "reports": [{"id": "r1", "day": 0, "sender": "barge_operator", "kind": "lcs_delay", "target": "v0001",
                     "magnitude": 1.5, "sent_at": 6.5,
                     "text": "Barge v0001 (PoR-Dordrecht) departs 90 minutes late due to lock congestion."}]}
    r = T.run_day(day)
    check("false alarm refuted by AIS -> original plan kept", abs(r["realized_cost"] - 15960.9) < 0.05,
          f"cost={r['realized_cost']}")
    t9 = [["release", "S4", 2.0], ["release", "S5", 2.0]]
    day = {"day": 1, "true_events": t9, "evidence": [
        {"id": "e2", "day": 1, "source": "terminal_gate", "kind": "release", "target": "S4", "magnitude": 2.0},
        {"id": "e3", "day": 1, "source": "terminal_gate", "kind": "release", "target": "S5", "magnitude": 2.0}],
        "reports": [{"id": "r2", "day": 1, "sender": "shipper_S4", "kind": "release", "target": "S4", "magnitude": 2,
                     "sent_at": 6.5, "text": "Release of shipment S4 postponed by 2 hours."}]}
    r = T.run_day(day)
    check("Table 9 delays (one unreported, seen by gate) -> paper's STP optimum", abs(r["realized_cost"] - 17261.8) < 0.05,
          f"cost={r['realized_cost']}")
    check("explanations faithful", r["explanation_check"]["faithful"])
    day = {"day": 2, "true_events": [["volume", "S4", -0.3]], "evidence": [
        {"id": "e4", "day": 2, "source": "weighbridge", "kind": "volume", "target": "S4", "magnitude": -0.3}],
        "reports": [{"id": "r4", "day": 2, "sender": "shipper_S4", "kind": "volume", "target": "S4",
                     "magnitude": -0.3, "sent_at": 6.5, "text": "Volume update S4: 70 TEU instead of 100 TEU."}]}
    r = T.run_day(day)
    check("volume DEcrease confirmed by weighbridge is acted on (signed evidence)",
          abs(r["realized_cost"] - 13700.4) < 0.05, f"cost={r['realized_cost']}")
    check("ledger valid after two days", T.ledger.verify_chain() is None, str(T.ledger.stats()))
    print("ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
