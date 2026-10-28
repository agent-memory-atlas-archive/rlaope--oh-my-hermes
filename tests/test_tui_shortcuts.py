"""The OMH TUI's key table: one list for the `?` overlay, `/keys` and the docs.

The Keys table in docs/INSTALLATION.md is compared row for row with
`shortcuts.SHORTCUTS`, so the two cannot drift. The Ctrl letters the table
names are compared with the Ctrl letters that do anything in the app, in both
directions, so a key bound without a row, or a row for a key nothing reads,
fails here too.
"""

from __future__ import annotations

from pathlib import Path
import re
import signal
import string
import unittest

from _tui_lines import line_text
from test_tui_app import FakePoller, ScriptedTerminal
from test_tui_modes import RecordingClient, make_app, type_text
from omh.tui.app import App
from omh.tui.gateway import ServerRequest
from omh.tui.keys import CHAR, ENTER, ESCAPE, LEFT, Key, Paste
from omh.tui.pager import ALT_SCREEN_OFF, ALT_SCREEN_ON
from omh.tui.shortcuts import SHORTCUTS, TITLE, shortcut_lines
from omh.tui.theme import DEFAULT_THEME
from omh.tui.width import line_width

INSTALLATION = Path(__file__).resolve().parents[1] / "docs" / "INSTALLATION.md"
TABLE_HEADER = "| Key | Does |"
QUESTION_MARK = Key(CHAR, "?")


