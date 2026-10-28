"""What a selection copies: no rail on the main screen, and the pager's own drag to copy (the owner's request of
2026-10-28, "the blue line on the left gets copied with the text").

On the main screen the transcript lives in the terminal's own scrollback and
the terminal's own selection copies it, so the rail a block hangs on is a cell
painted in its colour that holds a space: whatever a terminal copies there, it
is never a bar glyph. The pager (alternate screen) takes the mouse with a held
button's motion: a drag selects, its release copies the text without the rail
or a reply's margin, and the footer says what the copy did.
"""

from __future__ import annotations

import base64
import unittest

from _tui_lines import line_text
from _vt_screen import VTScreen
from test_tui_modes import drawn, make_app
from omh.tui.app import COPY_NOTE_SECONDS, App
from omh.tui.clipboard import COPY_OSC52, COPY_PBCOPY, COPY_UNCONFIRMED, copy_route
from omh.tui.keys import BUTTON_LEFT, CHAR, WHEEL_DOWN, WHEEL_UP, Key, Mouse
from omh.tui.pager import FOOTER_KEYS, WHEEL_LINES, Pager
from omh.tui.screen import paint_line
from omh.tui.style import Span
from omh.tui.theme import DEFAULT_LAUNCH, DEFAULT_THEME, build_theme, on_band, with_prompt_band, without_colour
from omh.tui.transcript import (
    NOTICE_ERROR,
    NOTICE_OK,
    NOTICE_WARN,
    TOOL_ERROR,
    TOOL_OK,
    TOOL_RUNNING,
    AssistantBlock,
    CommandBlock,
    NoticeBlock,
    SteerBlock,
    ThinkingBlock,
    ToolGroup,
    ToolRow,
    TurnSummaryBlock,
    UserBlock,
    block_rows,
)
from omh.skin_pack import SKIN_THEMES

# Every glyph a terminal could copy as a bar beside the text: the rail's old `┃` and its kin.
BAR_GLYPHS = set("┃│▎▍▌▋▏▕▐┆┊╎║█")
REPLY = "first line of the reply\nsecond line"
KITTY = {"TERM": "xterm-kitty"}


def every_block() -> list[object]:
    command = CommandBlock("/model", done=True)
    command.parts = [("switched", NOTICE_OK), ("a warning", NOTICE_WARN), ("it failed", NOTICE_ERROR)]
    return [
        UserBlock("a prompt\nits second line"),
        UserBlock("/omh-docs write it", known=frozenset({"omh-docs"})),
        command,
        AssistantBlock("**hello** there\n\n```sh\nls\n```"),
        ToolGroup([ToolRow("a", "terminal", "ls", TOOL_RUNNING), ToolRow("b", "read_file", "a.py", TOOL_OK),
                   ToolRow("c", "terminal", "pytest", TOOL_ERROR, output=("1 failed",))], done=True),
        NoticeBlock("a notice", NOTICE_WARN),
        NoticeBlock("allowed", NOTICE_OK),
        SteerBlock("go left"),
        ThinkingBlock("thinking about it", done=True),
        TurnSummaryBlock(34.0, 3, 1200),
    ]


def themes() -> list[tuple[str, object]]:
    banded = with_prompt_band(build_theme(SKIN_THEMES[0], "#1E1E1E"), SKIN_THEMES[0], "#1E1E1E", DEFAULT_LAUNCH)
    return [("default", DEFAULT_THEME), ("prompt band", banded), ("NO_COLOR", without_colour(DEFAULT_THEME))]


class NoBarOnTheMainScreenTests(unittest.TestCase):
    def test_no_block_draws_a_bar_glyph_in_any_cell(self) -> None:
        for name, theme in themes():
            for block in every_block():
                for expanded in (False, True):
                    with self.subTest(theme=name, block=type(block).__name__, expanded=expanded):
                        rows = block_rows(block, 60, theme, expanded=expanded)
                        self.assertTrue(rows)
                        screen = VTScreen(len(rows) + 1, 60)
                        screen.feed("\r\n".join(paint_line(row, 60) for row in rows))
                        drawn_glyphs = set("".join(screen.lines()))
                        self.assertFalse(drawn_glyphs & BAR_GLYPHS, screen.lines())

    def test_the_rail_is_a_reversed_space_in_its_colour_so_it_shows_without_colour_too(self) -> None:
        for name, theme in themes():
            with self.subTest(theme=name):
                (row, *_rest) = block_rows(UserBlock("hi"), 40, theme)
                self.assertEqual((row[0].text, row[1].text), (" ", " "))
                self.assertTrue(row[0].style.reverse)
                roles = on_band(theme, theme.prompt_band) if theme.prompt_band.bg is not None else theme
                self.assertEqual(row[0].style.fg, roles.dim.fg)  # the prompt's grey, measured against its band
                screen = VTScreen(2, 40)
                screen.feed(paint_line(row, 40))
                self.assertIn(7, screen.attrs_at(0, 0))  # painted: reverse video, whatever the colour depth
                self.assertNotIn(7, screen.attrs_at(0, 2))
                self.assertEqual(screen.line(0), "  ❯ hi")  # what a terminal's selection copies: two spaces, no bar

    def test_each_rail_keeps_its_blocks_colour(self) -> None:
        theme = DEFAULT_THEME
        reply = block_rows(AssistantBlock("ok"), 40, theme)
        call = block_rows(ToolGroup([ToolRow("a", "terminal", "ls", TOOL_ERROR)], done=True), 40, theme)
        self.assertEqual({row[0].style.fg for row in reply}, {theme.brand.fg})
        self.assertEqual({row[0].style.fg for row in call}, {theme.error.fg})


