"""Step 5: trust-aware approach (GenAI + ledger + reputation + STP) on the step-4 streams, with ablations.

    python -m scripts.run_step5 --backend cplex --workers 2
Variants:
  ours              evidence + reputation (+ de-biasing) + expected-cost lookahead, tamper-proof ledger
  no_evidence       reputation + lookahead only (no sensor records)
  no_reputation     sensor evidence, every other claim trusted as-is
  no_lookahead      threshold rule (act if p_real >= 0.5) instead of expected-cost search
  attack_ledger     'ours' while strategic stakeholders try to rewrite their history every night (ledger)
  attack_mutable    same attack against a mutable database (rewrites succeed)
"""
import argparse
import glob
import json
import os
import sys
import time
from multiprocessing import Pool

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VARIANTS = {
    "ours": {},
    "no_evidence": {"use_evidence": False},
    "no_reputation": {"use_reputation": False},
    "no_lookahead": {"lookahead": False},
    "attack_ledger": {"tamper_attack": True, "tamper_proof": True},
    "attack_mutable": {"tamper_attack": True, "tamper_proof": False},
}


def job(args):
    path, variant, backend, llm, model, llm_explain = args
    from stp import rotterdam
    from stp.ours import TrustAwarePlanner, Config
    stream = json.load(open(path, encoding="utf-8"))
    B, P = rotterdam.build_instance(), rotterdam.base_plan()
    interp = expl = None
    if llm != "mock":
        from scripts.eval_interpreter import build_interpreter
        interp = build_interpreter(B, llm, model)
        if llm_explain:
            from stp.genai import LLMExplainer, CachedExplainer
            from stp.llm_backends import make_complete
            safe = model.replace("/", "_").replace(":", "_")
            expl = CachedExplainer(LLMExplainer(make_complete(llm, model, json_mode=False)),
                                   f"{ROOT}/results/llm_cache/explain_{llm}_{safe}.json")
    T = TrustAwarePlanner(B, P, stream["stakeholders"], Config(backend=backend, **VARIANTS[variant]),
                          interpreter=interp, explainer=expl)
    days = []
    t0 = time.time()
    try:
        return _run_days(T, stream, variant, days, t0,
                         {"interpreter": "mock" if llm == "mock" else f"{llm}:{model}", "llm_explain": bool(llm_explain)})
    finally:
        if hasattr(interp, "save"):
            interp.save()


def _run_days(T, stream, variant, days, t0, meta):
    for d in stream["days"]:
        r = T.run_day(d)
        days.append({"day": r["day"], "realized_cost": r["realized_cost"], "n_certain": r["n_certain"],
                     "n_uncertain": r["n_uncertain"], "n_accepted": r["n_accepted"],
                     "n_discounted": r["n_discounted"], "faithful": r["explanation_check"]["faithful"],
                     "explanation": r["explanation"], "spot": r["outcome"]["spot_trucks"],
                     "unused": r["outcome"]["unused_booked"], "late_teuh": r["outcome"]["late_teuh"],
                     "feasible": r["outcome"]["feasible"]})
    return {"rho": stream["dishonesty"], "seed": stream["seed"], "variant": variant,
            "profiles": {s["id"]: s["profile"] for s in stream["stakeholders"]},
            "reputation": T.contract.snapshot(), "ledger": T.ledger.stats(),
            "tamper_detected": T.tamper_detected, "seconds": round(time.time() - t0, 1), "days": days,
            **meta}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    ap.add_argument("--seeds", type=int, nargs="*", default=[11, 22, 33], help="dev: 11 22 33, test: 44 55 66")
    ap.add_argument("--out", default="results/step5_runs.json")
    ap.add_argument("--llm", default="mock", choices=["mock", "anthropic", "openai"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--llm-explain", action="store_true", help="also generate explanations with the LLM")
    args = ap.parse_args()
    if args.llm != "mock" and args.workers != 1:
        print("LLM runs use one worker (shared answer cache).")
        args.workers = 1
    streams = sorted(f for f in glob.glob(f"{ROOT}/data/unreliable/stream_*.json")
                     if int(f.rsplit("seed", 1)[1].split(".")[0]) in args.seeds)
    jobs = [(s, v, args.backend, args.llm, args.model, args.llm_explain) for v in args.variants for s in streams]
    out = []
    t0 = time.time()
    pool = Pool(args.workers) if args.workers > 1 else None
    for r in (pool.imap_unordered(job, jobs) if pool else map(job, jobs)):
        out.append(r)
        print(f"{r['variant']:15s} rho={r['rho']:.2f} seed={r['seed']} "
              f"{sum(d['realized_cost'] for d in r['days']):,.1f} ({r['seconds']}s, total {time.time() - t0:.0f}s)",
              flush=True)
        json.dump(out, open(os.path.join(ROOT, args.out), "w", encoding="utf-8"))
    if pool:
        pool.close()


if __name__ == "__main__":
    main()
