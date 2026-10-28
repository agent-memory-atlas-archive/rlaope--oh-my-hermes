from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from _local_package import load_local_package

load_local_package()
from omh.config_adapter import external_dirs
from omh.paths import resolve_paths
from omh.targets import (
    build_target_change_notice,
    inspect_target_observation,
    record_target_observation,
    summarize_target_registry,
)


class TargetRegistryTests(unittest.TestCase):
    def test_records_single_to_multi_and_multi_to_single_without_forgetting_history(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes-a")

            first = record_target_observation(paths, source="setup")
            self.assertTrue(first["persisted"])
            self.assertEqual(first["topology"]["mode"], "single_agent_target")
            self.assertFalse(first["topology"]["changed"])

            second = record_target_observation(
                paths,
                source="chat:discord",
                source_metadata={
                    "agent_ref": "agent-b",
                    "hermes_home": str(root / ".hermes-b"),
                },
            )
            self.assertEqual(second["topology"]["transition"], "single_to_multi")
            self.assertEqual(second["topology"]["mode"], "multi_agent_targets")
            self.assertTrue(second["topology"]["requires_skill_scope_awareness"])

            back_to_one = inspect_target_observation(
                paths,
                source="chat:discord",
                source_metadata={
                    "agent_ref": "agent-a",
                    "hermes_home": str(root / ".hermes-a"),
                    "agent_count": "1",
                },
            )
            self.assertFalse(back_to_one["persisted"])
            self.assertEqual(back_to_one["topology"]["transition"], "multi_to_single")
            self.assertEqual(back_to_one["topology"]["mode"], "single_agent_target")
            self.assertGreaterEqual(back_to_one["topology"]["known_target_count"], 2)
            self.assertEqual(back_to_one["topology"]["active_agent_count"], 1)

    def test_change_notice_distinguishes_pending_and_applied_persistence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes-a")

            record_target_observation(paths, source="setup")
            pending = inspect_target_observation(
                paths,
                source="chat:slack",
                source_metadata={
                    "agent_ref": "agent-b",
                    "hermes_home": str(root / ".hermes-b"),
                    "agent_count": "2",
                },
            )
            pending_notice = build_target_change_notice(pending)

            self.assertIsNotNone(pending_notice)
            assert pending_notice is not None
            self.assertEqual(pending_notice["action"], "ask_to_apply_target_change")
            self.assertEqual(pending_notice["persistence"], "pending_user_confirmation")
            self.assertIn("multiple Hermes agent targets", pending_notice["body"])
            self.assertEqual(pending_notice["apply_payload"]["source"], "chat:slack")
            self.assertEqual(pending_notice["apply_payload"]["source_metadata"]["agent_ref"], "agent-b")
            self.assertEqual(pending_notice["apply_payload"]["source_metadata"]["agent_count"], "2")

            applied = record_target_observation(
                paths,
                source="chat:slack",
                source_metadata={
                    "agent_ref": "agent-b",
                    "hermes_home": str(root / ".hermes-b"),
                    "agent_count": "2",
                },
            )
            applied_notice = build_target_change_notice(applied, auto_applied=True)

            self.assertIsNotNone(applied_notice)
            assert applied_notice is not None
            self.assertEqual(applied_notice["action"], "target_change_applied")
            self.assertEqual(applied_notice["persistence"], "persisted")
            self.assertEqual(summarize_target_registry(paths)["mode"], "multi_agent_targets")

    def test_an_observed_home_s_registration_is_read_from_that_home(self) -> None:
        # The observed target is another Hermes home than the one `paths`
        # resolved. Its config names the skills directory relative to
        # itself; read from `paths.hermes_home` instead it names nothing, and
        # the apply would add the directory a second time.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes-a")
            other = root / "b" / ".hermes"
            other.mkdir(parents=True)
            # Resolved on both sides: `relpath` across the macOS `/var` alias
            # would climb out of the wrong prefix.
            relative = Path(os.path.relpath(paths.skills_dir.resolve(), other.resolve())).as_posix()
            config = other / "config.yaml"
            config.write_text(f"skills:\n  external_dirs:\n    - {relative}\n", encoding="utf-8", newline="")

            record_target_observation(
                paths,
                source="chat:slack",
                source_metadata={"agent_ref": "agent-b", "hermes_home": str(other), "agent_count": "2"},
                ensure_config=True,
                registered_dir=paths.skills_dir,
            )

            self.assertEqual(external_dirs(config.read_text(encoding="utf-8")), [relative])


if __name__ == "__main__":
    unittest.main()
