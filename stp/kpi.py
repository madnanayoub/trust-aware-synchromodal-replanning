"""The 11 KPIs of Qu et al. (2019) Sec. 6.3, computed for a re-plan against the original plan.

Flexibility: reroute_TEU   = TEU placed on a first-leg service (an arc leaving the shipment's origin) on which it was
                             not planned: sum_s sum_{a out of o^s} max(0, x - x_base). Reverse-engineered: matches
                             47/62 rows of the paper's Tables 5-8 (full L1 distance matches 24/62)
             reroute_L1_TEU = sum over shipments and all arcs of |x - x_base| (secondary, stricter measure)
             resched_vh    = sum over used LCS legs of |y_dep - pi| (pi = timetable of the disturbed instance,
                             i.e. imposed delays are not counted as rescheduling decisions)
LSP:         dCf/dCc/dCt % relative to the base plan; cancelled LCS; extra trucks (signed)
             dLCS_modal_pct / dLCS_util_pct: the paper uses TEU*km, but no distances are published. We use
             a TEU-leg basis: modal = LCS TEU-legs / all TEU-legs; utilisation = LCS load / capacity over
             used LCS legs. Signs are comparable with the paper, magnitudes are not.
Service:     dEarly_TEUh / dLate_TEUh (signed, vs base)
"""
from __future__ import annotations

from collections import defaultdict

from .evaluate import evaluate, Evaluation
from .instance import Instance
from .plan import Plan

COLS = ["reroute_TEU", "resched_vh", "dCf_pct", "dCc_pct", "dCt_pct", "cancelled_LCS", "extra_trucks",
        "dLCS_modal_pct", "dLCS_util_pct", "dEarly_TEUh", "dLate_TEUh"]


def _arc_flows(plan: Plan):
    f = defaultdict(float)
    for (sid, k), d in plan.lcs_flow.items():
        for s, q in d.items():
            f[(("L", sid, k), s)] += q
    for t in plan.trucks:
        f[(("T", t.lane), t.shipment)] += t.teu
    return f


def _shares(inst: Instance, plan: Plan):
    lcs = sum(plan.leg_load(k) for k in plan.lcs_flow)
    trk = sum(t.teu for t in plan.trucks)
    cap = sum(inst.service(sid).capacity for (sid, k) in plan.lcs_flow if plan.leg_load((sid, k)) > 1e-9)
    modal = 100 * lcs / (lcs + trk) if lcs + trk else 0.0
    util = 100 * lcs / cap if cap else 0.0
    return modal, util


def kpis(inst: Instance, plan: Plan, base_inst: Instance, base: Plan, ev: Evaluation = None,
         ev_base: Evaluation = None) -> dict:
    ev = ev or evaluate(inst, plan)
    ev_base = ev_base or evaluate(base_inst, base)
    fa, fb = _arc_flows(plan), _arc_flows(base)
    reroute_l1 = sum(abs(fa.get(k, 0) - fb.get(k, 0)) for k in set(fa) | set(fb))

    def origin_of_arc(k):
        (ev, s) = k
        node = inst.service(ev[1]).legs[ev[2]].i if ev[0] == "L" else inst.truck(ev[1]).i
        return node == inst.shipment(s).o
    reroute = sum(max(0.0, fa.get(k, 0) - fb.get(k, 0)) for k in set(fa) | set(fb) if origin_of_arc(k))
    resched = 0.0
    for (sid, k), dep in plan.lcs_dep.items():
        pi = inst.service(sid).legs[k].planned_dep
        if pi is not None and plan.leg_load((sid, k)) > 1e-9:
            resched += abs(dep - pi)
    m1, u1 = _shares(inst, plan)
    m0, u0 = _shares(base_inst, base)
    pct = lambda a, b: 100 * (a - b) / b if b else 0.0
    return {
        "reroute_TEU": round(reroute, 2),
        "reroute_L1_TEU": round(reroute_l1, 2),
        "resched_vh": round(resched, 2),
        "dCf_pct": round(pct(ev.fixed, ev_base.fixed), 2),
        "dCc_pct": round(pct(ev.variable, ev_base.variable), 2),
        "dCt_pct": round(float(pct(ev.transshipment, ev_base.transshipment)), 2),
        "cancelled_LCS": len(ev.lcs_cancelled),
        "extra_trucks": ev.trucks_used - ev_base.trucks_used,
        "dLCS_modal_pct": round(m1 - m0, 2),
        "dLCS_util_pct": round(u1 - u0, 2),
        "dEarly_TEUh": round(ev.early_teu_h - ev_base.early_teu_h, 2),
        "dLate_TEUh": round(ev.late_teu_h - ev_base.late_teu_h, 2),
        "total_cost": round(float(ev.total), 2),
        "dTotal_pct": round(float(pct(ev.total, ev_base.total)), 2),
        "unserved_TEU": round(float(ev.unserved_teu), 2),
    }