def documented_keys() -> list[tuple[str, str]]:
    """The rows of INSTALLATION's Keys table, backticks removed."""
    lines = INSTALLATION.read_text(encoding="utf-8").splitlines()
    start = lines.index(TABLE_HEADER) + 2  # the header and its rule
    rows: list[tuple[str, str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        keys, does = (cell.strip().replace("`", "") for cell in line.strip().strip("|").split("|"))
        rows.append((keys, does))
    return rows


def ctrl_letters_named() -> set[str]:
    return {letter.lower() for keys, _does in SHORTCUTS for letter in re.findall(r"Ctrl\+([A-Z])\b", keys)}


def observed(app: App, client: RecordingClient) -> tuple[object, ...]:
    """Everything a key can change that a person or Hermes would notice, once the frame after it is drawn (a full
    redraw's erase goes out with that frame)."""
    terminal: ScriptedTerminal = app.terminal  # type: ignore[assignment]
    app._render()
    return (
        app.composer.text,
        app.composer.caret,
        len(client.calls),
        len(app.transcript.blocks),
        app.transcript.details_expanded,
        app.exit_code,
        app.pager is not None,
        app.search is not None,
        len(terminal.writes),
        terminal.suspended,
    )


class TableTests(unittest.TestCase):
    def test_the_installation_keys_table_is_the_code_table(self) -> None:
        self.assertEqual(documented_keys(), list(SHORTCUTS))

    def test_every_row_names_keys_and_what_they_do_once(self) -> None:
        keys = [keys for keys, _does in SHORTCUTS]
        self.assertEqual(len(keys), len(set(keys)))
        for row in SHORTCUTS:
            with self.subTest(row=row):
                self.assertTrue(all(cell.strip() == cell and cell for cell in row))
                self.assertNotIn("|", "".join(row))  # a pipe would split the doc table's cell
                self.assertNotIn("`", "".join(row))

    def test_the_list_row_names_every_picker_that_takes_its_keys(self) -> None:
        # The session, rewind and bare /model pickers each say these keys on the info line; the list names all three.
        from omh.tui.model_picker import KEYS as MODEL_KEYS
        from omh.tui.rewind_picker import KEYS as REWIND_KEYS
        from omh.tui.session_picker import KEYS as SESSION_KEYS

        (row,) = [does for keys, does in SHORTCUTS if keys == "↑ / ↓, PageUp / PageDown, Enter, Esc"]
        for command, said in (("/sessions", SESSION_KEYS), ("/rewind", REWIND_KEYS), ("/model", MODEL_KEYS)):
            with self.subTest(picker=command):
                self.assertIn(command, row)
                self.assertTrue(said[0].startswith("↑↓ move · enter ") and said[0].endswith(" · esc closes"), said)

    def test_the_steer_row_names_the_key_and_the_command_that_works_everywhere(self) -> None:
        # Ctrl+Enter steers typed text where it is a key; /steer <text> works everywhere; the list names both, and
        # the send-now row names Ctrl+J, which every terminal sends apart from Enter.
        (steer,) = [does for keys, does in SHORTCUTS if keys == "Ctrl+Enter"]
        self.assertIn("/steer <text>", steer)
        (send_now,) = [does for keys, does in SHORTCUTS if keys == "Ctrl+Enter, Ctrl+J"]
        self.assertIn("oldest queued prompt", send_now)

    def test_the_ctrl_letters_named_are_the_ones_that_do_something(self) -> None:
        named = ctrl_letters_named()
        self.assertEqual(named, set("acdejklprstouvwxyz"))
        for letter in string.ascii_lowercase:
            changed = False
            for draft, undone in (("", False), ("one two", False), ("one two", True)):  # Ctrl+Y redoes the undone draft
                app, client = make_app()
                if letter == "p":  # Ctrl+P folds the plan: it does something only while a plan shows
                    from omh.tui.panels import HudSnapshot
                    from test_tui_panels import hud_payload

                    app.hud = HudSnapshot(app.stored_id, hud_payload([{"text": "Ship it", "state": "active"}]), 0.0)
                type_text(app, draft)
                app._on_input(Key(LEFT))
                if undone:
                    app._on_input(Key(CHAR, "_", ctrl=True))
                before = observed(app, client)
                app._on_input(Key(CHAR, letter, ctrl=True))
                changed = changed or observed(app, client) != before
            with self.subTest(letter=letter):
                # Ctrl+Z suspends only where there is job control (no SIGTSTP on Windows).
                self.assertEqual(changed, letter in named and (letter != "z" or hasattr(signal, "SIGTSTP")))


class OverlayLinesTests(unittest.TestCase):
    def test_every_key_and_description_is_on_the_overlay_at_any_width(self) -> None:
        for width in (20, 36, 60, 80, 120, 200):
            lines = shortcut_lines(width, DEFAULT_THEME)
            text = " ".join(" ".join(line_text(line).split()) for line in lines)
            with self.subTest(width=width):
                self.assertEqual(line_text(lines[0]), TITLE)
                self.assertTrue(all(line_width(line) <= width for line in lines))
                for keys, does in SHORTCUTS:
                    self.assertIn(" ".join(keys.split()), text)
                    self.assertIn(" ".join(does.split()), text)

    def test_a_wide_terminal_puts_the_description_beside_the_keys_and_a_narrow_one_under_them(self) -> None:
        wide = [line_text(line) for line in shortcut_lines(120, DEFAULT_THEME)]
        self.assertIn("Enter" + " " * 41 + "Sends the composer", wide)
        narrow = [line_text(line) for line in shortcut_lines(40, DEFAULT_THEME)]
        at = narrow.index("Enter")
        self.assertEqual(narrow[at + 1], "  Sends the composer")

    def test_the_keys_are_bold_and_the_text_is_text(self) -> None:
        line = next(line for line in shortcut_lines(120, DEFAULT_THEME) if line_text(line).startswith("Ctrl+T"))
        self.assertEqual(line[0].style, DEFAULT_THEME.user)  # bold, no hue: a key is no state
        self.assertEqual(line[-1].style, DEFAULT_THEME.text)


class GuideKeyTests(unittest.TestCase):
    def open(self, app: App) -> bool:
        return app.pager is not None  # a bool: a failed assertion must not print the whole pager

    def paged(self, app: App) -> str:
        assert app.pager is not None
        return "\n".join(line_text(line) for line in app.pager.lines)

    def test_a_question_mark_on_an_empty_composer_opens_the_key_list(self) -> None:
        app, client = make_app()
        app._on_input(QUESTION_MARK)
        self.assertTrue(self.open(app))
        self.assertEqual(app.composer.text, "")
        self.assertIn(ALT_SCREEN_ON, app.terminal.writes)  # type: ignore[attr-defined]
        paged = self.paged(app)
        self.assertIn(TITLE, paged)
        self.assertIn("Shift+Tab", paged)
        self.assertEqual(client.calls, [])

    def test_esc_and_q_close_it(self) -> None:
        for key in (Key(ESCAPE), Key(CHAR, "q")):
            with self.subTest(key=key):
                app, _client = make_app()
                app._on_input(QUESTION_MARK)
                app._on_input(key)
                self.assertFalse(self.open(app))
                self.assertIn(ALT_SCREEN_OFF, "".join(app.terminal.writes))  # type: ignore[attr-defined]
                self.assertEqual(app.composer.text, "")

    def test_a_question_mark_in_a_composer_with_text_is_text(self) -> None:
        for draft in ("why", " ", "what\n"):
            with self.subTest(draft=draft):
                app, _client = make_app()
                app.composer.replace(0, 0, draft)
                app._on_input(QUESTION_MARK)
                self.assertFalse(self.open(app))
                self.assertEqual(app.composer.text, draft + "?")

    def test_a_pasted_or_modified_question_mark_is_not_the_key(self) -> None:
        for event in (Paste("?"), Key(CHAR, "?", alt=True), Key(CHAR, "?", ctrl=True), Key(CHAR, "？")):
            with self.subTest(event=event):
                app, _client = make_app()
                app._on_input(event)
                self.assertFalse(self.open(app))

    def test_a_question_mark_typed_as_an_answer_is_the_answer(self) -> None:
        now = [100.0]
        app, client = make_app(clock=lambda: now[0])
        app._on_server_request(ServerRequest("srq-1", "vault.code", {}))
        now[0] += 1.0
        app._on_input(QUESTION_MARK)
        self.assertFalse(self.open(app))
        app._on_input(Key(ENTER))
        self.assertEqual(client.answers, [("srq-1", {"result": {"value": "?"}})])

    def test_a_question_mark_on_an_approval_opens_nothing_and_allows_nothing(self) -> None:
        now = [100.0]
        app, client = make_app(clock=lambda: now[0])
        app._on_server_request(ServerRequest("srq-1", "approval", {"command": "rm -rf ./x"}))
        now[0] += 1.0
        app._on_input(QUESTION_MARK)
        self.assertFalse(self.open(app))
        self.assertEqual(client.answers, [])

    def test_the_keys_command_opens_the_same_list(self) -> None:
        by_key, _client = make_app()
        by_key._on_input(QUESTION_MARK)
        by_command, client = make_app()
        type_text(by_command, "/keys")
        by_command._on_input(Key(ENTER))
        self.assertEqual(self.paged(by_command), self.paged(by_key))
        self.assertEqual(client.calls, [])

    def test_help_points_at_the_key_list_instead_of_keeping_its_own(self) -> None:
        app, _client = make_app()
        type_text(app, "/help")
        app._on_input(Key(ENTER))
        self.assertFalse(self.open(app))  # its output is the transcript's, however long
        text = app.transcript.blocks[-1].parts[0][0]
        self.assertIn("Keys: ? on an empty composer, or /keys, lists every key.", text)
        self.assertIn("/keys ", text)  # and the command is listed with the TUI's own
        self.assertNotIn("Ctrl+D", text)  # the key rows themselves are gone from /help
        self.assertNotIn("Alt+Enter", text)

    def test_an_app_without_a_terminal_size_surprise_still_builds_its_list(self) -> None:
        app = App(ScriptedTerminal(rows=6, cols=20), RecordingClient(), {}, poller=FakePoller())  # type: ignore[arg-type]
        app._on_input(QUESTION_MARK)
        assert app.pager is not None
        self.assertTrue(all(line_width(line) <= 20 for line in app.pager.lines))


if __name__ == "__main__":
    unittest.main()
