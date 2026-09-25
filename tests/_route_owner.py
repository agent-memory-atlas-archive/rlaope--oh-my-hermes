"""Who a route hands the request to, under shortlist-first routing.

A dispatch names its skill. A clarify the dispatch-evidence gate made from a
confident winner (`ambiguity_kind == "weak_dispatch_evidence"`) names no
skill: it asks, and its `candidate_skill` is the first entry of the shortlist
Hermes chooses from. By default these helpers treat only a dispatch as
ownership, so a test that pins a dispatch fails on a clarify. A caller whose
re-pin to a clarify was approved passes `allow_clarify=True`.
"""

from __future__ import annotations

from collections.abc import Mapping

WEAK_DISPATCH_EVIDENCE = "weak_dispatch_evidence"


def route_owner(route: Mapping[str, object], *, allow_clarify: bool = False) -> str:
    """The dispatched skill; with `allow_clarify=True`, also a weak-evidence clarify's candidate.

    Default is dispatch only: a caller that pins a dispatch fails on a
    clarify, because a clarify's `selected_skill` is the router. Pass
    `allow_clarify=True` only where the re-pin to a clarify was approved.
    """
    if allow_clarify and route.get("action") != "dispatch" and route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE:
        return str(route.get("candidate_skill") or "")
    return str(route.get("selected_skill") or "")


def shortlist_skills(route: Mapping[str, object]) -> list[str]:
    handoff = route.get("candidate_handoff")
    rows = handoff.get("candidates") if isinstance(handoff, Mapping) else None
    return [str(row.get("skill")) for row in rows or () if isinstance(row, Mapping)]


def route_owner_harness(route: Mapping[str, object], *, allow_clarify: bool = False) -> str:
    if allow_clarify and route.get("action") != "dispatch" and route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE:
        return str(route.get("candidate_harness") or "")
    return str(route.get("selected_harness") or "")


def dispatched_or_asked(route: Mapping[str, object], *, allow_clarify: bool = False) -> bool:
    """A dispatch; with `allow_clarify=True`, also the gate's weak-evidence clarify."""
    if route.get("action") == "dispatch":
        return True
    return allow_clarify and route.get("ambiguity_kind") == WEAK_DISPATCH_EVIDENCE
