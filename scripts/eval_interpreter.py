"""Accuracy of the GenAI message interpreter on (a) generated reports and (b) 40 held-out messages.

    python -m scripts.eval_interpreter                                            # mock (rule-based)
    python -m scripts.eval_interpreter --llm anthropic --model claude-haiku-5-5 --heldout-only   # 40 API calls
    python -m scripts.eval_interpreter --llm anthropic --model claude-haiku-5-5   # + all ~830 generated messages
    python -m scripts.eval_interpreter --llm openai --model <your-model>

Answers are cached in results/llm_cache/ (each message is sent once; re-runs are free). Correct = kind, target and
magnitude (±0.05) all match. 'Not understood' is reported separately from 'wrong': the planner never acts on a
claim it did not understand, but it may act on a wrong one.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stp import rotterdam
from stp.genai import MockLLM, LLMClient, CachedInterpreter, validate_event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_interpreter(inst, llm, model, complete=None):
    if llm == "mock":
        return MockLLM(inst)
    if complete is None:
        from stp.llm_backends import make_complete
        complete = make_complete(llm, model)
    inner = LLMClient(inst, complete)
    inner.name = f"{llm}:{model}"
    safe = model.replace("/", "_").replace(":", "_")
    return CachedInterpreter(inner, f"{ROOT}/results/llm_cache/interpret_{llm}_{safe}.json")


def score(interp, items, inst):
    c = Counter()
    errors = []
    for k, it in enumerate(items, 1):
        p = validate_event(interp.interpret(it["text"], it["sender"]), inst)
        good = p.kind == it["kind"] and p.target == it["target"] and p.magnitude is not None \
            and abs(p.magnitude - it["magnitude"]) < 0.051
        c["correct" if good else ("not_understood" if p.kind is None else "wrong")] += 1
        if not good:
            errors.append({"text": it["text"], "expected": [it["kind"], it["target"], it["magnitude"]],
                           "got": [p.kind, p.target, p.magnitude], "note": p.rationale[:120]})
        if k % 50 == 0:
            print(f"   {k}/{len(items)} ...", flush=True)
    n = sum(c.values())
    return {"n": n, **c, "accuracy_pct": round(100 * c["correct"] / n, 1) if n else None, "errors": errors}


def generated_items(seeds=None):
    """Unique (text, sender) messages of the Rotterdam streams (optionally only the given seeds)."""
    seen, out = set(), []
    for f in sorted(glob.glob(f"{ROOT}/data/unreliable/*.json")):
        if seeds and int(f.rsplit("seed", 1)[1].split(".")[0]) not in seeds:
            continue
        for d in json.load(open(f, encoding="utf-8"))["days"]:
            for r in d["reports"]:
                key = (r["text"], r["sender"])
                if key not in seen:                    # unique messages only (the stream repeats templates)
                    seen.add(key)
                    out.append({"text": r["text"], "sender": r["sender"], "kind": r["kind"], "target": r["target"],
                                "magnitude": r["magnitude"]})
    return out


def main(argv=None, complete=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="mock", choices=["mock", "anthropic", "openai"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--heldout-only", action="store_true")
    ap.add_argument("--max-generated", type=int, default=0, help="limit generated messages; 0 = all unique")
    ap.add_argument("--seeds", type=int, nargs="*", default=[11, 22, 33, 44, 55, 66],
                    help="streams whose messages are scored (default: development + original test streams)")
    a = ap.parse_args(argv)
    inst = rotterdam.build_instance()
    interp = build_interpreter(inst, a.llm, a.model, complete)
    held = json.load(open(f"{ROOT}/data/heldout_messages.json", encoding="utf-8"))
    res = {"interpreter": "mock-rule-based" if a.llm == "mock" else f"{a.llm}:{a.model}"}
    print("held-out messages ...")
    res["heldout"] = score(interp, held, inst)
    if not a.heldout_only:
        gen = generated_items(set(a.seeds))
        if a.max_generated:
            gen = gen[:a.max_generated]
        print(f"generated messages ({len(gen)} unique) ...")
        res["generated"] = score(interp, gen, inst)
    if hasattr(interp, "save"):
        interp.save()
        res["api_calls"] = interp.misses
        res["cache_hits"] = interp.hits
    os.makedirs(f"{ROOT}/results", exist_ok=True)
    tag = "mock" if a.llm == "mock" else f"{a.llm}_{a.model.replace('/', '_').replace(':', '_')}"
    json.dump(res, open(f"{ROOT}/results/interpreter_{tag}.json", "w", encoding="utf-8"), indent=1)
    print(f"\ninterpreter: {res['interpreter']}")
    for k in ("generated", "heldout"):
        if k in res:
            r = res[k]
            print(f"{k:10s} n={r['n']:4d} correct={r.get('correct', 0):4d} ({r['accuracy_pct']}%) "
                  f"not_understood={r.get('not_understood', 0)} wrong={r.get('wrong', 0)}")
    if "api_calls" in res:
        print(f"API calls this run: {res['api_calls']} (cache hits {res['cache_hits']})")
    print(f"saved results/interpreter_{tag}.json")
    return res


if __name__ == "__main__":
    main()
