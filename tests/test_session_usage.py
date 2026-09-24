"""Session usage: OMH utilization per Hermes host surface, read from state.db.

The fixture in ``test_reply_lint`` holds five sessions (tui, cli, desktop,
one with no source tag, and an archived, hidden cli session) and the tool
rows Hermes persists: an ``omh_*`` call re-persisted by a compaction, tool
rows without a ``tool_call_id`` and with an empty one, and ``skill_view``
results in the JSON, reference-file, compaction-placeholder and plain-text
shapes. Every count below is a hand count of those rows.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from omh.quality.session_usage import (
    COUNT_KEYS,
    SESSION_USAGE_SCHEMA_VERSION,
    SessionUsageError,
    build_session_usage,
    format_session_usage_summary,
)
from test_reply_lint import OTHER_SKILL_VIEW, _write_state_db


TUI_ROW = {
    "source": "tui",
    "sessions": 1,
    "tool_calls": 7,  # read_file (no id), c1 once, c2, c3, c4, c5, c10
    "omh_tool_calls": 2,
    "sessions_with_omh_tool": 1,
    "skill_views": 4,  # omh-plan SKILL.md, reviewer, omh-plan reference file, ulw-work
    "omh_skill_views": 3,  # the reviewer skill is not; the reference file names a catalog skill
    "sessions_with_omh_skill_view": 1,
    "sessions_with_any_omh": 1,
    "omh_tool_names": {"omh_recommend": 1, "omh_status": 1},
    "omh_skill_names": {"omh-plan": 2, "ulw-work": 1},
}
CLI_ROW = {
    "source": "cli",
    "sessions": 2,  # `older`, and `archived` (archived = 1, hidden = 1)
    "tool_calls": 2,
    "omh_tool_calls": 2,
    "sessions_with_omh_tool": 2,
    "skill_views": 0,
    "omh_skill_views": 0,
    "sessions_with_omh_skill_view": 0,
    "sessions_with_any_omh": 2,
    "omh_tool_names": {"omh_status": 1, "omh_todo": 1},
    "omh_skill_names": {},
}
DESKTOP_ROW = {
    "source": "desktop",
    "sessions": 1,
    "tool_calls": 5,  # c7, c8, c12, and two rows whose tool_call_id is ''
    "omh_tool_calls": 2,
    "sessions_with_omh_tool": 1,
    "skill_views": 3,
    "omh_skill_views": 2,  # the plain-text result naming the marker, and the placeholder naming omh-routing
    "sessions_with_omh_skill_view": 1,
    "sessions_with_any_omh": 1,
    "omh_tool_names": {"omh_hud": 1, "omh_todo": 1},
    "omh_skill_names": {"(unparsed)": 1, "omh-routing": 1},
}
UNTAGGED_ROW = {
    "source": "(none)",
    "sessions": 1,
    "tool_calls": 1,
    "omh_tool_calls": 0,
    "sessions_with_omh_tool": 0,
    "skill_views": 0,
    "omh_skill_views": 0,
    "sessions_with_omh_skill_view": 0,
    "sessions_with_any_omh": 0,
    "omh_tool_names": {},
    "omh_skill_names": {},
}
TOTALS = {
    "sessions": 5,
    "tool_calls": 15,
    "omh_tool_calls": 6,
    "sessions_with_omh_tool": 4,
    "skill_views": 7,
    "omh_skill_views": 5,
    "sessions_with_omh_skill_view": 2,
    "sessions_with_any_omh": 4,
}


def _append_tool_row(path: Path, row: tuple) -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_name, tool_call_id, timestamp) "
            "VALUES (?, ?, 'tool', ?, ?, ?, 0.0)",
            row,
        )
    connection.close()


class SessionUsageCountTests(unittest.TestCase):
    def test_rows_and_totals_are_the_hand_counts_per_source(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            payload = build_session_usage(home)

        self.assertEqual(payload["rows"], [UNTAGGED_ROW, CLI_ROW, DESKTOP_ROW, TUI_ROW])
        self.assertEqual(payload["totals"], TOTALS)
        self.assertEqual(payload["session_count"], 5)
        self.assertTrue(payload["observed"])
        self.assertEqual(set(TOTALS), set(COUNT_KEYS))

    def test_an_archived_hidden_session_counts_in_its_source_row(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            connection = sqlite3.connect(path)
            flags = connection.execute(
                "SELECT archived, hidden, ended_at FROM sessions WHERE id = 'archived'"
            ).fetchone()
            connection.close()

            cli = build_session_usage(home, source="cli")["rows"][0]

        # The fixture really carries the host's columns and flags; a reader that
        # added `WHERE archived = 0 AND hidden = 0` would report one cli session.
        self.assertEqual(flags, (1, 1, 0.3))
        self.assertEqual(cli["sessions"], 2)
        self.assertEqual(cli["omh_tool_names"], {"omh_status": 1, "omh_todo": 1})

    def test_a_re_persisted_tool_call_id_counts_once(self) -> None:
        # Rows 9 and 10 share tool_call_id c1 (a compaction re-persists the row).
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            payload = build_session_usage(home, source="tui")

        self.assertEqual(payload["rows"][0]["omh_tool_names"]["omh_status"], 1)
        self.assertEqual(payload["rows"][0]["omh_tool_calls"], 2)

    def test_a_row_without_a_tool_call_id_counts_by_its_own_row(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = build_session_usage(home, source="tui")["rows"][0]["tool_calls"]

            _append_tool_row(path, (30, "20260923_150513_4286a5", '{"ok": true}', "read_file", None))
            after_null = build_session_usage(home, source="tui")["rows"][0]["tool_calls"]
            _append_tool_row(path, (31, "20260923_150513_4286a5", '{"ok": true}', "read_file", ""))
            after_empty = build_session_usage(home, source="tui")["rows"][0]["tool_calls"]

        # Two rows with no tool_call_id are two calls, not one shared NULL key,
        # and an empty string is no id either, not a key every such row shares.
        self.assertEqual((before, after_null, after_empty), (7, 8, 9))

    def test_the_first_row_by_id_decides_a_re_persisted_skill_view(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            _append_tool_row(path, (30, "20260923_150513_4286a5", OTHER_SKILL_VIEW, "skill_view", "c20"))
            _append_tool_row(path, (31, "20260923_150513_4286a5", "[skill_view] name=omh-plan (1 chars)", "skill_view", "c20"))
            connection = sqlite3.connect(path)
            with connection:
                # An index the planner prefers returns the higher id first; the
                # reader's ORDER BY id is what makes the original row decide.
                connection.execute("CREATE INDEX messages_by_tool_name_desc ON messages(tool_name, id DESC)")
            connection.close()

            tui = build_session_usage(home, source="tui")["rows"][0]

        self.assertEqual(tui["skill_views"], 5)
        self.assertEqual(tui["omh_skill_views"], 3)
        self.assertEqual(tui["omh_skill_names"], {"omh-plan": 2, "ulw-work": 1})

    def test_omh_skill_signal_is_the_description_prefix_with_a_marker_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            _append_tool_row(
                path,
                (
                    30,
                    "desk",
                    json.dumps({"name": "omh-review", "description": "Review, [omh] mentioned later."}),
                    "skill_view",
                    "c20",
                ),
            )

            desktop = build_session_usage(home, source="desktop")["rows"][0]
            tui = build_session_usage(home, source="tui")["rows"][0]

        # The JSON shape with a description is decided by the description alone:
        # a marker in the body of a non-OMH description does not count, whatever
        # the name. Plain text falls back to the marker, and the ulw-* display
        # name in the tui row shows the `omh-` name prefix is not required.
        self.assertEqual(desktop["skill_views"], 4)
        self.assertEqual(desktop["omh_skill_views"], 2)
        self.assertEqual(desktop["omh_skill_names"], {"(unparsed)": 1, "omh-routing": 1})
        self.assertEqual(tui["omh_skill_names"], {"omh-plan": 2, "ulw-work": 1})

    def test_a_name_only_result_counts_when_the_name_is_a_catalog_skill(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            _append_tool_row(path, (30, "desk", json.dumps({"name": "reviewer", "file": "references/x.md"}), "skill_view", "c20"))
            _append_tool_row(path, (31, "desk", "[skill_view] name=reviewer (5 chars)", "skill_view", "c21"))
            _append_tool_row(path, (32, "desk", "[skill_view] name=", "skill_view", "c22"))
            _append_tool_row(path, (33, "desk", json.dumps({"name": "omh-ultrawork", "file": "SKILL.md"}), "skill_view", "c23"))

            desktop = build_session_usage(home, source="desktop")["rows"][0]

        # A reference-file load and a placeholder carry only a name: a name that
        # is not a catalog skill's does not count, a historical label (the
        # `omh-ultrawork` era of `ulw-work`) does, and the fixture's own
        # `omh-plan` reference file and `omh-routing` placeholder are in the hand counts.
        self.assertEqual(desktop["skill_views"], 7)
        self.assertEqual(desktop["omh_skill_views"], 3)
        self.assertEqual(desktop["omh_skill_names"], {"(unparsed)": 1, "omh-routing": 1, "omh-ultrawork": 1})

    def test_since_accepts_iso_or_epoch_and_drops_older_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            by_epoch = build_session_usage(home, since="3")
            by_iso = build_session_usage(home, since="1970-01-01T00:00:03Z")
            at_boundary = build_session_usage(home, since="4")
            past_desktop = build_session_usage(home, since="4.5")

        self.assertEqual([row["source"] for row in by_epoch["rows"]], ["desktop", "tui"])
        self.assertEqual(by_epoch["session_count"], 2)
        self.assertEqual(by_iso["rows"], by_epoch["rows"])
        self.assertEqual(by_iso["source"]["since_epoch"], 3.0)
        self.assertEqual(by_iso["source"]["since"], "1970-01-01T00:00:03Z")
        self.assertEqual([row["source"] for row in at_boundary["rows"]], ["desktop", "tui"])
        self.assertEqual([row["source"] for row in past_desktop["rows"]], ["tui"])

    def test_source_filter_keeps_one_surface_and_an_unknown_one_is_an_empty_observation(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            only_cli = build_session_usage(home, source="cli")
            untagged = build_session_usage(home, source="(none)")
            unknown = build_session_usage(home, source="slack")

        self.assertEqual(only_cli["rows"], [CLI_ROW])
        self.assertEqual(only_cli["source"]["source_filter"], "cli")
        # The label the payload prints for untagged rows selects them as a filter.
        self.assertEqual(untagged["rows"], [UNTAGGED_ROW])
        self.assertEqual(untagged["source"]["source_filter"], "(none)")
        self.assertEqual(unknown["rows"], [])
        self.assertEqual(unknown["session_count"], 0)
        self.assertTrue(unknown["observed"])
        self.assertEqual(unknown["totals"], {key: 0 for key in COUNT_KEYS})

    def test_missing_database_bad_since_and_an_older_schema_are_named_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            with self.assertRaisesRegex(SessionUsageError, "no Hermes state database"):
                build_session_usage(home)
            _write_state_db(home)
            # `nan` parses as a float and would silently disable the window; `inf`
            # would keep or drop everything. None of them is a stamp.
            for bad in ("nope", "nan", "inf", "-inf"):
                with self.assertRaisesRegex(SessionUsageError, f"--since must be an ISO-8601 timestamp or epoch seconds: {bad}"):
                    build_session_usage(home, since=bad)
            older = Path(tmp) / "older"
            older.mkdir()
            connection = sqlite3.connect(older / "state.db")
            with connection:
                connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, last_activity_at REAL)")
                connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
            connection.close()
            with self.assertRaisesRegex(SessionUsageError, "could not read"):
                build_session_usage(older)

    def test_payload_is_deterministic_read_only_and_carries_the_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = path.read_bytes()

            first = build_session_usage(home)
            second = build_session_usage(home)

            self.assertEqual(path.read_bytes(), before)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], SESSION_USAGE_SCHEMA_VERSION)
        self.assertEqual(first["source"]["kind"], "hermes_state_db")
        self.assertEqual(first["source"]["path"], str(path))
        self.assertEqual(first["counting"]["archived_hidden"], "included")
        self.assertIn("[omh] ", first["counting"]["omh_skill_view"])
        self.assertIn("persisted to a session", first["claim_boundary"])
        self.assertIn("not execution, review, CI, or merge evidence", first["claim_boundary"])

    def test_summary_lists_each_source_the_totals_and_the_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)
            payload = build_session_usage(home, since="3", source="tui")

        text = format_session_usage_summary(payload)
        lines = text.splitlines()
        self.assertEqual(lines[0], "OMH session usage: 1 session across 1 source")
        self.assertEqual(lines[1], "  since: 3    source_filter: tui")
        self.assertIn("  tui: sessions 1  tool calls 7  omh tool calls 2  sessions with omh tool 1", text)
        self.assertIn("Totals\n  sessions 1  tool calls 7", text)
        self.assertTrue(text.endswith("not execution, review, CI, or merge evidence."))
        empty = format_session_usage_summary(build_session_usage_empty())
        self.assertEqual(empty.splitlines()[0], "OMH session usage: 0 sessions across 0 sources")


def build_session_usage_empty() -> dict:
    with TemporaryDirectory() as tmp:
        home = Path(tmp) / ".hermes"
        _write_state_db(home)
        return build_session_usage(home, source="slack")


class SessionUsageCliTests(unittest.TestCase):
    def _common(self, tmp: str) -> list[str]:
        return ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes")]

    def test_json_payload_by_env_or_flag_and_plain_text_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _write_state_db(Path(tmp) / ".hermes")
            before = path.read_bytes()
            args = [*self._common(tmp), "quality-evidence", "session-usage"]

            env_status, env_stdout, env_stderr = run_cli(args)
            flag_status, flag_stdout, flag_stderr = run_cli([*args, "--json"], output_json=False)
            text_status, text_stdout, text_stderr = run_cli(args, output_json=False)

            self.assertEqual(path.read_bytes(), before)

        self.assertEqual((env_status, flag_status, text_status), (0, 0, 0), (env_stderr, flag_stderr, text_stderr))
        payload = json.loads(env_stdout)
        self.assertEqual(payload["schema_version"], SESSION_USAGE_SCHEMA_VERSION)
        self.assertEqual(payload["totals"], TOTALS)
        self.assertEqual(json.loads(flag_stdout), payload)
        self.assertFalse(text_stdout.lstrip().startswith("{"))
        self.assertIn("OMH session usage: 5 sessions across 4 sources", text_stdout)
        self.assertIn("Boundary", text_stdout)

    def test_filters_reach_the_payload_and_an_empty_window_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_state_db(Path(tmp) / ".hermes")

            status, stdout, stderr = run_cli(
                [
                    *self._common(tmp),
                    "quality-evidence", "session-usage", "--since", "1970-01-01T00:00:03Z", "--source", "slack",
                ]
            )
            none_status, none_stdout, none_stderr = run_cli(
                [*self._common(tmp), "quality-evidence", "session-usage", "--source", "(none)"]
            )

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["session_count"], 0)
        self.assertTrue(payload["observed"])
        self.assertEqual(payload["source"]["since"], "1970-01-01T00:00:03Z")
        self.assertEqual(payload["source"]["source_filter"], "slack")
        self.assertEqual(none_status, 0, none_stderr)
        self.assertEqual(json.loads(none_stdout)["rows"], [UNTAGGED_ROW])

    def test_a_missing_database_and_a_bad_since_exit_two(self) -> None:
        with TemporaryDirectory() as tmp:
            missing_status, missing_stdout, missing_stderr = run_cli(
                [*self._common(tmp), "quality-evidence", "session-usage"]
            )
            _write_state_db(Path(tmp) / ".hermes")
            since_status, since_stdout, since_stderr = run_cli(
                [*self._common(tmp), "quality-evidence", "session-usage", "--since", "nope"]
            )

        self.assertEqual(missing_status, 2)
        self.assertIn("no Hermes state database", missing_stderr)
        self.assertEqual(missing_stdout.strip(), "")
        self.assertEqual(since_status, 2)
        self.assertIn("--since must be an ISO-8601 timestamp or epoch seconds: nope", since_stderr)
        self.assertEqual(since_stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
