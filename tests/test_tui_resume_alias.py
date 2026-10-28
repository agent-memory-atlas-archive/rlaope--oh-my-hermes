"""`/resume` is `/sessions`: the menu offers it under its own name, and every form of it runs as /sessions' does.

Measured on a real TUI over Hermes' shared gateway (2026-10-28): `/res` highlighted Hermes' `/compress` (a name
that only contains it), `/resu` offered nothing and its Enter sent it to Hermes, which resolved it to its own
classic-only /resume; and `/sessions <title>` and `/resume <title>` were both refused `4001 not_found`, as the shared
gateway resumes ids only.
"""

from __future__ import annotations

import unittest

from test_tui_catalog import CATALOG, catalog_app
from test_tui_modes import SID, answer, make_app, notices, type_text
from omh.tui import slash
from omh.tui.app import RESUME_REFUSED, SESSION_LIST_LIMIT
from omh.tui.keys import ENTER, Key

# Hermes' own names next to the alias, as its catalog lists them.
HERMES_CATALOG = {**CATALOG, "pairs": [
    *CATALOG["pairs"],
    ["/compress", "Compress conversation context"],
    ["/egress", "Show Docker egress proxy status"],
    ["/restart", "Restart the gateway"],
    ["/resume", "Resume a previously-named session"],
]}


def picker_row(sid: str, title: str, count: int) -> dict:
    """A row as Hermes' shared gateway lists a stored session (`gateway/session_listing.py` `_picker_row`): no
    preview, no last_active."""
    return {"id": sid, "session_id": sid, "resolved_id": sid, "title": title, "source": "tui",
            "started_at": 1_791_900_000.0, "message_count": count, "model": "m", "running": False}


# The attach's `session.list`: the owner's live sessions first, then its stored ones, most recently active first.
LISTED = {"sessions": [
    {"id": "local-1f2e", "title": "", "preview": "", "started_at": 1_791_900_000.0, "message_count": 0, "source": "tui"},
    picker_row("20261027_084038_000003", "Release notes for 4.0.3", 31),
    picker_row("20261026_074038_000006", "Terminal capture driver", 46),
    picker_row("20261011_104038_00000c", "Weekly ops review", 3),
    picker_row("20261004_104038_00000f", "weekly OPS review", 5),  # Hermes keeps a title unique only in its case
    picker_row("20261002_104038_00000d", "terminal CAPTURE driver", 2),
]}
NOT_FOUND = {"code": RESUME_REFUSED, "message": "not_found"}


class MenuTests(unittest.TestCase):
    def names(self, typed: str) -> list[str]:
        return [completion.name for completion in slash.completions(typed, slash.catalog_pairs(HERMES_CATALOG))]

    def test_an_alias_is_offered_when_what_is_typed_starts_it_and_is_not_in_the_commands_name(self) -> None:
        sessions = slash.parse_slash("/sessions").command
        self.assertEqual(slash.completions("resu", slash.catalog_pairs(HERMES_CATALOG)),
                         [slash.Completion("resume", sessions.summary)])
        self.assertEqual(self.names("res"), ["resume", "restart", "compress", "egress"])  # a start before an inside
        self.assertEqual(self.names("re")[:5], ["redraw", "rewind", "reload-skills", "resume", "restart"])
        self.assertEqual(self.names("ses")[0], "sessions")  # its own name matches: no alias row
        self.assertNotIn("session", self.names("session"))
        self.assertNotIn("resume", self.names("sum"))  # an alias is offered on a start only
        self.assertNotIn("resume", self.names(""))
        self.assertTrue({"resume", "switch", "exit"} <= slash.known_names(slash.catalog_pairs(HERMES_CATALOG)))

    def test_res_and_resu_complete_to_resume_and_run_as_sessions_does(self) -> None:
        for typed, completed in (("/res", "/resume "), ("/resu", "/resume "), ("/sess", "/sessions ")):
            with self.subTest(typed=typed):
                app, client = catalog_app(HERMES_CATALOG)
                type_text(app, typed)
                self.assertEqual(app._menu_rows()[0][0], completed.strip())
                app._on_input(Key(ENTER))
                self.assertEqual(app.composer.text, completed)
                self.assertEqual(client.calls, [])
                app._on_input(Key(ENTER))
                self.assertEqual(client.calls, [("session.list", {"limit": SESSION_LIST_LIMIT})])


class TitleTests(unittest.TestCase):
    """`/sessions <arg>` and `/resume <arg>` against a shared gateway, which refuses anything but an id."""

    def typed(self, line: str, listing: dict | None = LISTED, refusal: dict = NOT_FOUND):
        app, client = make_app()
        type_text(app, line)
        app._on_input(Key(ENTER))
        answer(app, client, error=refusal)
        if listing is not None and client.calls[-1][0] == "session.list":
            answer(app, client, listing)
        return app, client

    def test_one_session_with_the_title_is_resumed_by_its_id(self) -> None:
        for line, sid in (("/sessions Release notes for 4.0.3", "20261027_084038_000003"),
                          ("/resume Release notes for 4.0.3", "20261027_084038_000003"),
                          ("/resume Terminal capture driver", "20261026_074038_000006"),  # the case match first
                          ("/resume RELEASE NOTES for 4.0.3", "20261027_084038_000003")):  # then ignoring case
            with self.subTest(line=line):
                app, client = self.typed(line)
                typed = line.split(" ", 1)[1]
                self.assertEqual(client.calls, [
                    ("session.resume", {"session_id": typed, "cols": 80}),
                    ("session.list", {"limit": SESSION_LIST_LIMIT}),
                    ("session.resume", {"session_id": sid, "cols": 80}),
                ])
                self.assertIsNone(app.picker)

    def test_a_title_several_sessions_share_opens_the_picker_filtered_by_it(self) -> None:
        app, client = self.typed("/resume Weekly Ops Review")  # no exact match; two ignoring case
        self.assertEqual([method for method, _params in client.calls], ["session.resume", "session.list"])
        self.assertEqual(app.picker.query, "Weekly Ops Review")
        self.assertEqual([row.id for row in app.picker.visible()],
                         ["20261011_104038_00000c", "20261004_104038_00000f"])
        self.assertEqual(notices(app)[-1], '2 sessions are titled "Weekly Ops Review": pick one.')
        app, client = self.typed("/resume Weekly ops review")  # the exact one, though another matches ignoring case
        self.assertEqual(client.calls[-1], ("session.resume", {"session_id": "20261011_104038_00000c", "cols": 80}))

    def test_a_title_none_has_opens_the_picker_filtered_by_it_and_sends_nothing_more(self) -> None:
        app, client = self.typed("/resume No such title")
        self.assertEqual([method for method, _params in client.calls], ["session.resume", "session.list"])
        self.assertEqual(app.picker.query, "No such title")
        self.assertEqual(notices(app)[-1], 'No session is titled "No such title": pick one, or change the filter.')
        self.assertFalse(any(text.startswith("Could not switch") for text in notices(app)))

    def test_a_listed_id_the_owner_refused_or_a_failed_listing_says_the_refusal(self) -> None:
        for line, listing in (("/resume 20261011_104038_00000c", LISTED), ("/resume Weekly ops review", None)):
            with self.subTest(line=line):
                app, client = self.typed(line, listing)
                if listing is None:
                    answer(app, client, error={"code": 5006, "message": "store locked"})
                self.assertEqual([method for method, _params in client.calls], ["session.resume", "session.list"])
                self.assertIsNone(app.picker)
                self.assertTrue(notices(app)[-1].startswith(f"Could not switch to {line.split(' ', 1)[1]} (4001)"))

    def test_an_id_switches_with_no_listing_and_a_pick_or_another_refusal_lists_nothing(self) -> None:
        app, client = make_app()
        type_text(app, "/resume 20261027_084038_000003")
        app._on_input(Key(ENTER))
        answer(app, client, {"session_id": "live-2", "stored_session_id": "20261027_084038_000003", "info": {},
                             "messages": []})
        self.assertEqual([method for method, _params in client.calls if method.startswith("session.")][:1],
                         ["session.resume"])
        self.assertNotIn("session.list", [method for method, _params in client.calls])
        app, client = make_app()
        app._switch_session("stored-9")  # what a pick runs: an id the person did not type
        answer(app, client, error=NOT_FOUND)
        type_text(app, "/resume stored-3")
        app._on_input(Key(ENTER))
        answer(app, client, error={"code": 4090, "message": "the session is open in another process"})
        self.assertEqual([method for method, _params in client.calls], ["session.resume", "session.resume"])
        self.assertEqual(len([text for text in notices(app) if text.startswith("Could not switch")]), 2)
        self.assertEqual(app.sid, SID)


if __name__ == "__main__":
    unittest.main()
