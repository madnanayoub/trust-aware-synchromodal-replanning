"""Offline test of the real-LLM path (no API key, no cost). Run: python -m scripts.test_llm_plumbing

Simulates three LLMs behind the same `complete(system, user)` interface the real adapters use:
  good          answers correctly (JSON)                -> pipeline, guardrail and cache work end to end
  bad           malformed JSON / invented ids / absurd  -> every answer must become 'not understood', never wrong
  hallucinating explainer adds a number not in the facts -> the faithfulness verifier must flag it
"""
import json
import os
import re
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.genai import MockLLM, LLMClient, LLMExplainer, CachedInterpreter, validate_event, verify_explanation
import scripts.eval_interpreter as EI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {info}")


inst = rotterdam.build_instance()
oracle = MockLLM(inst)
calls = {"n": 0}


def good(system, user):
    calls["n"] += 1
    msg = re.search(r"Message: (.*)$", user, re.S).group(1)
    sender = re.search(r"Sender: (.*)\n", user).group(1)
    p = oracle.interpret(msg, sender)
    return json.dumps({"kind": p.kind, "target": p.target, "magnitude": p.magnitude, "confidence": 0.9})


def bad(system, user):
    calls["n"] += 1
    return ["not json at all", '{"kind": "lcs_delay", "target": "v9999", "magnitude": 1}',
            '{"kind": "release", "target": "S1", "magnitude": 99}', '{"kind": "teleport"}'][calls["n"] % 4]


if __name__ == "__main__":
    cache_dir = f"{ROOT}/results/llm_cache"
    for f in ("interpret_anthropic_fake-good.json", "interpret_anthropic_fake-bad.json"):
        if os.path.exists(f"{cache_dir}/{f}"):
            os.remove(f"{cache_dir}/{f}")
    # good LLM: same answers as the mock on held-out + 100 generated, then a free re-run from cache
    calls["n"] = 0
    r1 = EI.main(["--llm", "anthropic", "--model", "fake-good", "--max-generated", "100"], complete=good)
    n1 = calls["n"]
    r2 = EI.main(["--llm", "anthropic", "--model", "fake-good", "--max-generated", "100"], complete=good)
    check("good LLM reaches the mock's accuracy through LLMClient + guardrail",
          r1["heldout"]["correct"] == 11 and r1["generated"]["accuracy_pct"] == 100.0)
    check("cache: second run makes no API calls", calls["n"] == n1 and r2["api_calls"] == 0,
          f"(first run {n1} calls)")
    # bad LLM: nothing may be acted on
    calls["n"] = 0
    r3 = EI.main(["--llm", "anthropic", "--model", "fake-bad", "--heldout-only"], complete=bad)
    check("bad LLM output never becomes a wrong claim", r3["heldout"].get("wrong", 0) == 0
          and r3["heldout"].get("not_understood", 0) == 40)
    # hallucinating explainer
    facts = {"believed": [{"kind": "release", "target": "S4", "magnitude": 2.0, "basis": "terminal_gate"}],
             "expected_cost": 17261.8, "cost_change": 1300.9, "extra_trucks": 30, "trucks": 100}
    honest = LLMExplainer(lambda s, u: "S4 is released 2 h late; 30 extra trucks; cost 17,261.8 EUR.").explain(facts)
    halluc = LLMExplainer(lambda s, u: "S4 is released 2 h late; this saves 4,000 EUR.").explain(facts)
    check("verifier accepts a faithful LLM explanation", verify_explanation(honest, facts)["faithful"])
    check("verifier rejects a hallucinated number", not verify_explanation(halluc, facts)["faithful"])
    for f in ("interpret_anthropic_fake-good.json", "interpret_anthropic_fake-bad.json"):
        if os.path.exists(f"{cache_dir}/{f}"):
            os.remove(f"{cache_dir}/{f}")
    for f in ("interpreter_anthropic_fake-good.json", "interpreter_anthropic_fake-bad.json"):
        if os.path.exists(f"{ROOT}/results/{f}"):
            os.remove(f"{ROOT}/results/{f}")
    print("ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
