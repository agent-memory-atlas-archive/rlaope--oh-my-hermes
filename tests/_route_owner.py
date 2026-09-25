"""Who a route hands the request to, under shortlist-first routing.

A dispatch names its skill. A clarify the dispatch-evidence gate made from a
confident winner (`ambiguity_kind == "weak_dispatch_evidence"`) names no
skill: it asks, and its `candidate_skill` is the first entry of the shortlist
Hermes chooses from. Tests that pin "this request belongs to that lane" read
the owner through here, so the claim survives the gate asking instead of
dispatching -- and still fails when the lane that leads the shortlist is the
wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping

WEAK_DISPATCH_EVIDENCE = "weak_dispatch_evidence"


def route_owner(route: Mapping[str, object]) -> str:
    if route.get("action") != "dispatch" and route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE:
        return str(route.get("candidate_skill") or "")
    return str(route.get("selected_skill") or "")


def shortlist_skills(route: Mapping[str, object]) -> list[str]:
    handoff = route.get("candidate_handoff")
    rows = handoff.get("candidates") if isinstance(handoff, Mapping) else None
    return [str(row.get("skill")) for row in rows or () if isinstance(row, Mapping)]


def route_owner_harness(route: Mapping[str, object]) -> str:
    if route.get("action") != "dispatch" and route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE:
        return str(route.get("candidate_harness") or "")
    return str(route.get("selected_harness") or "")


def dispatched_or_asked(route: Mapping[str, object]) -> bool:
    """A dispatch, or the clarify the dispatch-evidence gate made instead of one."""
    return route.get("action") == "dispatch" or route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE
