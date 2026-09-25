"""Shape of `SkillDefinition.situations`, the plain-words list of user situations.

Every installable skill names the situations it serves in the words a user who
does not know the skill name would use. A situation carries words the trigger
table does not, so it may neither equal nor contain one of the skill's own
multi-word triggers.
"""

from __future__ import annotations

import unittest

from omh.skills.catalog import builtin_definitions, installable_skill_definitions


class SkillSituationsShapeTests(unittest.TestCase):
    def test_every_installable_skill_carries_five_to_ten_situations(self) -> None:
        for definition in installable_skill_definitions():
            with self.subTest(skill=definition.name):
                self.assertGreaterEqual(len(definition.situations), 5)
                self.assertLessEqual(len(definition.situations), 10)

    def test_each_situation_is_non_empty_ascii_text(self) -> None:
        for definition in installable_skill_definitions():
            for situation in definition.situations:
                with self.subTest(skill=definition.name, situation=situation):
                    self.assertIsInstance(situation, str)
                    self.assertTrue(situation.strip(), "a situation is not blank")
                    self.assertEqual(situation, situation.strip())
                    self.assertTrue(situation.isascii(), "situations are English plain text")

    def test_no_skill_repeats_a_situation(self) -> None:
        for definition in installable_skill_definitions():
            with self.subTest(skill=definition.name):
                folded = [situation.casefold() for situation in definition.situations]
                self.assertEqual(len(folded), len(set(folded)))

    def test_no_situation_restates_one_of_the_skills_own_triggers(self) -> None:
        # A situation that equals a trigger, or wraps a multi-word trigger in a
        # few extra words, adds nothing the trigger table does not already say;
        # the field exists for the words triggers do not carry. Single-word
        # triggers are exempt: they are ordinary words a plain phrase needs.
        for definition in installable_skill_definitions():
            triggers = {trigger.strip().casefold() for trigger in definition.triggers}
            phrases = sorted(trigger for trigger in triggers if " " in trigger)
            for situation in definition.situations:
                folded = situation.casefold()
                with self.subTest(skill=definition.name, situation=situation):
                    self.assertNotIn(folded, triggers)
                    self.assertEqual([phrase for phrase in phrases if phrase in folded], [])

    def test_retired_definitions_carry_none(self) -> None:
        installable = {definition.name for definition in installable_skill_definitions()}
        for definition in builtin_definitions():
            if definition.name not in installable:
                with self.subTest(skill=definition.name):
                    self.assertEqual(definition.situations, ())


if __name__ == "__main__":
    unittest.main()