def reply_app(environ: dict[str, str] | None = None, platform: str = "linux") -> tuple[App, list[str]]:
    """An app with a prompt and a reply, the history pager open on them; what `pbcopy` was handed."""
    app, _client = make_app(rows=12, cols=60, environ=environ or {})
    app.platform = platform
    copied: list[str] = []
    app.pbcopy = lambda text: copied.append(text) is None  # type: ignore[method-assign]  # never the real clipboard
    app.transcript.add_user("explain it")
    app.transcript.blocks.append(AssistantBlock(REPLY, model="deepseek-v4.1-flash"))
    app._history("")
    drawn(app)
    return app, copied


def row_of(app: App, text: str) -> int:
    """The 1-based screen row showing `text`, as a report counts it."""
    return next(index for index, line in enumerate(app.terminal.screen.lines()) if text in line) + 1  # type: ignore[attr-defined]


def drag(app: App, start: tuple[int, int], end: tuple[int, int]) -> None:
    """A left press at `start`, the held button's motion to `end`, and the release there; (x, y) from 1."""
    app._on_input(Mouse(BUTTON_LEFT, *start))
    app._on_input(Mouse(BUTTON_LEFT, *end, motion=True))
    app._on_input(Mouse(BUTTON_LEFT, *end, pressed=False))


def osc52(app: App) -> list[str]:
    """What each OSC 52 the app wrote put on the clipboard."""
    sent = [text.removeprefix("52;c;") for text in app.terminal.screen.osc if text.startswith("52;c;")]  # type: ignore[attr-defined]
    return [base64.b64decode(text.rstrip("\x07")).decode("utf-8") for text in sent]


