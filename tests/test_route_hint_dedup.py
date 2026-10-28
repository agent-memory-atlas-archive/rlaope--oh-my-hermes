"""The rendered route hint drops text that repeats other text in the same block.

Three exact duplicates go: a hint's `adjacent_workflows=` line equal to the
block's top-level one, a `fallback_action_label=` that only respells the
action's underscores, and the same for `next_action_label=`. Each test pins
both ways (the duplicate removed, a differing value kept), and the lines that
carry meaning (`first_response_shape=`, `not_evidence_yet=`, `plan_todo=`)
render as before.
"""

from __future__ import annotations

import unittest

from omh.plugin_bundle.omh.awareness import (
    PLAN_TODO_DECLARATION_RULE,
    awareness_route_hint,
    awareness_route_hint_context_from_payload,
)


def _payload(**hint_fields: object) -> dict[str, object]:
    hint: dict[str, object] = {
        "id": "test_hint",
        "workflow": "code-review",
        "lane": "review",
        "next_action": "review_the_diff",
        "next_action_label": "reading the diff for bugs",
        "reason": "The user asked for a review.",
        "adjacent_workflows": ["ulw-plan", "agent-ops-review"],
        "fallback_action": "ask_for_the_diff",
        "fallback_action_label": "asking which diff to read",
        "not_evidence_yet": ["review", "CI"],
        "workflow_context_card": {"first_response_shape": "Name the diff, then the findings."},
    }
    hint.update(hint_fields)
    return {
        "status": "hinted",
        "intent_class": "review_intent",
        "selected_workflow": hint["workflow"],
        "adjacent_workflows": ["ulw-plan", "agent-ops-review"],
        "mentioned_workflows": [],
        "not_executed": [],
        "hints": [hint],
        "claim_boundary": "OMH route hints are local deterministic prompt context only.",
    }


def _lines(payload: dict[str, object]) -> list[str]:
    return awareness_route_hint_context_from_payload(payload).splitlines()


class AdjacentWorkflowsDedupTest(unittest.TestCase):
    def test_hint_line_equal_to_top_level_is_dropped(self) -> None:
        lines = _lines(_payload())
        self.assertIn("adjacent_workflows=ulw-plan, agent-ops-review.", lines)
        self.assertNotIn("  adjacent_workflows=ulw-plan, agent-ops-review.", lines)

    def test_hint_line_that_differs_is_kept(self) -> None:
        payload = _payload(adjacent_workflows=["ulw-loop", "code-review"])
        lines = _lines(payload)
        self.assertIn("adjacent_workflows=ulw-plan, agent-ops-review.", lines)
        self.assertIn("  adjacent_workflows=ulw-loop, code-review.", lines)

    def test_hint_line_is_kept_when_there_is_no_top_level_line(self) -> None:
        payload = _payload()
        payload["adjacent_workflows"] = []
        lines = _lines(payload)
        self.assertNotIn("adjacent_workflows=ulw-plan, agent-ops-review.", lines)
        self.assertIn("  adjacent_workflows=ulw-plan, agent-ops-review.", lines)


class FallbackActionLabelDedupTest(unittest.TestCase):
    def test_label_that_respells_the_action_is_dropped(self) -> None:
        lines = _lines(_payload(fallback_action_label="ask for the diff"))
        self.assertIn("  fallback_action=ask_for_the_diff.", lines)
        self.assertFalse(any("fallback_action_label=" in line for line in lines))

    def test_prose_label_is_kept(self) -> None:
        lines = _lines(_payload())
        self.assertIn(
            "  fallback_action_label=asking which diff to read; fallback_action=ask_for_the_diff.", lines
        )


class NextActionLabelDedupTest(unittest.TestCase):
    def test_label_that_respells_the_action_is_dropped(self) -> None:
        lines = _lines(_payload(next_action_label="review the diff"))
        self.assertIn(
            "- selected=code-review; lane=review; next_action=review_the_diff; reason=The user asked for a review.",
            lines,
        )

    def test_prose_label_is_kept(self) -> None:
        lines = _lines(_payload())
        self.assertIn(
            "- selected=code-review; lane=review; next_action_label=reading the diff for bugs; "
            "next_action=review_the_diff; reason=The user asked for a review.",
            lines,
        )


class MeaningLinesUnchangedTest(unittest.TestCase):
    def test_first_response_shape_and_not_evidence_yet_render_as_before(self) -> None:
        lines = _lines(_payload(fallback_action_label="ask for the diff", next_action_label="review the diff"))
        self.assertIn("  first_response_shape=Name the diff, then the findings.", lines)
        self.assertIn("  not_evidence_yet=review, CI.", lines)
        self.assertEqual(lines[-1], "Boundary: OMH route hints are local deterministic prompt context only.")

    def test_plan_todo_renders_as_before(self) -> None:
        lines = _lines(_payload(workflow="ulw-work"))
        self.assertIn(f"  plan_todo={PLAN_TODO_DECLARATION_RULE}", lines)

    def test_live_routed_request_keeps_its_meaning_lines(self) -> None:
        payload = awareness_route_hint("review this PR for bugs before merge")
        text = awareness_route_hint_context_from_payload(payload)
        self.assertIn("  first_response_shape=", text)
        self.assertIn("  not_evidence_yet=", text)
        self.assertIn("  plan_todo=", text)
        self.assertIn("\nadjacent_workflows=", text)
        self.assertNotIn("\n  adjacent_workflows=", text)


if __name__ == "__main__":
    unittest.main()
