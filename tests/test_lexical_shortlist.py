"""An undecided English route offers the skills its situation describes.

`routing/lexical_shortlist.py` ranks the catalog as a bag of words (BM25 over
name, triggers, situations, description, use_when) and `candidate_handoff`
fills a clarify's shortlist from it. Ranking is not admission: a ranked skill
enters only with an anchor word from what users say about it, never as a
`jev-*` skill, never when its offers-itself precondition fails, and never for a
non-ASCII request. Jev's fallback uses the same ranking to pick a sibling only
on a clear lead.
"""

from __future__ import annotations

import unittest

from omh.routing.candidate_handoff import LEXICAL_WHY, MAX_CANDIDATES
from omh.routing.chat import route_chat_message
from omh.routing.jev_addressing import JEV_SKILL_NAMES, jev_addressed_skill
from omh.routing.lexical_shortlist import lexical_anchor_terms, lexical_ranking, lexical_terms


def _shortlist(message: str) -> list[str]:
    route = route_chat_message(message, source="discord")
    handoff = route.get("candidate_handoff") or {}
    return [str(candidate["skill"]) for candidate in handoff.get("candidates", [])]


class LexicalRankingTests(unittest.TestCase):
    def test_terms_drop_function_words_and_fold_inflections(self) -> None:
        self.assertEqual(sorted(lexical_terms("the failing tests in my reports")), ["fail", "report", "test"])

    def test_the_ranking_is_reproducible_and_ordered(self) -> None:
        first = lexical_ranking("our spending spreadsheet needs a cash forecast")
        self.assertEqual(first, lexical_ranking("our spending spreadsheet needs a cash forecast"))
        scores = [score for _skill, score in first]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_message_of_function_words_ranks_nothing(self) -> None:
        self.assertEqual(lexical_ranking("can you do that for me please"), ())

    def test_a_held_back_word_is_not_an_anchor(self) -> None:
        # `document` alone is held back for long-document-reading: the scorer
        # credits it only inside a whole phrase, and so does the shortlist.
        self.assertNotIn("document", lexical_anchor_terms("document the api", "long-document-reading"))


class LexicalFillTests(unittest.TestCase):
    def test_a_weak_clarify_offers_lexical_candidates(self) -> None:
        route = route_chat_message("review the onboarding flow for rough edges", source="discord")
        handoff = route["candidate_handoff"]
        self.assertLessEqual(len(handoff["candidates"]), MAX_CANDIDATES)
        # The scored leader leads: it is the route's candidate, and a question
        # that did not offer it could not be answered with it.
        self.assertEqual(handoff["candidates"][0]["skill"], route["candidate_skill"])
        lexical = [candidate for candidate in handoff["candidates"] if candidate["why_it_matched"] == LEXICAL_WHY]
        self.assertTrue(lexical)
        for candidate in lexical:
            self.assertEqual(candidate["score"], 0)
            self.assertEqual(candidate["matched"], ["lexical_shortlist"])

    def test_no_jev_skill_enters_by_word_overlap(self) -> None:
        for message in ("pick the right workflow for my request", "is this failure flaky or a real regression"):
            with self.subTest(message=message):
                self.assertFalse([skill for skill in _shortlist(message) if skill.startswith("jev-")])

    def test_a_failed_offers_itself_precondition_keeps_the_skill_out(self) -> None:
        # apple-design offers itself only for Apple-platform design; the
        # company's stock shares its words and not its job.
        self.assertNotIn("apple-design", _shortlist("Apple stock is unrelated to interface design."))

    def test_a_non_ascii_request_keeps_the_scored_shortlist(self) -> None:
        route = route_chat_message("PR 리뷰 좀 해줘", source="generic", limit=3)
        candidates = route["candidate_handoff"]["candidates"]
        self.assertTrue(candidates)
        self.assertFalse([candidate for candidate in candidates if candidate["why_it_matched"] == LEXICAL_WHY])

    def test_a_dispatch_carries_no_shortlist(self) -> None:
        route = route_chat_message("why is the build failing on main?", source="discord")
        self.assertEqual(route["action"], "dispatch")
        self.assertNotIn("candidate_handoff", route)


class JevLexicalSiblingTests(unittest.TestCase):
    def test_a_clear_lead_picks_the_sibling(self) -> None:
        self.assertEqual(
            jev_addressed_skill("have jev decide whether this flaky failure deserves a retry", JEV_SKILL_NAMES),
            "jev-failure-triage",
        )

    def test_a_thin_or_close_ranking_stays_with_jev_ask(self) -> None:
        for message in (
            "have jev look this over",
            "let jev judge whether this shell command could wipe data",
            "ask jev whether this doc section is complete",
        ):
            with self.subTest(message=message):
                self.assertEqual(jev_addressed_skill(message, JEV_SKILL_NAMES), "jev-ask")


if __name__ == "__main__":
    unittest.main()


class ShortlistHintTests(unittest.TestCase):
    """What the model reads on a shortlist clarify: a plain instruction to pick."""

    def test_the_clarify_card_names_each_candidate_by_its_situation(self) -> None:
        from omh.quality.reply_lint import _ENGLISH_RECORD_TERMS
        from omh.skills.catalog import routable_definitions
        from omh.wrapper.contract import build_chat_interaction_payload

        payload = build_chat_interaction_payload("review the onboarding flow for rough edges", source="discord")
        route = payload["route"]
        body = payload["chat_response"]["body"]
        candidates = [candidate["skill"] for candidate in route["candidate_handoff"]["candidates"]]
        self.assertTrue(body.startswith("Pick the workflow whose situation matches what the user described"))
        self.assertEqual(route["candidate_skill"], candidates[0])
        descriptions = {definition.name: definition.description for definition in routable_definitions()}
        for skill in candidates:
            with self.subTest(skill=skill):
                situation = descriptions[skill].removeprefix("[omh]").strip().partition(":")[0]
                self.assertIn(f"`{skill}` ({situation})", body)
        for term in _ENGLISH_RECORD_TERMS:
            with self.subTest(term=term):
                self.assertNotIn(term, body.lower())
        # The same shortlist is the question an answerer is asked.
        from omh.routing.route_question import ROUTE_CHOICE_KEY

        options = route["route_question"]["questions"][ROUTE_CHOICE_KEY]["options"]
        self.assertEqual([skill for skill in options if skill != "none"], candidates)
