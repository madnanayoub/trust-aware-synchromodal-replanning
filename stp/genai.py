"""Generative-AI layer: (1) interpret free-text disturbance messages, (2) explain re-planning decisions.

Two interchangeable back-ends behind the same interface:
  MockLLM    deterministic rule-based stand-in (regex + number words). Reproducible, no API cost; used for
             all experiments in this repository.
  LLMClient  wraps any chat-completion function `complete(system, user) -> str` (e.g. the Anthropic Messages
             API). Prompts below; outputs are parsed as JSON and then go through exactly the same validation
             (`validate_event`) and the same explanation verifier (`verify_explanation`), so a real model can be
             dropped in without changing the planner or the evaluation.

The interpreter only sees: message text, sender id, and public network data (shipments' origin/volume/release
time, services' itineraries and timetable). It never sees the generator's structured fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional

from .instance import Instance


@dataclass
class ParsedEvent:
    kind: Optional[str]          # release | lcs_delay | volume | None (not understood)
    target: Optional[str]
    magnitude: Optional[float]   # hours, or volume fraction
    confidence: float
    rationale: str = ""


# =================================================================================== interpretation
_NUM_WORDS = {"half an hour": 0.5, "an hour and a half": 1.5, "two and a half hours": 2.5,
              "four and a half hours": 4.5, "an hour": 1.0, "one hour": 1.0, "two hours": 2.0,
              "three hours": 3.0, "four hours": 4.0}
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*(\d{1,2})?\b|(\d+(?:\.\d+)?)\s*(?:minutes|min)\b", re.I)


def _duration(text: str) -> Optional[float]:
    t = text.lower()
    for phrase in sorted(_NUM_WORDS, key=len, reverse=True):
        if phrase in t:
            return _NUM_WORDS[phrase]
    for m in _DUR.finditer(t):
        if m.group(1):
            h = float(m.group(1))
            if m.group(2):                     # "1h30"
                h += int(m.group(2)) / 60
            return h
        if m.group(3):
            return float(m.group(3)) / 60
    return None


def _clock(text: str) -> Optional[float]:
    m = re.search(r"(?:before|approx\.?|at|around)\s*(\d{1,2}(?:\.\d+)?):00", text.lower())
    return float(m.group(1)) if m else None


class MockLLM:
    """Rule-based interpreter with the same contract as an LLM interpreter."""
    name = "mock-rule-based"

    def __init__(self, inst: Instance):
        self.inst = inst
        self.ship = {s.id: s for s in inst.shipments}
        self.svc = {v.id: v for v in inst.services}

    def interpret(self, text: str, sender: str) -> ParsedEvent:
        t = text
        svc = next((v for v in self.svc if re.search(rf"\b{v}\b", t)), None)
        ship = next((s for s in sorted(self.ship, key=len, reverse=True) if re.search(rf"\b{s}\b", t)), None)
        low = t.lower()
        # ---- volume change: "<n> TEU instead of", "now <n> TEU", "amended to <n> TEU"
        mv = re.search(r"(\d+(?:\.\d+)?)\s*teu\s+instead of|now\s+(\d+(?:\.\d+)?)\s*teu|amended to\s+(\d+(?:\.\d+)?)\s*teu",
                       low)
        if ship and mv and ("volume" in low or "amended" in low or "increase" in low or "decrease" in low):
            new = float(next(g for g in mv.groups() if g))
            n = self.ship[ship].n
            return ParsedEvent("volume", ship, round((new - n) / n, 4), 0.95, f"new volume {new:g} vs booked {n:g}")
        # ---- LCS delay
        if svc and any(w in low for w in ("depart", "delay", "late", "behind schedule", "out of")):
            d = _duration(t)
            if d is None:
                c = _clock(t)
                if c is not None:
                    v = self.svc[svc]
                    d = c - (v.legs[0].planned_dep - v.load_time)
            if d is not None and d > 0:
                return ParsedEvent("lcs_delay", svc, round(d * 2) / 2, 0.9, "service delay")
        # ---- release delay
        if ship and any(w in low for w in ("release", "not available", "delayed", "postponed", "later")):
            d = _duration(t)
            if d is None:
                c = _clock(t)
                if c is not None:
                    d = c - self.ship[ship].alpha
            if d is not None and d > 0:
                return ParsedEvent("release", ship, round(d * 2) / 2, 0.9, "release delay")
        return ParsedEvent(None, None, None, 0.0, "not understood")


INTERPRET_SYSTEM = (
    "You extract freight-transport disturbances from operator messages. Answer with ONE JSON object: "
    '{"kind": "release"|"lcs_delay"|"volume"|null, "target": <shipment id or service id>, '
    '"magnitude": <hours of delay, or volume change as a fraction e.g. 0.2 for +20%>, "confidence": 0..1}. '
    "Use only ids that appear in the network description. Do not guess; use null if unclear.")


class LLMClient:
    """Adapter for a real LLM. `complete(system, user) -> str` must return the model's text."""
    name = "llm"

    def __init__(self, inst: Instance, complete: Callable[[str, str], str]):
        self.inst, self.complete = inst, complete

    def _network(self):
        return {"shipments": {s.id: {"origin": s.o, "destination": s.d, "TEU": s.n, "release": s.alpha}
                              for s in self.inst.shipments},
                "services": {v.id: {"mode": v.mode, "route": v.itinerary,
                                    "start_loading": (v.legs[0].planned_dep or 0) - v.load_time}
                             for v in self.inst.services}}

    def interpret(self, text: str, sender: str) -> ParsedEvent:
        user = f"Network: {json.dumps(self._network())}\nSender: {sender}\nMessage: {text}"
        try:
            d = json.loads(self.complete(INTERPRET_SYSTEM, user))
            return validate_event(ParsedEvent(d.get("kind"), d.get("target"), d.get("magnitude"),
                                              float(d.get("confidence", 0.5)), "llm"), self.inst)
        except Exception as ex:   # malformed output -> treated as not understood
            return ParsedEvent(None, None, None, 0.0, f"llm error: {ex}")


