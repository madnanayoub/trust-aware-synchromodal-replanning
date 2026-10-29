"""Export ledger traces of the trust-aware framework for replay on a Besu (or any EVM) network.

For each (rho, seed) the framework is run as in the paper. Everything it writes to its ledger is exported per day:
signed reports (text hash plus the interpreted claim), oracle sensor records, plan and explanation hashes, and the
realised outcome. The reputation state after every day is exported as well, so that the on-chain contract can be
checked against the reference implementation day by day.

    python -m scripts.export_chain_trace --rho 0.5 1.0 --seeds 44 --backend cplex
    -> results/chain_traces/trace_rho0.50_seed44.json, ...
"""
import argparse
import hashlib
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KINDS = ("release", "lcs_delay", "volume")


def rep_state(contract, ids):
    out = {}
    for sid in ids:
        r = contract.get(sid)
        out[sid] = {"real": r.real, "false": r.false, "omissions": r.omissions,
                    "p_real": r.p_real,
                    "real_k": {k: r.real_k[k] for k in KINDS}, "false_k": {k: r.false_k[k] for k in KINDS},
                    "accurate_k": {k: r.accurate_k[k] for k in KINDS},
                    "p_kind": {k: r.p_kind(k) for k in KINDS}, "q_kind": {k: r.q_kind(k) for k in KINDS},
                    "factor": {k: r.factor(k) for k in KINDS}}
    return out


def export(rho, seed, backend, out_dir):
    from stp import rotterdam
    from stp.ours import TrustAwarePlanner, Config, ORACLES
    path = f"{ROOT}/data/unreliable/stream_rho{rho:.2f}_seed{seed}.json"
    stream = json.load(open(path, encoding="utf-8"))
    B, P = rotterdam.build_instance(), rotterdam.base_plan()
    T = TrustAwarePlanner(B, P, stream["stakeholders"], Config(backend=backend))
    ids = [s["id"] for s in stream["stakeholders"]]
    days = []
    t0 = time.time()
    for d in stream["days"]:
        T.run_day(d)
        dd = d["day"]
        reports = []
        for e in T.ledger.entries("report"):
            if e.payload["day"] != dd:
                continue
            p = T.interp.interpret(e.payload["text"], e.author)
            reports.append({"id": e.payload["id"], "sender": e.author,
                            "text_hash": hashlib.sha256(e.payload["text"].encode()).hexdigest(),
                            "claim": None if p.kind is None else
                            {"kind": p.kind, "target": p.target, "magnitude": p.magnitude}})
        evidence = [{"oracle": ORACLES[e.payload["source"]], "kind": e.payload["kind"], "target": e.payload["target"],
                     "magnitude": e.payload["magnitude"]}
                    for e in T.ledger.entries("evidence") if e.payload["day"] == dd]
        plan = [e.payload for e in T.ledger.entries("plan") if e.payload["day"] == dd][-1]
        outcome = [e.payload for e in T.ledger.entries("outcome") if e.payload["day"] == dd][-1]
        days.append({"day": dd, "reports": reports, "evidence": evidence,
                     "plan_hash": plan["plan_hash"], "explanation_hash": plan["explanation_hash"],
                     "outcome": outcome["events"], "reputation_after": rep_state(T.contract, ids)})
    trace = {"rho": rho, "seed": seed, "prior": list(T.contract.prior), "shrink": 2.0, "window": 15,
             "stakeholders": stream["stakeholders"], "oracles": sorted(set(ORACLES.values())),
             "days": days, "python_ledger": T.ledger.stats(), "seconds": round(time.time() - t0, 1)}
    os.makedirs(out_dir, exist_ok=True)
    p = f"{out_dir}/trace_rho{rho:.2f}_seed{seed}.json"
    json.dump(trace, open(p, "w", encoding="utf-8"), indent=1)
    n = sum(len(d["reports"]) for d in days)
    print(f"{p}: {len(days)} days, {n} reports, {sum(len(d['evidence']) for d in days)} sensor records "
          f"({trace['seconds']} s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rho", type=float, nargs="+", default=[0.5, 1.0])
    ap.add_argument("--seeds", type=int, nargs="+", default=[44])
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--out", default=f"{ROOT}/results/chain_traces")
    a = ap.parse_args()
    for rho in a.rho:
        for s in a.seeds:
            export(rho, s, a.backend, a.out)


if __name__ == "__main__":
    main()