class PagerDragTests(unittest.TestCase):
    def test_a_drag_copies_the_text_without_the_rail_or_the_margin_through_osc52(self) -> None:
        app, copied = reply_app(KITTY)
        first, second = row_of(app, "first line"), row_of(app, "second line")
        drag(app, (1, first), (60, second))  # from the rail's own cell to the row's far end
        self.assertEqual(osc52(app), [REPLY])
        self.assertEqual(copied, [])
        footer = drawn(app)[-1]
        self.assertEqual(footer, f"✓ copied {len(REPLY)} chars to clipboard")

    def test_a_drag_from_mid_row_copies_from_there(self) -> None:
        app, _copied = reply_app(KITTY)
        y = row_of(app, "first line")
        x = app.terminal.screen.line(y - 1).index("line of") + 1  # type: ignore[attr-defined]
        drag(app, (x, y), (x + len("line of") - 1, y))
        self.assertEqual(osc52(app), ["line of"])

    def test_the_selection_is_drawn_reversed_while_it_is_made(self) -> None:
        app, _copied = reply_app(KITTY)
        y = row_of(app, "first line")
        x = app.terminal.screen.line(y - 1).index("first") + 1  # type: ignore[attr-defined]
        app._on_input(Mouse(BUTTON_LEFT, x, y))
        app._on_input(Mouse(BUTTON_LEFT, x + 4, y, motion=True))
        drawn(app)
        screen = app.terminal.screen  # type: ignore[attr-defined]
        self.assertEqual([7 in screen.attrs_at(y - 1, x - 1 + at) for at in range(6)], [True] * 5 + [False])
        self.assertEqual(osc52(app), [])  # nothing copied before the release

    def test_the_highlight_leaves_the_rail_and_the_margin_as_they_are(self) -> None:
        app, _copied = reply_app(KITTY)
        first, second = row_of(app, "first line"), row_of(app, "second line")
        app._on_input(Mouse(BUTTON_LEFT, 1, first))
        app._on_input(Mouse(BUTTON_LEFT, 60, second, motion=True))
        drawn(app)
        screen = app.terminal.screen  # type: ignore[attr-defined]
        self.assertIn(7, screen.attrs_at(first - 1, 0))  # the rail's cell still shows, reversed as it always is
        self.assertEqual([7 in screen.attrs_at(first - 1, column) for column in range(1, 5)], [False] * 3 + [True])

    def test_a_double_click_copies_a_word_and_a_triple_click_the_row(self) -> None:
        now = [100.0]
        app, _client = make_app(rows=12, cols=60, environ=KITTY, clock=lambda: now[0])
        app.transcript.blocks.append(AssistantBlock("run omh-docs/api.md now"))
        app._history("")
        drawn(app)
        y = row_of(app, "omh-docs")
        x = app.terminal.screen.line(y - 1).index("docs") + 1  # type: ignore[attr-defined]
        for _ in range(3):
            app._on_input(Mouse(BUTTON_LEFT, x, y))
            app._on_input(Mouse(BUTTON_LEFT, x, y, pressed=False))
            now[0] += 0.1
        self.assertEqual(osc52(app), ["omh-docs/api.md", "run omh-docs/api.md now"])

    def test_a_plain_click_copies_nothing(self) -> None:
        app, _copied = reply_app(KITTY)
        y = row_of(app, "first line")
        app._on_input(Mouse(BUTTON_LEFT, 8, y))
        app._on_input(Mouse(BUTTON_LEFT, 8, y, pressed=False))
        self.assertEqual(osc52(app), [])
        self.assertIn(FOOTER_KEYS[:20], drawn(app)[-1])

    def test_the_notice_goes_after_a_while_and_the_keys_come_back(self) -> None:
        now = [100.0]
        app, _client = make_app(rows=12, cols=60, environ=KITTY, clock=lambda: now[0])
        app.transcript.blocks.append(AssistantBlock(REPLY))
        app._history("")
        drawn(app)
        drag(app, (1, row_of(app, "first line")), (60, row_of(app, "second line")))
        self.assertIn("copied", drawn(app)[-1])
        self.assertEqual(app._read_timeout() <= COPY_NOTE_SECONDS, True)  # the loop wakes to take it off
        now[0] += COPY_NOTE_SECONDS
        app._expire_notification()
        self.assertIn(FOOTER_KEYS[:20], drawn(app)[-1])

    def test_a_copy_nothing_confirms_says_it_was_sent(self) -> None:
        for environ, why in (({"TERM": "xterm-256color"}, "terminal may not support OSC 52"),
                             ({"TERM": "tmux-256color", "TMUX": "/tmp/tmux-501/default,1,0"},
                              "tmux passes it on only with set-clipboard on")):
            with self.subTest(environ=environ):
                app, copied = reply_app(environ, platform="linux")
                drag(app, (1, row_of(app, "first line")), (60, row_of(app, "first line")))
                self.assertEqual(osc52(app), ["first line of the reply"])
                self.assertEqual(copied, [])
                self.assertEqual(drawn(app)[-1], f"⚠ copy sent ({why})")

    def test_on_a_local_mac_a_terminal_that_ignores_osc52_gets_pbcopy(self) -> None:
        app, copied = reply_app({"TERM_PROGRAM": "Apple_Terminal"}, platform="darwin")
        drag(app, (1, row_of(app, "first line")), (60, row_of(app, "first line")))
        self.assertEqual((copied, osc52(app)), (["first line of the reply"], []))
        self.assertEqual(drawn(app)[-1], "✓ copied 23 chars to clipboard")

    def test_a_pbcopy_that_fails_falls_back_to_osc52_and_says_it_was_sent(self) -> None:
        app, _copied = reply_app({"TERM_PROGRAM": "Apple_Terminal"}, platform="darwin")
        app.pbcopy = lambda text: False  # type: ignore[method-assign]
        drag(app, (1, row_of(app, "first line")), (60, row_of(app, "first line")))
        self.assertEqual(osc52(app), ["first line of the reply"])
        self.assertEqual(drawn(app)[-1], "⚠ copy sent (terminal may not support OSC 52)")

    def test_a_key_lets_the_selection_go(self) -> None:
        app, _copied = reply_app(KITTY)
        y = row_of(app, "first line")
        app._on_input(Mouse(BUTTON_LEFT, 5, y))
        app._on_input(Mouse(BUTTON_LEFT, 12, y, motion=True))
        app._on_input(Key(CHAR, "k"))
        app._on_input(Mouse(BUTTON_LEFT, 12, y, pressed=False))
        self.assertEqual(osc52(app), [])
        drawn(app)
        self.assertNotIn(7, app.terminal.screen.attrs_at(y - 1, 6))  # type: ignore[attr-defined]


