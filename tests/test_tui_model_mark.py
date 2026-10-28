"""The bare `/model` picker marks the session's model, opens on it, and says where the highlight is (walk G5).

The recorded `model.options` answer (Hermes 6c61c3e27d, `tests/fixtures/tui_gateway/parity.jsonl`) is the
owner's own case: the session runs `deepseek/deepseek-v4.1-flash-ultrafast` on the `og` provider, which Hermes
marks `is_current` and whose model list does not hold that route.
"""

from __future__ import annotations

import unittest

from _tui_lines import lines_text
from test_tui_muscle_memory import OPTIONS
from test_tui_modes import SID, answer, drawn, make_app, type_text
from omh.tui.keys import CHAR, DOWN, ENTER, Key
from omh.tui.model_picker import CURRENT_MARK, ModelPicker, model_rows
from omh.tui.theme import DEFAULT_THEME

RUNNING = "deepseek/deepseek-v4.1-flash-ultrafast"


class CurrentModelTests(unittest.TestCase):
    def test_a_model_no_provider_lists_comes_first_marked_and_picked_on_enter(self) -> None:
        self.assertEqual((OPTIONS["model"], OPTIONS["provider"]), (RUNNING, "og"))
        rows = model_rows(OPTIONS)
        self.assertEqual((rows[0].model, rows[0].provider, rows[0].provider_name, rows[0].current), (RUNNING, "og", "og", True))
        self.assertEqual([row for row in rows if row.current], [rows[0]])
        picker = ModelPicker(rows, current=RUNNING)
        self.assertEqual(picker.index, 0)
        self.assertEqual(picker.handle_key(Key(ENTER)).row.arg, f"{RUNNING} --provider og --session")

    def test_a_listed_model_is_marked_under_the_provider_hermes_names_by_slug_alias_or_is_current(self) -> None:
        providers = [
            {"slug": "anthropic", "name": "Anthropic", "models": ["m1", "shared"]},
            {"slug": "og", "name": "og", "is_current": True, "aliases": ["custom:og", "og"], "models": ["shared", "m2"]},
        ]
        for provider in ("og", "custom:og", "something-hermes-calls-it"):
            with self.subTest(provider=provider):
                rows = model_rows({"model": "shared", "provider": provider, "providers": providers})
                self.assertEqual([(row.model, row.provider) for row in rows if row.current], [("shared", "og")])
                self.assertEqual(len(rows), 4)  # listed: no row of its own

    def test_an_alias_alone_names_the_provider(self) -> None:
        providers = [{"slug": "og", "name": "og", "aliases": ["custom:og", "og"], "models": ["shared"]},
                     {"slug": "other", "name": "Other", "models": ["shared"]}]
        rows = model_rows({"model": "shared", "provider": "custom:og", "providers": providers})
        self.assertEqual([(row.model, row.provider) for row in rows if row.current], [("shared", "og")])

    def test_a_model_that_is_not_one_plain_word_gets_no_row_of_its_own(self) -> None:
        rows = model_rows({"model": "x --global", "provider": "og", "providers": OPTIONS["providers"]})
        self.assertFalse([row for row in rows if row.current])
        self.assertEqual(rows[0].model, "claude-fable-5.1")


class PositionTests(unittest.TestCase):
    def test_the_header_says_where_the_highlight_is_once_the_rows_scroll(self) -> None:
        picker = ModelPicker(model_rows(OPTIONS), current=RUNNING)
        count = len(picker.rows)
        rows = lines_text(picker.lines(100, DEFAULT_THEME, 6))
        self.assertTrue(rows[0].startswith(f" Model (1/{count})"), rows[0])
        for _ in range(5):
            picker.handle_key(Key(DOWN))
        self.assertTrue(lines_text(picker.lines(100, DEFAULT_THEME, 6))[0].startswith(f" Model (6/{count})"))

    def test_a_list_that_fits_shows_its_count_alone(self) -> None:
        picker = ModelPicker(model_rows(OPTIONS), current=RUNNING)
        rows = lines_text(picker.lines(100, DEFAULT_THEME, len(picker.rows) + 2))
        self.assertTrue(rows[0].startswith(f" Model ({len(picker.rows)})  now"), rows[0])

    def test_the_position_counts_the_rows_the_filter_keeps(self) -> None:
        picker = ModelPicker(model_rows(OPTIONS), current=RUNNING)
        for char in "o":
            picker.handle_key(Key(CHAR, char))
        kept = len(picker.visible())
        self.assertTrue(lines_text(picker.lines(100, DEFAULT_THEME, 4))[0].startswith(f" Model (1/{kept})"))


class OpenedPickerTests(unittest.TestCase):
    def test_bare_model_opens_on_the_marked_current_model(self) -> None:
        app, client = make_app()
        type_text(app, "/model")
        app._on_input(Key(ENTER))
        self.assertEqual(client.calls, [("model.options", {"session_id": SID})])
        answer(app, client, OPTIONS)
        screen = drawn(app)
        header = next(index for index, line in enumerate(screen) if line.startswith(" Model ("))
        # The first row under the header's rule.
        self.assertTrue(screen[header + 2].startswith(f"❯ {CURRENT_MARK} {RUNNING}"), screen)


if __name__ == "__main__":
    unittest.main()
