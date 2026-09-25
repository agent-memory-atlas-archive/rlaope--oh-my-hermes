"""A confident score dispatches only when the winner's own labels name the job.

`routing/dispatch_evidence.py` reads a scored winner's matched labels and
classifies the evidence; a `weak` winner becomes a clarify with the
`weak_dispatch_evidence` reason and a candidate handoff. These tests pin the
classifier on label sets directly, and the route-level shape on sentences built
from the collisions the design traced: everyday words that are also trigger
tokens ("before I merge"), and a guard boost carrying a skill with no evidence
of its own ("last time").
"""

from __future__ import annotations

import unittest

from omh.routing.candidate_handoff import WEAK_DISPATCH_EVIDENCE
from omh.routing.chat import route_chat_message
from omh.routing.dispatch_evidence import (
    EVIDENCE_DIRECT,
    EVIDENCE_EXPLICIT,
    EVIDENCE_FAST_PATH,
    EVIDENCE_NON_ASCII_EXEMPT,
    EVIDENCE_TRIGGER_PHRASE,
    EVIDENCE_TRUSTED_GUARD,
    EVIDENCE_WEAK,
    dispatch_evidence,
    own_evidence_score,
)
from omh.routing import policy
from omh.routing.policy import GUARD_CONTEXT_ONLY, GUARD_DISPATCH_TRUST, GUARD_TRUSTED

TRUST = {"guard:trusted_shape": GUARD_TRUSTED, "guard:topic_words": GUARD_CONTEXT_ONLY}


def _classify(labels: list[str], *, others: list[list[str]] = (), message: str = "plain ascii") -> str:
    return dispatch_evidence(
        {"skill": "x", "score": 20, "matched": labels},
        [{"skill": f"o{index}", "score": 9, "matched": other} for index, other in enumerate(others)],
        message=message,
        guard_trust=TRUST,
    )


class DispatchEvidenceClassifierTests(unittest.TestCase):
    def test_explicit_direct_and_domain_labels_keep_the_dispatch(self) -> None:
        self.assertEqual(_classify(["explicit_invocation", "name:x"]), EVIDENCE_EXPLICIT)
        self.assertEqual(_classify(["direct:x_specialist", "trigger:review"]), EVIDENCE_DIRECT)
        self.assertEqual(_classify(["domain:contract"]), EVIDENCE_DIRECT)

    def test_a_label_the_scorer_does_not_produce_is_a_fast_path(self) -> None:
        self.assertEqual(_classify(["operator_surface_fast_path:status"]), EVIDENCE_FAST_PATH)

    def test_an_own_phrase_dispatches_only_when_no_other_skill_said_one(self) -> None:
        self.assertEqual(_classify(["trigger:code review", "trigger:review"]), EVIDENCE_TRIGGER_PHRASE)
        self.assertEqual(_classify(["trigger:$ulw"]), EVIDENCE_TRIGGER_PHRASE)
        self.assertEqual(_classify(["name:code review"]), EVIDENCE_TRIGGER_PHRASE)
        # Two skills each said a phrase of their own: a choice, not a decision.
        self.assertEqual(
            _classify(["trigger:code review"], others=[["trigger:security review"]]),
            EVIDENCE_WEAK,
        )

    def test_tokens_alone_never_dispatch_however_rare(self) -> None:
        self.assertEqual(_classify(["trigger:segfault", "trigger:coredump", "trigger:review"]), EVIDENCE_WEAK)
        self.assertEqual(_classify(["name:plan", "trigger:plan"]), EVIDENCE_WEAK)

    def test_a_trusted_guard_dispatches_on_its_own(self) -> None:
        # "fix the login bug": an imperative plus a code object, no trigger word.
        self.assertEqual(_classify(["guard:trusted_shape"]), EVIDENCE_TRUSTED_GUARD)

    def test_a_context_only_or_unknown_guard_does_not(self) -> None:
        self.assertEqual(_classify(["guard:topic_words", "trigger:review", "trigger:before"]), EVIDENCE_WEAK)
        self.assertEqual(_classify(["guard:unlisted", "trigger:segfault"]), EVIDENCE_WEAK)

    def test_non_ascii_input_is_left_to_the_frozen_tables(self) -> None:
        self.assertEqual(_classify(["trigger:리뷰"], message="PR 리뷰 좀 해줘"), EVIDENCE_NON_ASCII_EXEMPT)

    def test_own_evidence_counts_neither_guards_nor_metadata(self) -> None:
        self.assertEqual(own_evidence_score(["guard:x", "metadata:a", "metadata:b"]), 0)
        self.assertEqual(own_evidence_score(["trigger:code review", "trigger:review", "name:x"]), 6 + 3 + 5)


