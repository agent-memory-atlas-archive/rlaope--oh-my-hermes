"""The `✦ OMH` row the OMH TUI prints when OMH caught a model mistake (`catch_log`).

One row per catch, at the moment the HUD first carries it, and nothing
otherwise: the first payload of a session only sets the mark, so a resumed
session does not replay what it caught before; a kind the TUI cannot word is
skipped. `✦ OMH` steps through the launch look's logo colours and the words
take its label colour, so the row follows `/theme`; colour stays out of the text. Driven through `test_tui_modes`' app,
the HUD snapshots handed in as the poller would hand them.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from _tui_lines import line_text
from test_tui_modes import drawn, make_app

from omh.tui.panels import CATCH_EMOJI, HudSnapshot, catch_lines
from omh.tui.style import Style
from omh.tui.theme import (
    LAUNCH_PRESETS, build_theme, launch_palette, theme_for_name, theme_named, with_catch_colours, without_colour,
)
from omh.tui.transcript import NoticeBlock, block_rows
from omh.tui.width import cell_width, line_width

STORED = "20260919_140745_db409e"
DARK = "#1E2030"
LOOP = {"seq": 1, "kind": "repeat_loop", "period": 1, "laps": 3}
READ = {"seq": 2, "kind": "truncated_read", "offset": 1667}
PLAN = {"seq": 3, "kind": "plan_continued", "open_items": 2}
WAIT = {"seq": 4, "kind": "unarmed_wait"}


def payload(*catches: dict) -> dict:
    return {"privacy": "metadata_only", "catches": list(catches)}


def deliver(app, *catches: dict, session: str = STORED) -> None:
    """One poll that read `catches` for `session`."""
    snapshot = HudSnapshot(session, payload(*catches), 0.0)
    app.poller.take = lambda: snapshot
    app._take_hud()


def cells(row) -> list[tuple[str, object]]:
    """A drawn row as (character, style) pairs, however its spans were merged."""
    return [(char, span.style) for span in row for char in span.text]


def catch_rows(app) -> list[NoticeBlock]:
    return [block for block in app.transcript.blocks if isinstance(block, NoticeBlock) and block.mark]


class CatchLineTests(unittest.TestCase):
    def test_each_kind_reads_as_its_owner_approved_line(self) -> None:
        self.assertEqual(
            [line for _seq, line in catch_lines(payload(LOOP, READ, PLAN, WAIT))],
            [
                "🔁 stopped a loop of 3 identical calls",
                "📖 pointed the re-read at line 1667",
                "📋 kept going, plan has 2 open items",
                "⏰ flagged a wait with nothing armed to wake it",
            ],
        )

    def test_a_longer_cycle_and_a_single_item_are_worded_for_what_they_are(self) -> None:
        lines = catch_lines(payload(
            {"seq": 1, "kind": "repeat_loop", "period": 2, "laps": 4},
            {"seq": 2, "kind": "plan_continued", "open_items": 1},
        ))
        self.assertEqual([line for _seq, line in lines], [
            "🔁 stopped a loop of 2 calls repeated 4 times",
            "📋 kept going, plan has 1 open item",
        ])

    def test_an_unknown_kind_or_unusable_number_keeps_its_seq_with_no_line(self) -> None:
        lines = catch_lines(payload(
            {"seq": 5, "kind": "something_new"},
            {"seq": 6, "kind": "truncated_read", "offset": "1667"},
            {"seq": "7", "kind": "unarmed_wait"},
        ))
        self.assertEqual(lines, [(5, ""), (6, "")])
        self.assertIsNone(catch_lines({"catches": [WAIT]}))  # not a metadata-only payload
        self.assertIsNone(catch_lines({"error": "unreadable", "privacy": "metadata_only"}))

    def test_every_emoji_is_two_cells_and_has_no_variation_selector(self) -> None:
        for emoji in CATCH_EMOJI.values():
            with self.subTest(emoji=emoji):
                self.assertEqual(len(emoji), 1)
                self.assertEqual(cell_width(emoji), 2)
        self.assertEqual(cell_width("✦"), 1)


class CatchRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.client = make_app()
        self.app.stored_id = STORED

    def test_the_first_payload_of_a_session_prints_nothing(self) -> None:
        deliver(self.app, LOOP, READ)
        self.assertEqual(catch_rows(self.app), [])

    def test_a_new_catch_prints_exactly_one_one_line_row(self) -> None:
        deliver(self.app, LOOP)
        deliver(self.app, LOOP, READ)
        deliver(self.app, LOOP, READ)  # the same payload again prints nothing more

        (row,) = catch_rows(self.app)
        self.assertEqual((row.mark, row.text, row.one_line), ("✦ OMH", "📖 pointed the re-read at line 1667", True))
        self.assertIn("✦ OMH 📖 pointed the re-read at line 1667", drawn(self.app))
        self.assertEqual(self.client.calls, [])

    def test_several_new_catches_print_in_seq_order_and_unknown_kinds_are_skipped(self) -> None:
        deliver(self.app)
        deliver(self.app, PLAN, {"seq": 5, "kind": "something_new"}, {**WAIT, "seq": 6})
        deliver(self.app, PLAN, {"seq": 5, "kind": "something_new"}, {**WAIT, "seq": 6})

        self.assertEqual([row.text for row in catch_rows(self.app)], [
            "📋 kept going, plan has 2 open items",
            "⏰ flagged a wait with nothing armed to wake it",
        ])

    def test_another_session_s_payload_is_not_taken_and_a_switch_sets_a_new_mark(self) -> None:
        deliver(self.app)
        deliver(self.app, LOOP, session="another")  # not this session's: `_take_hud` drops it
        self.assertEqual(catch_rows(self.app), [])

        self.app.stored_id = "rotated"
        deliver(self.app, LOOP, READ, session="rotated")  # first payload of the new id: the mark only
        deliver(self.app, LOOP, READ, PLAN, session="rotated")
        self.assertEqual([row.text for row in catch_rows(self.app)], ["📋 kept going, plan has 2 open items"])

    def test_an_error_payload_is_not_the_first_reading(self) -> None:
        snapshot = HudSnapshot(STORED, {"error": "OSError"}, 0.0)
        self.app.poller.take = lambda: snapshot
        self.app._take_hud()
        deliver(self.app, LOOP, READ)  # the first readable payload: the mark only, no replay
        self.assertEqual(catch_rows(self.app), [])

    def test_a_ledger_that_started_over_prints_its_new_catches(self) -> None:
        deliver(self.app, {**LOOP, "seq": 40})
        deliver(self.app, {**READ, "seq": 1})  # the file was deleted: seq restarts below the mark
        deliver(self.app, {**READ, "seq": 1}, {**PLAN, "seq": 2})
        self.assertEqual([row.text for row in catch_rows(self.app)], ["📋 kept going, plan has 2 open items"])

    def test_an_empty_snapshot_is_not_the_first_reading(self) -> None:
        snapshot = HudSnapshot(STORED, None, 0.0)
        self.app.poller.take = lambda: snapshot
        self.app._take_hud()
        deliver(self.app, WAIT)  # still the first real payload
        self.assertEqual(catch_rows(self.app), [])


class CatchRowStyleTests(unittest.TestCase):
    def rows(self, theme, width: int = 80):
        block = NoticeBlock("📖 pointed the re-read at line 1667", one_line=True, mark="✦ OMH")
        return block_rows(block, width, theme)

    def test_the_mark_steps_through_the_launch_look_s_logo_and_the_words_take_its_label(self) -> None:
        skin = theme_for_name("amber")
        for look in LAUNCH_PRESETS:
            with self.subTest(look=look):
                theme = with_catch_colours(build_theme(skin, DARK), skin, DARK, look)
                (row,) = self.rows(theme)
                drawn_cells = cells(row)
                self.assertEqual("".join(cell for cell, _ in drawn_cells[:6]), "✦ OMH ")
                mark = [style for _, style in drawn_cells[:5]]
                self.assertEqual(mark, list(theme.catch_mark))
                self.assertTrue(all(style.bold for style in mark))
                self.assertGreater(len({style.fg for style in mark}), 1)  # a ramp, not one colour
                self.assertEqual(drawn_cells[-1][1], Style(fg=launch_palette(look, DARK).label))
        spectrum, violet = (with_catch_colours(build_theme(skin, DARK), skin, DARK, look) for look in ("spectrum", "violet"))
        self.assertNotEqual(spectrum.catch_mark, violet.catch_mark)

    def test_a_built_theme_carries_the_default_launch_look_s_colours(self) -> None:
        skin = theme_for_name("amber")
        built = build_theme(skin, DARK)
        self.assertEqual(built, with_catch_colours(built, skin, DARK, LAUNCH_PRESETS[0]))
        self.assertTrue(built.catch_mark and built.catch_words.fg)

    def test_a_theme_that_carries_no_catch_colours_draws_the_mark_in_brand_and_the_words_muted(self) -> None:
        theme = replace(theme_named("amber"), catch_mark=())
        (row,) = self.rows(theme)
        self.assertEqual({style for _, style in cells(row)[:6]}, {theme.brand})
        self.assertEqual(cells(row)[-1][1], theme.muted)

    def test_without_colour_the_text_is_the_same(self) -> None:
        (row,) = self.rows(without_colour(theme_named("amber")))
        self.assertEqual(line_text(row), "✦ OMH 📖 pointed the re-read at line 1667")

    def test_a_narrow_row_is_cut_with_an_ellipsis_inside_the_width(self) -> None:
        for width in (6, 7, 12, 20, 40):
            with self.subTest(width=width):
                (row,) = self.rows(theme_named("amber"), width)
                self.assertLessEqual(line_width(row), width)
                self.assertTrue(line_text(row).endswith("…"), line_text(row))
        (whole,) = self.rows(theme_named("amber"), 41)  # `✦ OMH `, 6; 📖, 2; the space and the sentence, 33
        self.assertEqual((line_width(whole), line_text(whole)), (41, "✦ OMH 📖 pointed the re-read at line 1667"))

    def test_other_notices_are_drawn_as_before(self) -> None:
        theme = theme_named("amber")
        (row,) = block_rows(NoticeBlock("💾 saved 1 memory", one_line=True), 80, theme)
        self.assertEqual({span.style for span in row}, {theme.faint})
        self.assertEqual(line_text(row), "💾 saved 1 memory")


if __name__ == "__main__":
    unittest.main()
