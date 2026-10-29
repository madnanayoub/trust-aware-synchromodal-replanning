"""Trust-aware synchromodal replanning: GenAI interpretation + ledger-anchored evidence & reputation + STP MIP.

Daily pipeline (one call to `TrustAwarePlanner.run_day`):
  1. Stakeholders' messages and oracle sensor records are written to the ledger (signed, hash-chained).
  2. The GenAI interpreter turns each message (text only) into a structured claim; guardrails reject
     malformed claims.
  3. Belief fusion per (kind, target):
       sensor evidence on the ledger  -> it decides (0 = claim refuted; otherwise magnitude from evidence,
                                         or the claim's magnitude when they agree within 0.25 h)
       no evidence, trusted sender    -> accept, magnitude de-biased with the sender's learned ratio
       no evidence, uncertain sender  -> uncertain claim with probability p_real
  4. Uncertain claims: choose which to act on by minimising EXPECTED realised cost over the 2^k possible truths
     (stage-1 plan per candidate, executed against each hypothetical truth with the same recourse simulator).
     Only beliefs and reputations are used - never the true events.
  5. The chosen stage-1 plan is solved by the STP MIP (CPLEX), explained in natural language, verified for
     numeric faithfulness, and its hash committed to the ledger.
  6. After execution the realised outcome is posted to the ledger by the port oracle; the reputation contract
     re-scores stakeholders from the ledger (tamper-evident history).
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .evaluate import evaluate
from .execution import execute, T0
from .genai import MockLLM, MockExplainer, fact_sheet, validate_event, verify_explanation
from .instance import Instance
from .ledger import Ledger, canonical
from .model import STPModel, Options
from .plan import Plan
from .scenarios import apply_events
from .trust import ReputationContract
from .uncertainty import render_text

ORACLES = {"AIS": "AIS_oracle", "rail_signalling": "rail_signalling_oracle",
           "terminal_gate": "terminal_gate_oracle", "weighbridge": "weighbridge_oracle"}
VALIDATORS = ["port_authority", "terminal_PoR", "lsp"]


@dataclass
class Config:
    use_evidence: bool = True
    use_reputation: bool = True
    lookahead: bool = True
    max_uncertain: int = 2         # exhaustive expected-cost search over at most this many uncertain claims
    trusted_p: float = 0.85        # p_real at/above which (with >= min_n claims) a sender is trusted
    min_n: int = 5
    accept_p: float = 0.5          # threshold rule for claims beyond the lookahead cap / without lookahead
    tamper_proof: bool = True      # ledger (True) vs mutable database (False) for the reputation history
    tamper_attack: bool = False    # unreliable 'strategic' stakeholders rewrite their past reports each night
    prior: tuple = (3.0, 1.0)      # Beta prior (a0, b0) of the reputation contract
    premium: float = 0.5           # spot-truck premium of the real execution environment
    la_premium: Optional[float] = None   # premium assumed by the lookahead's simulator (None = same as environment)
    backend: str = "auto"


def _round_half(x):
    return max(0.5, round(x * 2) / 2)


class TrustAwarePlanner:
    def __init__(self, base: Instance, base_plan: Plan, stakeholders: List[dict], cfg: Config,
                 interpreter=None, explainer=None):
        self.base, self.base_plan, self.cfg = base, base_plan, cfg
        self.interp = interpreter or MockLLM(base)
        self.explainer = explainer or MockExplainer()
        self.stakeholders = {s["id"]: s for s in stakeholders}
        members = list(self.stakeholders) + list(ORACLES.values()) + VALIDATORS
        self.ledger = Ledger(members, VALIDATORS)
        self.replica = None                    # honest replica held by other nodes (ledger mode)
        owners = {}
        for s in stakeholders:
            for a in s["assets"]:
                owners[a] = s["id"]
        self.contract = ReputationContract(self.interp, owners, prior=tuple(cfg.prior))
        self.base_cost = evaluate(base, base_plan).total
        self.tamper_detected = 0
        self.tamper_flags: Dict[str, int] = {}

    # ------------------------------------------------------------------ belief
    def _claims(self, day: int):
        out = []
        for e in self.ledger.entries("report"):
            if e.payload["day"] != day:
                continue
            p = validate_event(self.interp.interpret(e.payload["text"], e.author), self.base)
            out.append((e.author, p, e.payload["text"]))
        return out

    def _evidence(self, day: int):
        ev = {}
        for e in self.ledger.entries("evidence"):
            if e.payload["day"] == day:
                ev[(e.payload["kind"], e.payload["target"])] = (e.payload["magnitude"], e.payload["source"])
        return ev

    def build_belief(self, day: int):
        cfg = self.cfg
        claims = self._claims(day)
        evidence = self._evidence(day) if cfg.use_evidence else {}
        certain, uncertain, discounted = [], [], []
        seen = set()
        for sender, p, text in claims:
            if p.kind is None:
                discounted.append({"sender": sender, "kind": "unknown", "target": "?", "magnitude": 0,
                                   "reason": "message not understood"})
                continue
            key = (p.kind, p.target)
            seen.add(key)
            rep = self.contract.get(sender)
            if key in evidence:
                em, src = evidence[key]
                if abs(em) <= 1e-9:                     # sensor shows no change (volume changes are signed)
                    discounted.append({"sender": sender, "kind": p.kind, "target": p.target, "magnitude": p.magnitude,
                                       "reason": f"refuted by {src} record"})
                    continue
                if p.kind == "volume":
                    mag, basis = em, f"{src} record"
                elif abs(em - p.magnitude) <= 0.25 + 1e-9:
                    mag, basis = p.magnitude, f"confirmed by {src}"
                else:
                    mag, basis = _round_half(em), f"magnitude from {src}"
                certain.append({"kind": p.kind, "target": p.target, "magnitude": mag, "basis": basis})
                continue
            if not cfg.use_reputation:
                certain.append({"kind": p.kind, "target": p.target, "magnitude": p.magnitude,
                                "basis": f"reported by {sender}"})
                continue
            f = rep.factor(p.kind)
            mag = p.magnitude * f
            mag = round(mag * 20) / 20 if p.kind == "volume" else _round_half(mag)
            pk, qk = rep.p_kind(p.kind), rep.q_kind(p.kind)
            tag = f"{sender}, p_real {pk:.2f}" + (f", de-biased by factor {f:.2f}" if abs(f - 1) > 1e-6 else "")
            item = {"kind": p.kind, "target": p.target, "magnitude": mag, "claimed": p.magnitude,
                    "sender": sender, "p": pk, "q": qk, "basis": tag}
            if pk >= cfg.trusted_p and rep.n_kind(p.kind) >= cfg.min_n:
                certain.append(item)
            else:
                uncertain.append(item)
        # sensor evidence of events nobody reported (omissions) - acted upon directly
        if cfg.use_evidence:
            for (k, t), (em, src) in evidence.items():
                if (k, t) not in seen and abs(em) > 1e-9:
                    certain.append({"kind": k, "target": t, "magnitude": em if k == "volume" else _round_half(em),
                                    "basis": f"unreported, detected by {src}"})
        return certain, uncertain, discounted

    # ------------------------------------------------------------------ decision under uncertainty
    @staticmethod
    def _ev(items):
        return [[i["kind"], i["target"], i["magnitude"]] for i in items]

    def _stage1(self, belief_events):
        if not belief_events:
            return self.base_plan
        bi, un = apply_events(self.base, belief_events)
        return STPModel(bi, Options(backend=self.cfg.backend, anchor_plan=self.base_plan, allow_unserved=True,
                                    unavailable_lcs=un)).solve().plan

    @staticmethod
    def _options(u):
        """Possible truths of an uncertain claim with probabilities: absent, as claimed, as de-biased."""
        opts = [(None, 1 - u["p"])]
        if abs(u["magnitude"] - u["claimed"]) < 1e-9:
            opts.append((u["claimed"], u["p"]))
        else:
            opts += [(u["claimed"], u["p"] * u["q"]), (u["magnitude"], u["p"] * (1 - u["q"]))]
        return opts

    def decide(self, certain, uncertain):
        """Choose, for each uncertain claim, to ignore it, act on the claimed or on the de-biased magnitude,
        minimising expected realised cost over the joint distribution of possible truths."""
        cfg = self.cfg
        uncertain = sorted(uncertain, key=lambda u: -u["p"])
        if not cfg.lookahead:
            acc = [u for u in uncertain if u["p"] >= cfg.accept_p]
            return acc, [u for u in uncertain if u not in acc], None
        head, tail = uncertain[:cfg.max_uncertain], uncertain[cfg.max_uncertain:]
        acc_tail = [u for u in tail if u["p"] >= cfg.accept_p]
        C = self._ev(certain + acc_tail)
        if not head:
            return acc_tail, [u for u in tail if u not in acc_tail], None
        per_claim = [self._options(u) for u in head]
        hyps = list(itertools.product(*per_claim))
        # actions per claim: act on claimed / act on de-biased (if different) / ignore; ties -> use the claim
        acts = [[o[0] for o in opts[::-1] if o[0] is not None] + [None] for opts in per_claim]
        best, table = None, []
        for cand in itertools.product(*acts):
            bel = C + [[u["kind"], u["target"], m] for u, m in zip(head, cand) if m is not None]
            p1 = self._stage1(bel)
            exp = 0.0
            for h in hyps:
                pr = 1.0
                for _, w in h:
                    pr *= w
                if pr < 1e-9:
                    continue
                truth = C + [[u["kind"], u["target"], m] for u, (m, _) in zip(head, h) if m is not None]
                la_prem = cfg.premium if cfg.la_premium is None else cfg.la_premium
                exp += pr * execute(self.base, self.base_plan, truth, bel, "lookahead", backend=cfg.backend,
                                    premium=la_prem, stage1_plan=p1).realized_cost
            table.append({"act": [f"{u['target']}={m}" for u, m in zip(head, cand)], "expected_cost": round(exp, 1)})
            if best is None or exp < best[0] - 1e-6:
                best = (exp, cand)
        accepted = []
        for u, m in zip(head, best[1]):
            if m is not None:
                v = dict(u)
                v["magnitude"] = m
                if abs(m - u["claimed"]) < 1e-9 and abs(u["magnitude"] - m) > 1e-9:
                    v["basis"] = u["basis"].split(", de-biased")[0] + ", claimed magnitude kept"
                accepted.append(v)
        accepted += acc_tail
        acc_keys = {(a["kind"], a["target"]) for a in accepted}
        rej = [u for u in uncertain if (u["kind"], u["target"]) not in acc_keys]
        return accepted, rej, table

    # ------------------------------------------------------------------ day
    def run_day(self, day: dict) -> dict:
        d = day["day"]
        for r in day["reports"]:
            self.ledger.submit("report", r["sender"], {"day": d, "id": r["id"], "text": r["text"],
                                                       "sent_at": r["sent_at"]})
        for e in day["evidence"]:
            self.ledger.submit("evidence", ORACLES[e["source"]], {"day": d, "kind": e["kind"], "target": e["target"],
                                                                  "magnitude": e["magnitude"], "source": e["source"]})
        self.ledger.commit(timestamp=d * 24 + T0 - 0.01)

        certain, uncertain, discounted = self.build_belief(d)
        accepted, rejected, table = self.decide(certain, uncertain)
        believed = certain + accepted
        for u in rejected:
            discounted.append({"sender": u["sender"], "kind": u["kind"], "target": u["target"],
                               "magnitude": u["claimed"],
                               "reason": f"low expected value of acting, p_real {u['p']:.2f}"})
        belief_events = self._ev(believed)
        p1 = self._stage1(belief_events)
        bi, _ = apply_events(self.base, belief_events)
        facts = fact_sheet(self.base, self.base_plan, p1, believed, discounted, evaluate(bi, p1).total, self.base_cost)
        text = self.explainer.explain(facts)
        check = verify_explanation(text, facts)
        self.ledger.submit("plan", "lsp", {"day": d, "belief": belief_events,
                                           "plan_hash": hashlib.sha256(canonical(p1.to_dict())).hexdigest(),
                                           "explanation_hash": hashlib.sha256(text.encode()).hexdigest()})
        self.ledger.commit(timestamp=d * 24 + T0)

        out = execute(self.base, self.base_plan, day["true_events"], belief_events, "ours", backend=self.cfg.backend,
                      premium=self.cfg.premium, stage1_plan=p1)

        # end of day: realised outcome by the port oracle, then (optionally) a tampering attack, then reputation
        self.ledger.submit("outcome", "port_authority", {"day": d, "events": day["true_events"]})
        self.ledger.commit(timestamp=d * 24 + 23.9)
        if self.replica is None or self.cfg.tamper_proof:
            self.replica = copy.deepcopy(self.ledger.chain)
        if self.cfg.tamper_attack:
            self._attack(day)
        self.contract.update_from_ledger(self.ledger, self.tamper_flags)
        return {"day": d, "realized_cost": out.realized_cost, "belief": belief_events,
                "n_certain": len(certain), "n_uncertain": len(uncertain), "n_accepted": len(accepted),
                "n_discounted": len(discounted),
                "n_refuted": sum("refuted by" in d["reason"] for d in discounted),
                "n_rejected": sum(d["reason"].startswith("low expected value") for d in discounted),
                "n_not_understood": sum(d["reason"] == "message not understood" for d in discounted),
                "lookahead": table, "explanation": text,
                "explanation_check": check, "outcome": out.__dict__}

    # ------------------------------------------------------------------ attack
    def _attack(self, day: dict):
        """Strategic stakeholders rewrite their own past messages of today so that they look truthful."""
        attackers = {s for s, v in self.stakeholders.items() if v["profile"] == "strategic"}
        if not attackers:
            return
        truth = {(k, t): m for k, t, m in day["true_events"]}
        changed = False
        for b in self.ledger.chain:
            for e in b.entries:
                if e.type != "report" or e.author not in attackers or e.payload["day"] != day["day"]:
                    continue
                p = self.interp.interpret(e.payload["text"], e.author)
                if p.kind is None:
                    continue
                tm = truth.get((p.kind, p.target))
                if tm is None:
                    e.payload["text"] = f"No disturbance expected for {p.target}."   # erase a false alarm
                elif abs(tm - (p.magnitude or 0)) > 1e-6:
                    e.payload["text"] = render_text(p.kind, p.target, tm, e.author, self.base,
                                                    __import__("random").Random(0))  # 'correct' an exaggeration
                else:
                    continue
                self.ledger.sign(e.author, e)           # the author re-signs its edited message with its own key
                changed = True
        if not changed:
            return
        if self.cfg.tamper_proof:
            bad = self.ledger.verify_chain()
            if bad is not None:                         # detected by any node: rejected, history restored
                self.tamper_detected += 1
                for a in attackers:
                    self.tamper_flags[a] = self.tamper_flags.get(a, 0) + 1
                self.ledger.chain = copy.deepcopy(self.replica)
        # mutable database: the rewrite silently succeeds and feeds the reputation scores