class GuardDispatchTrustTableTests(unittest.TestCase):
    def test_every_guard_rule_is_classified(self) -> None:
        # A new guard must be a visible choice, not a silent default.
        rules = {value.id for value in vars(policy).values() if isinstance(value, policy.RoutingGuardRule)}
        self.assertEqual(rules, set(GUARD_DISPATCH_TRUST))

    def test_every_entry_is_a_known_trust_with_a_reason(self) -> None:
        for guard_id, (trust, reason) in GUARD_DISPATCH_TRUST.items():
            with self.subTest(guard=guard_id):
                self.assertIn(trust, {GUARD_TRUSTED, GUARD_CONTEXT_ONLY})
                self.assertTrue(reason.strip())


class WeakEvidenceRouteTests(unittest.TestCase):
    """Negative half: a high score built from everyday words does not dispatch."""

    def _assert_weak_clarify(self, message: str) -> dict:
        route = route_chat_message(message, source="discord")
        self.assertEqual(route["action"], "clarify")
        self.assertEqual(route["selected_skill"], "oh-my-hermes")
        self.assertEqual(route.get("ambiguity_kind"), WEAK_DISPATCH_EVIDENCE)
        handoff = route["candidate_handoff"]
        self.assertIn(WEAK_DISPATCH_EVIDENCE, handoff["reasons"])
        self.assertTrue(handoff["candidates"])
        self.assertIn("route_question", route)
        return route

    def test_before_and_merge_do_not_dispatch_the_verification_gate(self) -> None:
        # `before` and `merge` are trigger tokens of verification-gate phrases;
        # the sentence says neither phrase.
        self._assert_weak_clarify("check this diff before I merge it")

    def test_last_time_does_not_carry_a_live_information_guard(self) -> None:
        # `what` plus `time` fired the live-information guard for +42 on a
        # request about a past decision; the guard now needs a live cue.
        route = route_chat_message("what did we pick last time for the cache layer?", source="discord")
        self.assertNotEqual(route["action"], "dispatch")
        self.assertNotEqual(route["candidate_skill"], "live-info-operator")


class StrongEvidenceRouteTests(unittest.TestCase):
    """Positive half: a winner whose own labels name the job still dispatches."""

    def test_an_own_trigger_phrase_dispatches(self) -> None:
        route = route_chat_message("why is the build failing on main?", source="discord")
        self.assertEqual(route["action"], "dispatch")
        self.assertNotIn("ambiguity_kind", route)

    def test_an_explicit_invocation_dispatches(self) -> None:
        route = route_chat_message("$code-review this change", source="discord")
        self.assertEqual(route["action"], "dispatch")
        self.assertEqual(route["selected_skill"], "code-review")

    def test_a_non_ascii_request_is_not_gated(self) -> None:
        route = route_chat_message("기억이 잘못 저장된 것 같아 확인해줘", source="slack")
        self.assertEqual(route["action"], "dispatch")


class NarrowedGuardTests(unittest.TestCase):
    """The guards narrowed beside the gate keep their own shapes."""

    def test_canonical_code_edits_still_dispatch_delivery(self) -> None:
        for message in ("fix the login bug", "implement dark mode toggle", "rename this variable"):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], "ultrawork")

    def test_writing_a_prompt_for_a_named_agent_is_not_a_delivery(self) -> None:
        route = route_chat_message("write the instructions I will give codex for the parser fix", source="discord")
        self.assertNotEqual(route["selected_skill"], "ultrawork")
        route = route_chat_message("have codex fix the flaky checkout test", source="discord")
        self.assertEqual(route["selected_skill"], "ultrawork")


class RecurringIssueDigestTests(unittest.TestCase):
    """`new github issue` is a substring of `new github issues`.

    The filing guard's explicit phrase handed a recurring digest of new issues a
    +44 filing boost. A cadence word now takes the filing guard off; filing
    requests keep it.
    """

    def test_a_daily_digest_of_new_issues_is_not_issue_filing(self) -> None:
        route = route_chat_message("every weekday, post the new github issues to our team channel daily", source="discord")
        self.assertNotEqual(route["selected_skill"], "github-issue-intake")
        route = route_chat_message("weekly, list the new github issues in our repo", source="discord")
        self.assertNotEqual(route["selected_skill"], "github-issue-intake")

    def test_filing_a_new_issue_still_reaches_intake(self) -> None:
        for message in (
            "file a new github issue for this crash",
            "open a new GitHub issue for the flaky login test",
        ):
            with self.subTest(message=message):
                route = route_chat_message(message, source="discord")
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], "github-issue-intake")


if __name__ == "__main__":
    unittest.main()