class CopyRouteTests(unittest.TestCase):
    def test_which_way_a_copy_goes(self) -> None:
        cases = [
            ({"TERM": "xterm-kitty"}, "darwin", COPY_OSC52),
            ({"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty"}, "darwin", COPY_OSC52),
            ({"TERM_PROGRAM": "WezTerm"}, "linux", COPY_OSC52),
            ({"TERM": "xterm-kitty", "TMUX": "/tmp/t,1,0"}, "linux", COPY_UNCONFIRMED),  # tmux may not pass it on
            ({"TERM_PROGRAM": "Apple_Terminal"}, "darwin", COPY_PBCOPY),
            ({"TERM_PROGRAM": "iTerm.app"}, "darwin", COPY_PBCOPY),  # its OSC 52 waits on a preference
            ({"TERM_PROGRAM": "tmux", "TMUX": "/tmp/t,1,0"}, "darwin", COPY_PBCOPY),
            ({"TERM_PROGRAM": "iTerm.app", "SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}, "darwin", COPY_UNCONFIRMED),
            ({"TERM": "xterm-256color"}, "linux", COPY_UNCONFIRMED),
        ]
        for environ, platform, route in cases:
            with self.subTest(environ=environ, platform=platform):
                self.assertEqual(copy_route(environ, platform), route)


class PagerWheelTests(unittest.TestCase):
    def test_the_wheel_scrolls_the_pager_its_reports_now_reach(self) -> None:
        pager = Pager("log", [(Span(f"row {index}"),) for index in range(40)])
        pager.frame(30, 10, DEFAULT_THEME)
        self.assertEqual(pager.handle_mouse(Mouse(WHEEL_DOWN, 5, 5), 10, 0.0), "")
        self.assertEqual(pager.top, WHEEL_LINES)
        pager.handle_mouse(Mouse(WHEEL_DOWN, 5, 5), 10, 0.0)
        pager.handle_mouse(Mouse(WHEEL_UP, 5, 5), 10, 0.0)
        self.assertEqual(pager.top, WHEEL_LINES)
        for _ in range(20):
            pager.handle_mouse(Mouse(WHEEL_DOWN, 5, 5), 10, 0.0)
        self.assertEqual(pager.top, 40 - 9)  # no further than the last page

    def test_in_the_app_the_wheel_scrolls_the_history_pager(self) -> None:
        app, _client = make_app(rows=12, cols=60)
        for index in range(30):
            app.transcript.add_notice(f"notice {index}")
        app._history("")
        top = drawn(app)[0]
        app._on_input(Mouse(WHEEL_DOWN, 10, 3))
        self.assertNotEqual(drawn(app)[0], top)
        app._on_input(Mouse(WHEEL_UP, 10, 3))
        self.assertEqual(drawn(app)[0], top)
        self.assertTrue(app.terminal.mouse_drag)  # type: ignore[attr-defined]

    def test_a_selection_ends_with_the_wheel(self) -> None:
        pager = Pager("log", [(Span(f"row {index}"),) for index in range(40)])
        pager.frame(30, 10, DEFAULT_THEME)
        pager.handle_mouse(Mouse(BUTTON_LEFT, 1, 2), 10, 0.0)
        pager.handle_mouse(Mouse(BUTTON_LEFT, 5, 3, motion=True), 10, 0.0)
        pager.handle_mouse(Mouse(WHEEL_DOWN, 5, 3), 10, 0.0)
        self.assertEqual(pager.handle_mouse(Mouse(BUTTON_LEFT, 5, 3, pressed=False), 10, 0.0), "")

    def test_rows_of_blocks_lose_their_gutter_in_the_copy(self) -> None:
        rows = block_rows(UserBlock("hello\nthere"), 40, DEFAULT_THEME)
        pager = Pager("log", rows)
        pager.frame(40, 6, DEFAULT_THEME)
        pager.handle_mouse(Mouse(BUTTON_LEFT, 1, 1), 6, 0.0)
        pager.handle_mouse(Mouse(BUTTON_LEFT, 40, 2, motion=True), 6, 0.0)
        copied = pager.handle_mouse(Mouse(BUTTON_LEFT, 40, 2, pressed=False), 6, 0.0)
        self.assertEqual(copied, "❯ hello\n  there")  # the prompt's mark stays; the rail's two cells do not
        self.assertEqual([line_text(row) for row in rows], ["  ❯ hello", "    there"])


if __name__ == "__main__":
    unittest.main()
