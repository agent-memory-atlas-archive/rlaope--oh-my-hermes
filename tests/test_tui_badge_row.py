"""The info line's badge and branch: `[ ⚚ OMH Native ]` at its left end, the session's git branch on its right
(owner's requests of 2026-10-27, made one line on 2026-10-28)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest import mock

from _platform_support import requires_posix
from _tui_lines import line_text
from omh.skin_pack import default_theme
from omh.tui.frame import badge_forms, branch_forms
from omh.tui.git_status import MIN_GAP_SECONDS, GitState, GitWatcher, parse_status, read_status
from omh.tui.layout import allocate
from omh.tui.modes import info_line
from omh.tui.style import Span
from omh.tui.theme import (
    LAUNCH_PRESETS, TEXT_CONTRAST, build_theme, contrast_ratio, launch_palette, launch_styles, launch_wordmark,
)

PORCELAIN = """# branch.oid 9d013ddd827e803eea5b5d2d1259acad43b295d9
# branch.head omh/terminology-unification
# branch.upstream origin/omh/terminology-unification
1 .M N... 100644 100644 100644 aaa bbb src/a.py
1 M. N... 100644 100644 100644 aaa bbb src/b.py
2 R. N... 100644 100644 100644 aaa bbb R100 src/c.py\tsrc/old.py
? notes.md
? scratch/
"""


def native(look: str, background: str):
    skin = default_theme()
    palette = launch_palette(look, background)
    return launch_wordmark("Native", skin, background, palette), launch_styles(skin, background, palette).brand


class StatusTests(unittest.TestCase):
    def test_porcelain_v2_names_the_branch_and_counts_tracked_and_untracked_files(self) -> None:
        self.assertEqual(parse_status(PORCELAIN), GitState("omh/terminology-unification", 3, 2))

    def test_a_detached_head_is_its_short_sha_and_an_unborn_one_is_nothing(self) -> None:
        detached = "# branch.oid 9d013ddd827e803eea5b5d2d1259acad43b295d9\n# branch.head (detached)\n"
        self.assertEqual(parse_status(detached), GitState("9d013dd", 0, 0))
        self.assertIsNone(parse_status("# branch.oid (initial)\n# branch.head (detached)\n"))
        self.assertEqual(parse_status("# branch.oid (initial)\n# branch.head main\n"), GitState("main", 0, 0))
        self.assertIsNone(parse_status("fatal: not a git repository\n"))

    def test_a_missing_or_slow_git_or_a_directory_outside_a_repository_reads_as_nothing(self) -> None:
        class Answer:
            def __init__(self, code: int = 0, out: str = PORCELAIN, slow: bool = False) -> None:
                self.returncode, self.out, self.slow, self.killed = code, out, slow, False

            def communicate(self, timeout=None):
                if self.slow and not self.killed:
                    raise subprocess.TimeoutExpired("git", timeout)
                return self.out, ""

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout=None) -> int:
                return -9

        def missing(*_args, **_kwargs):
            raise FileNotFoundError("git")

        slow = Answer(slow=True)
        for popen in (missing, lambda *_a, **_k: slow, lambda *_a, **_k: Answer(128, "")):
            with self.subTest(popen=popen):
                self.assertIsNone(read_status("/somewhere", popen=popen))
        self.assertTrue(slow.killed, "a git past its timeout is killed, not left running")

    def test_output_that_is_not_utf8_never_kills_the_reader(self) -> None:
        # #2342 delta review: a ref or a path that is not UTF-8 raised UnicodeDecodeError out of the read, the
        # watcher's thread died with a traceback on the TUI's terminal, and the row never updated again.
        class Undecodable:
            returncode, killed = 0, False

            def communicate(self, timeout=None):
                raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout=None) -> int:
                return -9

        answer = Undecodable()
        self.assertIsNone(read_status("/repo", popen=lambda *_a, **_k: answer))
        self.assertTrue(answer.killed)

    @requires_posix
    def test_a_real_branch_that_is_not_utf8_reads_and_the_watcher_lives_on(self) -> None:
        with TemporaryDirectory() as tmp:
            git = ["git", "-c", "init.defaultBranch=main", "-C", tmp]
            subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
            subprocess.run([b"git", b"-C", tmp.encode(), b"symbolic-ref", b"HEAD", b"refs/heads/caf\xe9"],
                           check=True, capture_output=True)
            self.assertEqual(read_status(tmp), GitState("caf\ufffd", 0, 0))
            changed = threading.Event()
            watcher = GitWatcher(tmp)
            watcher.start(changed.set)
            try:
                self.assertTrue(changed.wait(5.0))
                self.assertEqual(watcher.state, GitState("caf\ufffd", 0, 0))
                self.assertTrue(watcher._thread is not None and watcher._thread.is_alive())
            finally:
                watcher.stop()

    def test_the_read_is_read_only_and_bounded(self) -> None:
        seen = {}

        class Answer:
            returncode = 0

            def communicate(self, timeout=None):
                seen["timeout"] = timeout
                return PORCELAIN, ""

        def popen(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return Answer()

        self.assertEqual(read_status("/repo", popen=popen), GitState("omh/terminology-unification", 3, 2))
        self.assertEqual(seen["argv"][:5], ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "status"])
        self.assertEqual((seen["cwd"], seen["timeout"] > 0), ("/repo", True))

    def test_the_watcher_reads_off_the_caller_s_thread_and_wakes_on_a_change(self) -> None:
        changed = threading.Event()
        calls: list[str] = []

        def read(cwd: str) -> GitState:
            calls.append(threading.current_thread().name)
            return GitState("main", len(calls), 0)

        watcher = GitWatcher("/repo", read=read)
        self.assertIsNone(watcher.state)  # nothing until the thread has read
        watcher.start(changed.set)
        try:
            self.assertTrue(changed.wait(2.0))
            self.assertEqual(watcher.state.branch, "main")
            changed.clear()
            for _ in range(5):  # a burst of tool completions: at most one read for it, after the gap
                watcher.refresh()
            self.assertTrue(changed.wait(MIN_GAP_SECONDS + 2.0))
        finally:
            watcher.stop()
        self.assertTrue(all(name == "omh-git-status" for name in calls), calls)
        self.assertLessEqual(len(calls), 3)

    @requires_posix
    def test_stopping_mid_read_kills_git_at_once(self) -> None:
        # #2342 review: a quit while `git status` ran waited out the join and left git running after the TUI.
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / "git"
            fake.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
            fake.chmod(0o755)
            started = threading.Event()
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}"}):
                watcher = GitWatcher(tmp)
                track = watcher._track
                seen: list[subprocess.Popen] = []

                def tracked(process):
                    if process is not None:
                        seen.append(process)
                        started.set()
                    track(process)

                watcher._track = tracked
                watcher.start()
                self.assertTrue(started.wait(5.0))
                began = time.monotonic()
                watcher.stop()
                self.assertLess(time.monotonic() - began, 0.9)
            self.assertIsNotNone(seen[0].wait(timeout=2.0), "git is gone with the watcher")


class BottomLineTests(unittest.TestCase):
    """One line under the composer (owner, 2026-10-28): the badge at its left end, the branch first on its right."""

    THEME = build_theme(default_theme(), "#0D1117")
    RIGHT = ((Span("deepseek-v4.1-flash · ctx 12% · effort high", THEME.muted),), (Span("ctx 12% · effort high", THEME.muted),))
    STATE = GitState("omh/terminology-unification", 3, 128)

    def line(self, width: int, state: GitState | None = STATE, **facts: object) -> str:
        word, accent = native("spectrum", "#0D1117")
        return line_text(info_line(mode=facts.pop("mode", "normal"), busy=False, width=width, theme=self.THEME,
                                   right=self.RIGHT, badge=badge_forms(self.THEME, accent, word),
                                   branch=branch_forms(state, self.THEME), **facts))  # type: ignore[arg-type]

    def test_the_badge_on_the_left_and_the_branch_then_the_fields_on_the_right(self) -> None:
        text = self.line(130)
        self.assertEqual(len(text), 130)
        self.assertTrue(text.startswith("[ ⚚ OMH Native ]  ? for shortcuts"), text)
        self.assertTrue(text.endswith("branch:omh/terminology-unification | !3 ?128 · deepseek-v4.1-flash · ctx 12% · "
                                      "effort high"), text)

    def test_the_fields_go_first_then_the_badge_shrinks_and_the_label_and_the_branch_stay(self) -> None:
        branch = "branch:omh/terminology-unification | !3 ?128"  # 44 cells
        widths = {  # the left side is 33 cells with the whole badge, 26 with `[ ⚚ OMH ]`, 18 with `⚚`; the gap 3
            125: ("[ ⚚ OMH Native ]  ? for shortcuts", branch + " · ctx 12% · effort high"),
            104: ("[ ⚚ OMH Native ]  ? for shortcuts", branch + " · ctx 12% · effort high"),
            103: ("[ ⚚ OMH Native ]  ? for shortcuts", branch),
            80: ("[ ⚚ OMH Native ]  ? for shortcuts", branch),
            79: ("[ ⚚ OMH ]  ? for shortcuts", branch),
            73: ("[ ⚚ OMH ]  ? for shortcuts", branch),
            72: ("⚚  ? for shortcuts", branch),
            65: ("⚚  ? for shortcuts", branch),
            64: ("? for shortcuts", branch),  # the badge goes before the left side's words shorten
            61: ("? for shortcu…", branch),  # then the hint shortens, beside the branch
        }
        for width, (left, right) in widths.items():
            with self.subTest(width=width):
                text = self.line(width)
                self.assertLessEqual(len(text), width)
                self.assertTrue(text.startswith(left + "   "), text)
                self.assertTrue(text.endswith(right), text)
        # The label's glyph beside the whole branch; narrower, the branch's name is cut to 24 cells.
        self.assertEqual(self.line(50, mode="accept"), "⏵⏵    branch:omh/terminology-unification | !3 ?128")
        self.assertEqual(self.line(46, mode="accept"), "⏵⏵   branch:omh/terminology-unifica… | !3 ?128")

    def test_counts_that_are_zero_are_left_out_and_no_repository_shows_no_branch(self) -> None:
        self.assertTrue(self.line(120, GitState("main", 0, 0)).endswith("branch:main · deepseek-v4.1-flash · ctx 12% · "
                                                                       "effort high"))
        self.assertIn("branch:main | ?4 ·", self.line(120, GitState("main", 0, 4)))
        self.assertNotIn("branch:", self.line(120, None))

    def test_the_badge_and_the_branch_keep_their_colours(self) -> None:
        word, accent = native("spectrum", "#0D1117")
        row = info_line(mode="normal", busy=False, width=120, theme=self.THEME, badge=badge_forms(self.THEME, accent, word),
                        branch=branch_forms(self.STATE, self.THEME))
        styles = {span.text: span.style for span in row}
        self.assertEqual((styles["!3"], styles["?128"], styles["⚚"]), (self.THEME.warn, self.THEME.faint, accent))
        self.assertEqual(styles["omh/terminology-unification"], self.THEME.label)
        self.assertEqual([span.style for span in row if span.text in "Native"][:6], [span.style for span in word])

    def test_native_reads_in_every_launch_look_on_light_and_dark(self) -> None:
        for background in ("#0D1117", "#FFFFFF"):
            for look in LAUNCH_PRESETS:
                word, accent = native(look, background)
                with self.subTest(background=background, look=look):
                    for span in (*word, Span("⚚", accent)):
                        self.assertGreaterEqual(contrast_ratio(span.style.fg, background), TEXT_CONTRAST)
        sweep, _accent = native("spectrum", "#0D1117")
        self.assertGreater(len({span.style.fg for span in sweep}), 3, "the spectrum sweeps its hues across the word")

    def test_no_row_of_its_own_at_any_height(self) -> None:
        for rows in (24, 29, 30, 40):
            busy = allocate(rows, 80, sticky=False, tail_rows=3, slot_rows=12, composer_rows=1, agents_rows=9)
            idle = allocate(rows, 80, sticky=False, tail_rows=0, slot_rows=0, composer_rows=1)
            with self.subTest(rows=rows):
                self.assertEqual(idle.total, idle.composer + idle.rule + idle.bottom_rule + idle.info)
                self.assertLessEqual(busy.total, rows - 1)


if __name__ == "__main__":
    unittest.main()
