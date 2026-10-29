"""Reputation 'smart contract': stakeholder trust computed only from what is on the ledger.

For every stakeholder and every past day with a posted outcome:
  * each report is interpreted (same GenAI interpreter as the planner) and matched to the day's realised events:
      real      the claimed (kind, target) really happened   -> success
      false     it did not happen                            -> failure
    and for real claims the magnitude ratio true/claimed is stored (exaggeration calibration);
  * true events of the stakeholder's own assets that were never reported are counted as omissions.
Outputs per stakeholder:
  p_real   posterior mean of Beta(a0 + successes, b0 + failures) = probability that a claim is real
  p_kind   the same per claim type (release / lcs_delay / volume), shrunk towards p_real with weight SHRINK
           (a stakeholder may be reliable about volumes but not about delays)
  q_kind   probability that a real claim of this type had the right magnitude (|true - claimed| <= 0.25)
  factor   median of recent true/claimed ratios (1.0 until enough evidence) - used to de-bias magnitudes
  n        number of scored claims
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

PRIOR_A, PRIOR_B = 3.0, 1.0       # mild prior that stakeholders are honest (p_real = 0.75 at cold start)
WINDOW = 15                       # recent ratios used for de-biasing
SHRINK = 2.0                      # pseudo-observations pulling the per-type estimate towards the pooled one


@dataclass
class Reputation:
    stakeholder: str
    real: int = 0
    false: int = 0
    omissions: int = 0
    ratios: Dict[str, List[float]] = field(default_factory=lambda: defaultdict(list))
    real_k: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    false_k: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    accurate_k: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tamper_flags: int = 0
    a0: float = PRIOR_A
    b0: float = PRIOR_B

    @property
    def n(self):
        return self.real + self.false

    @property
    def p_real(self):
        return (self.a0 + self.real) / (self.a0 + self.b0 + self.real + self.false)

    def n_kind(self, kind):
        return self.real_k[kind] + self.false_k[kind]

    def p_kind(self, kind: str) -> float:
        return (SHRINK * self.p_real + self.real_k[kind]) / (SHRINK + self.n_kind(kind))

    def q_kind(self, kind: str) -> float:
        return (1.0 + self.accurate_k[kind]) / (2.0 + self.real_k[kind])

    def factor(self, kind: str) -> float:
        r = self.ratios.get(kind, [])[-WINDOW:]
        return statistics.median(r) if len(r) >= 2 else 1.0


class ReputationContract:
    def __init__(self, interpreter, owners: Dict[str, str], prior=(PRIOR_A, PRIOR_B)):
        """owners: asset id (shipment or service) -> responsible stakeholder."""
        self.prior = prior
        self.interp = interpreter
        self.owners = owners
        self.rep: Dict[str, Reputation] = {}
        self.scored_days = set()

    def get(self, sid: str) -> Reputation:
        return self.rep.setdefault(sid, Reputation(sid, a0=self.prior[0], b0=self.prior[1]))

    def update_from_ledger(self, ledger, tamper_flags: Dict[str, int] = None):
        """Score every day whose outcome is on the ledger and which was not scored yet."""
        reports = defaultdict(list)
        for e in ledger.entries("report"):
            reports[e.payload["day"]].append(e)
        for e in ledger.entries("outcome"):
            day = e.payload["day"]
            if day in self.scored_days:
                continue
            self.scored_days.add(day)
            truth = {(k, t): m for k, t, m in e.payload["events"]}
            claimed = set()
            for r in reports.get(day, []):
                p = self.interp.interpret(r.payload["text"], r.author)
                if p.kind is None:
                    continue
                claimed.add((p.kind, p.target))
                rep = self.get(r.author)
                if (p.kind, p.target) in truth:
                    rep.real += 1
                    rep.real_k[p.kind] += 1
                    tm = truth[(p.kind, p.target)]
                    if p.magnitude:
                        rep.ratios[p.kind].append(tm / p.magnitude)
                        rep.accurate_k[p.kind] += abs(tm - p.magnitude) <= 0.25 + 1e-9
                else:
                    rep.false += 1
                    rep.false_k[p.kind] += 1
            for (k, t), m in truth.items():
                if (k, t) not in claimed and t in self.owners:
                    self.get(self.owners[t]).omissions += 1
        for sid, n in (tamper_flags or {}).items():
            self.get(sid).tamper_flags = n

    def snapshot(self) -> Dict[str, dict]:
        return {s: {"p_real": round(r.p_real, 3), "n": r.n, "real": r.real, "false": r.false,
                    "omissions": r.omissions, "factor_lcs": round(r.factor("lcs_delay"), 3),
                    "factor_release": round(r.factor("release"), 3), "tamper_flags": r.tamper_flags}
                for s, r in sorted(self.rep.items())}
