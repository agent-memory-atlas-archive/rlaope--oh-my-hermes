"""The awareness primer as a Hermes system prompt section, with its fallback.

Hermes 0.20.4 (tag v2026.8.18) added `register_system_prompt_section`: text
rendered once per new session and frozen into its system prompt. The primer is
session-stable, so on such a host it is registered there and `pre_llm_call`
stops carrying it for the sessions the section rendered for. A host without
the API, or a session the section did not render for, keeps the old path.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.maintenance.release import AWARENESS_PRIMER_CONTEXT_CHAR_LIMIT
from omh.plugin_bundle.omh import register
from omh.plugin_bundle.omh.awareness import awareness_primer_context
from omh.plugin_bundle.omh.hooks import llm_hooks

# Hermes' own limits (`hermes_cli/plugins_dispatch.py`), copied because the
# host is not importable here. The per-section cap is what `max_chars` may not
# exceed; the aggregate is shared by every plugin's rendered sections.
_HOST_MAX_SECTION_CHARS = 4000
_HOST_MAX_SECTIONS_TOTAL_CHARS = 8000
_HOST_HEADING = "## Plugin Context: "

_PLAIN_REQUEST = "migrate the database schema and fix the tests"


class _Ctx:
    """The two methods `register()` requires, and nothing else."""

    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_tool(self, name, *args, **kwargs) -> None:
        pass

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback


class _SectionCtx(_Ctx):
    """A host that offers system prompt sections, recording each registration."""

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[dict[str, object]] = []

    def register_system_prompt_section(self, id, content, *, position="after_memory", max_chars=4000):
        self.sections.append({"id": id, "content": content, "position": position, "max_chars": max_chars})


class _RejectingSectionCtx(_Ctx):
    def register_system_prompt_section(self, id, content, **kwargs):
        raise ValueError(f"system prompt section {id!r} is already registered")


def _session_info(session_id: str, **overrides: str) -> dict[str, str]:
    info = {
        "session_id": session_id,
        "model": "gpt-6-astra",
        "provider": "openai-codex",
        "platform": "cli",
        "profile_name": "default",
        "cwd": "/tmp/project-a",
    }
    info.update(overrides)
    return info


class SectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        llm_hooks._reset_awareness_section_state()
        self.addCleanup(llm_hooks._reset_awareness_section_state)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.omh_home = Path(tmp.name) / "omh"
        self.hermes_home = Path(tmp.name) / "hermes"
        self.omh_home.mkdir()
        self.hermes_home.mkdir()

    def first_turn_context(self, session_id: str) -> str:
        payload = llm_hooks.pre_llm_call(
            omh_home=str(self.omh_home),
            hermes_home=str(self.hermes_home),
            session_id=session_id,
            user_message=_PLAIN_REQUEST,
            is_first_turn=True,
        )
        return str((payload or {}).get("context", ""))


class RegistrationTests(SectionTestCase):
    def test_a_host_with_the_api_gets_one_bounded_section(self) -> None:
        ctx = _SectionCtx()
        register(ctx)
        self.assertEqual(len(ctx.sections), 1)
        section = ctx.sections[0]
        self.assertEqual(section["id"], "omh.awareness")
        self.assertEqual(section["position"], "after_memory")
        self.assertLessEqual(int(section["max_chars"]), _HOST_MAX_SECTION_CHARS)
        content = section["content"]
        self.assertTrue(callable(content))
        text = content(_session_info("s-1"))
        self.assertEqual(text, awareness_primer_context())
        self.assertLessEqual(len(text), AWARENESS_PRIMER_CONTEXT_CHAR_LIMIT)
        self.assertLessEqual(len(text), int(section["max_chars"]))
        # Framed the way the host frames it, the section leaves most of the
        # aggregate budget to other plugins.
        framed = f"{_HOST_HEADING}{section['id']}\n<!-- hermes-plugin-section-chars:{len(text)} -->\n\n{text}"
        self.assertLess(len(framed), _HOST_MAX_SECTIONS_TOTAL_CHARS // 2)
        # The hooks still register beside it.
        self.assertIn("pre_llm_call", ctx.hooks)

    def test_a_host_without_the_api_registers_the_hooks_and_keeps_the_primer_per_turn(self) -> None:
        ctx = _Ctx()
        register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertIn(awareness_primer_context(), self.first_turn_context("s-old-host"))

    def test_a_host_that_rejects_the_section_keeps_the_primer_per_turn(self) -> None:
        ctx = _RejectingSectionCtx()
        register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertIn(awareness_primer_context(), self.first_turn_context("s-rejected"))


class FrozenContentTests(SectionTestCase):
    def test_the_rendered_text_carries_no_session_or_turn_data(self) -> None:
        a = llm_hooks.awareness_system_prompt_section(_session_info("session-alpha-123"))
        b = llm_hooks.awareness_system_prompt_section(
            _session_info(
                "session-beta-456",
                model="claude-opus-5-5",
                provider="anthropic",
                platform="discord",
                profile_name="miku",
                cwd="/srv/other-checkout",
            )
        )
        self.assertEqual(a, b)
        for value in ("session-alpha-123", "gpt-6-astra", "/tmp/project-a", "cli", "default"):
            with self.subTest(value=value):
                self.assertNotIn(value, a)

    def test_the_rendered_text_is_the_same_after_a_turn_ran(self) -> None:
        # A turn writes its own state (delivery ledger, plan counters); the
        # section reads none of it.
        before = llm_hooks.awareness_system_prompt_section(_session_info("s-state"))
        self.first_turn_context("s-state")
        after = llm_hooks.awareness_system_prompt_section(_session_info("s-state"))
        self.assertEqual(before, after)


class PerTurnDeliveryTests(SectionTestCase):
    def test_a_session_the_section_rendered_for_gets_no_per_turn_primer(self) -> None:
        llm_hooks.awareness_system_prompt_section(_session_info("s-sectioned"))
        self.assertNotIn(awareness_primer_context(), self.first_turn_context("s-sectioned"))

    def test_a_session_the_section_did_not_render_for_still_gets_it(self) -> None:
        llm_hooks.awareness_system_prompt_section(_session_info("s-sectioned"))
        self.assertIn(awareness_primer_context(), self.first_turn_context("s-resumed-after-restart"))

    def test_a_primer_the_host_would_skip_is_not_recorded_as_delivered(self) -> None:
        # Over the host's per-section cap the host drops the text after
        # rendering it, so the session must keep the per-turn primer.
        oversized = "p" * (llm_hooks.AWARENESS_SECTION_MAX_CHARS + 1)
        with mock.patch.object(llm_hooks, "awareness_primer_context", return_value=oversized):
            llm_hooks.awareness_system_prompt_section(_session_info("s-oversized"))
            self.assertIn(oversized, self.first_turn_context("s-oversized"))

    def test_an_empty_session_id_is_never_recorded(self) -> None:
        llm_hooks.awareness_system_prompt_section(_session_info(""))
        self.assertIn(awareness_primer_context(), self.first_turn_context(""))


if __name__ == "__main__":
    unittest.main()
