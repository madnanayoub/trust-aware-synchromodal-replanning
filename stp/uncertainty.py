"""Unreliable-information scenarios: true disturbances vs what stakeholders report.

The STP of Qu et al. (2019) assumes "full deterministic information" (Sec. 3): every disturbance is known
exactly at replanning time. In multi-stakeholder synchromodal practice, disturbance information arrives as
messages from shippers and carriers who may be mistaken or self-interested. This module generates:

  * stakeholders with a persistent behaviour profile (reliable / noisy / strategic);
  * for each operational day, the TRUE events (same event types and magnitudes as Tables 5-8);
  * the REPORTS stakeholders send before the replanning time t0 (structured + free text);
  * independent EVIDENCE (AIS position, rail signalling, terminal gate data) available for part of the
    events - the raw material for verification in the trust layer (step 5).

Ground-truth labels (report_type, truth) are kept in separate fields and must never be read by a policy.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from .instance import Instance

# behaviour when the stakeholder is responsible for a TRUE event, and daily false-alarm probability
PROFILES = {
    "reliable":  {"truthful": 0.95, "exaggerate": 0.02, "understate": 0.02, "omit": 0.01, "false_alarm": 0.01},
    "noisy":     {"truthful": 0.60, "exaggerate": 0.15, "understate": 0.10, "omit": 0.15, "false_alarm": 0.05},
    "strategic": {"truthful": 0.30, "exaggerate": 0.45, "understate": 0.05, "omit": 0.20, "false_alarm": 0.20},
    # role-specific variant (profile_set="role"): a strategic CARRIER hides or plays down its own delays to avoid
    # penalties instead of exaggerating them; strategic shippers keep the exaggerating profile above
    "strategic_carrier": {"truthful": 0.30, "exaggerate": 0.02, "understate": 0.33, "omit": 0.30, "false_alarm": 0.05},
}
RELEASE_DELAYS = [0.5, 1.0, 1.5, 2.0]
LCS_DELAYS = [0.5, 1.0, 1.5]
VOLUME_CHANGES = [-0.3, -0.2, -0.1, 0.1, 0.2, 0.3]
EVENT_COUNT_P = [(0, 0.15), (1, 0.50), (2, 0.35)]
EVIDENCE_P = {"release": 0.7, "lcs_delay": 0.6, "volume": 0.5}   # chance an independent sensor record exists


@dataclass
class Stakeholder:
    id: str
    role: str                 # shipper | carrier
    assets: List[str]         # shipments (shipper) or services (carrier) it reports on
    profile: str


@dataclass
class Report:
    id: str
    day: int
    sender: str
    kind: str                 # release | lcs_delay | volume
    target: str
    magnitude: float          # as claimed
    text: str                 # free-text message (input for the GenAI interpreter)
    sent_at: float            # hour of the day
    # ---- ground truth, for scoring only ----
    report_type: str = ""     # truthful | exaggerated | understated | false_alarm
    true_magnitude: float = 0.0


@dataclass
class Evidence:
    """Independent sensor record (e.g. AIS, rail signalling, terminal gate). Noise-free on the event type,
    small measurement noise on the magnitude."""
    id: str
    day: int
    source: str               # AIS | rail_signalling | terminal_gate | weighbridge
    kind: str
    target: str
    magnitude: float


@dataclass
class Day:
    day: int
    true_events: List[list]
    reports: List[Report]
    evidence: List[Evidence]
    omitted: List[list] = field(default_factory=list)   # true events nobody reported (scoring only)

    def to_dict(self):
        return {"day": self.day, "true_events": self.true_events, "reports": [asdict(r) for r in self.reports],
                "evidence": [asdict(e) for e in self.evidence], "omitted": self.omitted}


def rotterdam_stakeholders(inst: Instance, dishonesty: float, rng: random.Random,
                           profile_set: str = "default") -> List[Stakeholder]:
    """One shipper per shipment, one barge operator, one rail operator. Each is unreliable with probability
    `dishonesty`, in which case it is 'noisy' or 'strategic' with equal probability."""
    sh = [Stakeholder(f"shipper_{s.id}", "shipper", [s.id], "reliable") for s in inst.shipments]
    sh.append(Stakeholder("barge_operator", "carrier", [v.id for v in inst.services if v.mode == "barge"], "reliable"))
    sh.append(Stakeholder("rail_operator", "carrier", [v.id for v in inst.services if v.mode == "rail"], "reliable"))
    for st in sh:
        if rng.random() < dishonesty:
            st.profile = rng.choice(["noisy", "strategic"])
            if profile_set == "role" and st.profile == "strategic" and st.role == "carrier":
                st.profile = "strategic_carrier"
    return sh


# ------------------------------------------------------------------------------------------- text
_HOURS = {0.5: ["30 minutes", "half an hour", "0.5 h", "30 min"], 1.0: ["1 hour", "an hour", "60 minutes", "1h"],
          1.5: ["1.5 hours", "an hour and a half", "90 minutes", "1h30"], 2.0: ["2 hours", "two hours", "120 min"],
          2.5: ["2.5 hours", "two and a half hours", "150 minutes"], 3.0: ["3 hours", "three hours", "180 min"],
          4.0: ["4 hours", "four hours"], 4.5: ["4.5 hours", "four and a half hours"]}
_CAUSE = {"release": ["customs inspection", "late vessel discharge", "missing documents", "yard congestion",
                      "reefer plug-in check"],
          "lcs_delay": ["lock congestion", "crew shortage", "engine trouble", "signal failure", "late inbound",
                        "low water restriction", "track maintenance"]}


def _hours_text(h, rng):
    return rng.choice(_HOURS.get(h, [f"{h:g} hours"]))


def render_text(kind, target, mag, sender, inst: Instance, rng: random.Random) -> str:
    if kind == "release":
        s = inst.shipment(target)
        return rng.choice([
            f"Container batch {target} ({s.n:g} TEU, {s.o} to {s.d}) will be released {_hours_text(mag, rng)} later "
            f"than planned because of {rng.choice(_CAUSE['release'])}.",
            f"FYI: {target} not available before {s.alpha + mag:g}:00 - {rng.choice(_CAUSE['release'])}.",
            f"Release of shipment {target} postponed by {_hours_text(mag, rng)}.",
            f"{sender}: our cargo {target} is delayed ~{_hours_text(mag, rng)} at {s.o}.",
        ])
    if kind == "lcs_delay":
        v = inst.service(target)
        leg = v.legs[0]
        return rng.choice([
            f"{v.mode.capitalize()} {target} ({leg.i}-{v.legs[-1].j}) departs {_hours_text(mag, rng)} late due to "
            f"{rng.choice(_CAUSE['lcs_delay'])}.",
            f"Service {target} delayed by {_hours_text(mag, rng)}; new departure approx. "
            f"{(leg.planned_dep or 0) - v.load_time + mag:g}:00.",
            f"Expect {target} out of {leg.i} {_hours_text(mag, rng)} behind schedule ({rng.choice(_CAUSE['lcs_delay'])}).",
        ])
    if kind == "volume":
        s = inst.shipment(target)
        new = round(s.n * (1 + mag))
        verb = "increase" if mag > 0 else "decrease"
        return rng.choice([
            f"Volume update {target}: {new} TEU instead of {s.n:g} TEU.",
            f"Please note a {abs(mag):.0%} {verb} for {target} (now {new} TEU).",
            f"{target} booking amended to {new} TEU.",
        ])
    raise ValueError(kind)


# ------------------------------------------------------------------------------------------- day
def _round_half(x):
    return max(0.5, round(x * 2) / 2)


def _sample_event(inst: Instance, rng: random.Random):
    kind = rng.choices(["release", "lcs_delay", "volume"], weights=[0.4, 0.35, 0.25])[0]
    if kind == "release":
        return ["release", rng.choice([s.id for s in inst.shipments]), rng.choice(RELEASE_DELAYS)]
    if kind == "lcs_delay":
        return ["lcs_delay", rng.choice([v.id for v in inst.services]), rng.choice(LCS_DELAYS)]
    return ["volume", rng.choice([s.id for s in inst.shipments]), rng.choice(VOLUME_CHANGES)]


def _owner(stakeholders, kind, target):
    for st in stakeholders:
        if target in st.assets and ((kind == "lcs_delay") == (st.role == "carrier")):
            return st
    raise KeyError(target)


def generate_day(inst: Instance, stakeholders: List[Stakeholder], day: int, rng: random.Random,
                 t0: float = 7.0) -> Day:
    k = rng.choices([c for c, _ in EVENT_COUNT_P], weights=[p for _, p in EVENT_COUNT_P])[0]
    events, seen = [], set()
    while len(events) < k:
        e = _sample_event(inst, rng)
        if (e[0], e[1]) not in seen:
            seen.add((e[0], e[1]))
            events.append(e)
    reports, evidence, omitted = [], [], []
    rid = 0

    def add_report(st, kind, target, mag, rtype, true_mag):
        nonlocal rid
        rid += 1
        reports.append(Report(id=f"d{day}_r{rid}", day=day, sender=st.id, kind=kind, target=target, magnitude=mag,
                              text=render_text(kind, target, mag, st.id, inst, rng),
                              sent_at=round(rng.uniform(t0 - 1.0, t0 - 0.1), 2), report_type=rtype,
                              true_magnitude=true_mag))

    for kind, target, mag in events:
        st = _owner(stakeholders, kind, target)
        p = PROFILES[st.profile]
        act = rng.choices(["truthful", "exaggerate", "understate", "omit"],
                          weights=[p["truthful"], p["exaggerate"], p["understate"], p["omit"]])[0]
        if act == "truthful":
            add_report(st, kind, target, mag, "truthful", mag)
        elif act == "exaggerate":
            claimed = mag * rng.choice([1.5, 2.0, 3.0])
            claimed = _round_half(claimed) if kind != "volume" else max(-0.5, min(0.6, round(claimed * 20) / 20))
            add_report(st, kind, target, claimed, "exaggerated", mag)
        elif act == "understate":
            claimed = mag * 0.5
            claimed = round(claimed * 2) / 2 if kind != "volume" else round(claimed * 20) / 20
            if claimed == 0:
                omitted.append([kind, target, mag])
            else:
                add_report(st, kind, target, claimed, "understated", mag)
        else:
            omitted.append([kind, target, mag])
        # independent evidence
        if rng.random() < EVIDENCE_P[kind]:
            src = {"release": "terminal_gate", "volume": "weighbridge"}.get(kind) or \
                ("AIS" if inst.service(target).mode == "barge" else "rail_signalling")
            noise = 0.0 if kind == "volume" else rng.choice([-0.25, 0.0, 0.0, 0.25])
            evidence.append(Evidence(id=f"d{day}_e{len(evidence) + 1}", day=day, source=src, kind=kind,
                                     target=target, magnitude=max(0.0, mag + noise) if kind != "volume" else mag))
    # false alarms
    for st in stakeholders:
        if rng.random() < PROFILES[st.profile]["false_alarm"]:
            if st.role == "carrier":
                tgt, kind, mag = rng.choice(st.assets), "lcs_delay", rng.choice(LCS_DELAYS)
            else:
                tgt, kind, mag = st.assets[0], "release", rng.choice(RELEASE_DELAYS)
            if (kind, tgt) in seen:
                continue
            add_report(st, kind, tgt, mag, "false_alarm", 0.0)
            # sensors observe 'nothing happened' for part of false alarms too
            if rng.random() < EVIDENCE_P[kind]:
                src = "terminal_gate" if kind == "release" else \
                    ("AIS" if inst.service(tgt).mode == "barge" else "rail_signalling")
                evidence.append(Evidence(id=f"d{day}_e{len(evidence) + 1}", day=day, source=src, kind=kind,
                                         target=tgt, magnitude=0.0))
    rng.shuffle(reports)
    return Day(day, events, reports, evidence, omitted)


def generate_stream(inst: Instance, days: int, dishonesty: float, seed: int, profile_set: str = "default"):
    rng = random.Random(seed)
    stakeholders = rotterdam_stakeholders(inst, dishonesty, rng, profile_set)
    return stakeholders, [generate_day(inst, stakeholders, d, rng) for d in range(days)]


def reported_events(day: Day) -> List[list]:
    """Belief of a planner that trusts every report (one per (kind, target); later reports override)."""
    b = {}
    for r in sorted(day.reports, key=lambda r: r.sent_at):
        b[(r.kind, r.target)] = r.magnitude
    return [[k, t, m] for (k, t), m in b.items()]