def validate_event(p: ParsedEvent, inst: Instance) -> ParsedEvent:
    """Guardrail applied to every interpreter: ids must exist, magnitudes must be in a physical range."""
    ships, svcs = {s.id for s in inst.shipments}, {v.id for v in inst.services}
    ok = (p.kind == "release" and p.target in ships and p.magnitude and 0 < p.magnitude <= 12) or \
         (p.kind == "lcs_delay" and p.target in svcs and p.magnitude and 0 < p.magnitude <= 12) or \
         (p.kind == "volume" and p.target in ships and p.magnitude is not None and -1 < p.magnitude <= 2)
    return p if ok else ParsedEvent(None, None, None, 0.0, f"rejected by guardrail: {asdict(p)}")


# =================================================================================== explanation
def fact_sheet(inst: Instance, base_plan, plan, belief: List[dict], discounted: List[dict],
               expected_cost: float, base_cost: float) -> dict:
    """Structured, verifiable facts about a re-plan (the only input of the explainer)."""
    moves = []
    for s in inst.shipments:
        def modes(p):
            out = {}
            for (sid, k), fl in p.lcs_flow.items():
                v = inst.service(sid)
                if v.legs[k].i == s.o and fl.get(s.id):
                    out[f"{v.mode} {sid}"] = out.get(f"{v.mode} {sid}", 0) + fl[s.id]
            for t in p.trucks:
                if t.shipment == s.id and inst.truck(t.lane).i == s.o:
                    out["truck"] = out.get("truck", 0) + t.teu
            return out
        a, b = modes(base_plan), modes(plan)
        if a != b:
            moves.append({"shipment": s.id, "before": a, "after": b})
    shifts = []
    for (sid, k), dep in plan.lcs_dep.items():
        if k == 0 and plan.leg_load((sid, k)) > 0:
            b0 = base_plan.lcs_dep.get((sid, k))
            if b0 is not None and abs(dep - b0) > 1e-6:
                shifts.append({"service": sid, "from": b0 - inst.service(sid).load_time,
                               "to": dep - inst.service(sid).load_time})
    cancelled = [v.id for v in inst.services if v.preplanned and not any(
        plan.leg_load((v.id, k)) > 0 for k in range(len(v.legs)))]
    trucks = int(round(sum(t.teu for t in plan.trucks)))
    base_trucks = int(round(sum(t.teu for t in base_plan.trucks)))
    return {"believed": belief, "discounted": discounted, "reroutes": moves, "shifts": shifts,
            "cancelled": cancelled, "trucks": trucks, "extra_trucks": trucks - base_trucks,
            "expected_cost": round(expected_cost, 1), "cost_change": round(expected_cost - base_cost, 1)}


def _fmt_mag(e):
    return f"{e['magnitude'] * 100:+g} %" if e["kind"] == "volume" else f"{e['magnitude']:g} h"


class MockExplainer:
    """Template-based explainer: every sentence is generated from the fact sheet (faithful by construction)."""

    def explain(self, f: dict) -> str:
        out = []
        if not f["believed"] and not f["discounted"]:
            return "No disturbance confirmed for today; the original plan is kept."
        if f["believed"]:
            out.append("Acted on: " + "; ".join(
                f"{e['target']} {e['kind'].replace('lcs_delay', 'departure delay').replace('release', 'late release').replace('volume', 'volume change')}"
                f" of {_fmt_mag(e)} ({e['basis']})" for e in f["believed"]) + ".")
        if f["discounted"]:
            out.append("Not acted on: " + "; ".join(
                f"{e['sender']}'s claim that {e['target']} has a {_fmt_mag(e)} "
                f"{e['kind'].replace('lcs_delay', 'delay').replace('release', 'release delay').replace('volume', 'volume change')}"
                f" ({e['reason']})"
                for e in f["discounted"]) + ".")
        for m in f["reroutes"]:
            out.append(f"{m['shipment']} moves from {_alloc(m['before'])} to {_alloc(m['after'])}.")
        for s in f["shifts"]:
            out.append(f"{s['service']} starts loading at {s['to']:g}:00 instead of {s['from']:g}:00.")
        if f["cancelled"]:
            out.append("Cancelled: " + ", ".join(f["cancelled"]) + ".")
        if f["extra_trucks"]:
            out.append(f"Truck use changes by {f['extra_trucks']:+d} trucks ({f['trucks']} in total).")
        out.append(f"Expected operating cost {f['expected_cost']:,.1f} EUR ({f['cost_change']:+,.1f} EUR vs the "
                   f"original plan).")
        return " ".join(out)


def _alloc(d):
    return ", ".join(f"{q:g} TEU {k}" for k, q in sorted(d.items())) or "nothing"


EXPLAIN_SYSTEM = ("You explain a freight re-planning decision to shippers and carriers in at most 6 sentences. "
                  "Use ONLY the facts in the JSON. Every number you write must appear in the JSON.")


class LLMExplainer:
    def __init__(self, complete: Callable[[str, str], str]):
        self.complete = complete

    def explain(self, f: dict) -> str:
        return self.complete(EXPLAIN_SYSTEM, json.dumps(f))


def verify_explanation(text: str, facts: dict) -> dict:
    """Faithfulness check: every number in the explanation must occur in the fact sheet."""
    allowed = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            for v in (x, abs(x), round(x, 1), round(abs(x) * 100)):
                allowed.add(round(float(v), 1))
        elif isinstance(x, str):
            for n in re.findall(r"\d+(?:\.\d+)?", x):
                allowed.add(round(float(n), 1))
    walk(facts)
    # identifiers (v0006, S4, shipper_S2) and clock suffixes (':00') are names, not numeric claims
    body = re.sub(r"\b[A-Za-z_]+\d+\b", " ", text)
    body = re.sub(r":00\b", " ", body)
    nums = [round(float(n.replace(",", "")), 1) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", body)]
    bad = [n for n in nums if n not in allowed]
    stripped = text.strip()
    complete = bool(stripped) and stripped[-1] in ".!?)"
    # faithful = complete text AND every number supported (an empty or cut-off text is never faithful)
    return {"numbers": len(nums), "unsupported": bad, "complete": complete, "faithful": complete and not bad}


# =================================================================================== caching
class CachedInterpreter:
    """Disk cache around any interpreter: each distinct (sender, text) is sent to the model once.
    Needed because the reputation contract re-reads the whole ledger every day."""

    def __init__(self, inner, path: str):
        import os
        self.inner, self.path = inner, path
        self.name = getattr(inner, "name", "interpreter")
        self.cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        self.hits = self.misses = 0

    def _key(self, text, sender):
        import hashlib
        return hashlib.sha256(f"{self.name}|{sender}|{text}".encode()).hexdigest()

    def interpret(self, text: str, sender: str) -> ParsedEvent:
        k = self._key(text, sender)
        if k in self.cache:
            self.hits += 1
            return ParsedEvent(**self.cache[k])
        self.misses += 1
        p = self.inner.interpret(text, sender)
        if not p.rationale.startswith("llm error"):          # never cache transient API failures
            self.cache[k] = asdict(p)
            if self.misses % 25 == 0:
                self.save()
        return p

    def save(self):
        import os
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        json.dump(self.cache, open(self.path, "w", encoding="utf-8"))


class CachedExplainer:
    def __init__(self, inner, path: str):
        import os
        self.inner, self.path = inner, path
        self.cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    def explain(self, facts: dict) -> str:
        import hashlib
        k = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
        if k not in self.cache:
            self.cache[k] = self.inner.explain(facts)
            json.dump(self.cache, open(self.path, "w", encoding="utf-8"))
        return self.cache[k]
