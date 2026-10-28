"""Independent regressions for the five install-lifecycle audit findings.

Each class pins one finding from the immutable 2026-10-24 audit, not the
implementation that happened to be current when the test was written:

* F1 shared-store deletion during a profile-targeted full uninstall
* F2 registration-only opt-out when the skill store differs from the runtime
* F3 one unreadable profile config aborting every later profile
* F4 ``update --json`` hiding a profile refresh failure
* F5 generation-venv ownership disagreement between update and uninstall

The fixtures are synthetic and local. They never touch real OMH/Hermes homes,
never use the network and never execute an installer.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _cli_harness import run_cli
from _local_package import load_local_package
from _platform_support import requires_symlinks

load_local_package()

from omh.commands.setup import _sync_hermes_profiles, read_config  # noqa: E402
from omh.commands import setup as setup_module  # noqa: E402
from omh.install.config_adapter import external_dirs, plugin_omh_home_setting  # noqa: E402
from omh.install.installer import uninstall_skill_pack  # noqa: E402
from omh.install.lifecycle_safety import store_removal_plan  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _external_paths(config: str) -> set[Path]:
    return {Path(value).resolve() for value in external_dirs(config)}


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="install-lifecycle-safety-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.hermes = self.root / "hermes"
        self.omh = self.root / "omh"
        self.hermes.mkdir()
        self.omh.mkdir()
        self.addCleanup(self._restore_environ)
        self._saved_environ = {
            key: os.environ.get(key)
            for key in ("OMH_VENV_DIR", "OMH_BIN_DIR", "XDG_DATA_HOME", "OMH_OUTPUT")
        }

    def _setup_primary(self) -> None:
        status, _, stderr = run_cli(
            ["--hermes-home", str(self.hermes), "--omh-home", str(self.omh),
             "setup", "--core", "--yes", "--no-interactive", "--no-menubar", "--json"]
        )
        self.assertEqual(status, 0, stderr)
        self.assertTrue((self.omh / "manifest.json").is_file(), "real setup must prove ownership")

    def _file_snapshot(self, root: Path) -> dict[str, bytes]:
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def _restore_environ(self) -> None:
        for key, value in self._saved_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class SharedStoreUninstallTests(_Fixture):
    """F1: a bot profile must not tear down a store the primary still uses."""

    def _profile_binding(self, bot: Path, store: Path) -> None:
        _write(
            bot / "config.yaml",
            "plugins:\n"
            "  entries:\n"
            "    omh:\n"
            "      settings:\n"
            f"        omh_home: {store}\n",
        )

    def test_targeted_uninstall_preserves_a_shared_store_and_its_records(self) -> None:
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        self._profile_binding(bot, self.omh)
        _write(self.hermes / "config.yaml", f"skills:\n  external_dirs:\n    - {self.omh / 'skills'}\n")
        sentinel = self.omh / "runtime" / "user-record-sentinel.json"
        _write(sentinel, '{"synthetic_user_record": true}\n')
        _write(self.omh / "skills" / "primary-only" / "SKILL.md", "# primary skill\n")
        _write(self.hermes / "plugins" / "omh" / "plugin.yaml", "name: omh\n")
        primary_before = (self.hermes / "config.yaml").read_bytes()

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--keep-command", "--json"]
        )
        payload = json.loads(stdout)

        self.assertEqual(status, 0, stderr)
        self.assertTrue(self.omh.is_dir(), "shared OMH store was deleted")
        self.assertTrue(sentinel.is_file(), "shared runtime record was deleted")
        self.assertTrue((self.omh / "skills" / "primary-only" / "SKILL.md").is_file())
        self.assertTrue((self.hermes / "plugins" / "omh" / "plugin.yaml").is_file())
        self.assertIn((self.omh / "skills").resolve(),
                      _external_paths(read_config(self.hermes / "config.yaml")))
        self.assertEqual((self.hermes / "config.yaml").read_bytes(), primary_before)
        self.assertIn("would_remove", payload)

    def test_unshared_store_is_still_removed(self) -> None:
        """Ownership control: refusal must not degrade into never removing."""
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        private = self.root / "private-omh"
        private.mkdir()
        # An unshared path is not ownership evidence: install the real pack.
        status, _, stderr = run_cli([
            "--hermes-home", str(bot), "--omh-home", str(private),
            "setup", "--core", "--yes", "--no-interactive", "--no-menubar", "--json",
        ])
        self.assertEqual(status, 0, stderr)
        _write(private / "runtime" / "record.json", "{}\n")
        self._profile_binding(bot, private)
        # The primary uses A, not private/skills: private really is unshared.
        _write(self.hermes / "config.yaml",
               "plugins:\n  entries:\n    omh:\n      settings:\n"
               f"        omh_home: {self.omh}\n"
               f"skills:\n  external_dirs:\n    - {self.omh / 'skills'}\n")
        primary_before = (self.hermes / "config.yaml").read_bytes()
        self.assertNotEqual(Path(plugin_omh_home_setting(primary_before.decode("utf-8"))).resolve(),
                            private.resolve())
        self.assertNotIn((private / "skills").resolve(),
                         _external_paths(primary_before.decode("utf-8")))

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--all", "--keep-command", "--json"]
        )
        self.assertEqual(status, 0, stderr)
        self.assertFalse(private.exists(), "an unshared profile-local store was not removed")
        self.assertTrue(self.omh.is_dir(), json.loads(stdout))
        self.assertEqual((self.hermes / "config.yaml").read_bytes(), primary_before)

    def test_skills_only_reference_still_protects_a_shared_store(self) -> None:
        """A primary need not share runtime to share the target's skills."""
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        self._profile_binding(bot, self.omh)
        primary_runtime = self.root / "primary-runtime"
        self._profile_binding(self.hermes, primary_runtime)
        config_path = self.hermes / "config.yaml"
        config_path.write_bytes(config_path.read_bytes() +
                                f"skills:\n  external_dirs:\n    - {self.omh / 'skills'}\n".encode("utf-8"))
        sentinel = self.omh / "runtime" / "shared-record.json"
        _write(sentinel, '{"shared": true}\n')
        skill = self.omh / "skills" / "primary-only" / "SKILL.md"
        _write(skill, "# shared skill\n")
        before = (self.hermes / "config.yaml").read_bytes()

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "--omh-home", str(self.omh),
             "uninstall", "--all", "--keep-command", "--json"]
        )

        self.assertEqual(status, 0, stderr)
        self.assertTrue(self.omh.is_dir(), "skills-only sharing was ignored")
        self.assertEqual(sentinel.read_bytes(), b'{"shared": true}\n')
        self.assertTrue(skill.is_file())
        self.assertEqual((self.hermes / "config.yaml").read_bytes(), before)
        self.assertNotIn(self.omh.resolve(),
                         {Path(value).resolve() for value in json.loads(stdout)["removed_paths"]})

    def test_a_skills_reference_spelled_through_a_variable_still_protects_a_shared_store(self) -> None:
        # Hermes expands `${VAR}` in `skills.external_dirs`, so the primary
        # still loads skills from this store and the profile's uninstall
        # must keep it. Read with `~` only, the entry named nothing.
        bot = self.hermes / "profiles" / "bot"
        self._profile_binding(bot, self.omh)
        self._profile_binding(self.hermes, self.root / "primary-runtime")
        config_path = self.hermes / "config.yaml"
        config_path.write_bytes(config_path.read_bytes() + b"skills:\n  external_dirs:\n    - ${OMH_TEST_SHARED_STORE}/skills\n")
        paths = OmhPaths(omh_home=self.omh, hermes_home=bot)

        with mock.patch.dict(os.environ, {"OMH_TEST_SHARED_STORE": self.omh.as_posix()}):
            plan = store_removal_plan(paths, remove_profiles=False)

        self.assertEqual((plan["status"], plan["homes"]), ("kept", [str(self.hermes)]))

    def test_a_skills_reference_relative_to_its_home_still_protects_a_shared_store(self) -> None:
        # Hermes anchors a relative entry at its own home, so `../omh/skills`
        # in the primary names this store whatever directory uninstall runs
        # from. Resolved against an unrelated cwd it named nothing.
        bot = self.hermes / "profiles" / "bot"
        self._profile_binding(bot, self.omh)
        self._profile_binding(self.hermes, self.root / "primary-runtime")
        config_path = self.hermes / "config.yaml"
        config_path.write_bytes(config_path.read_bytes() + b"skills:\n  external_dirs:\n    - ../omh/skills\n")
        paths = OmhPaths(omh_home=self.omh, hermes_home=bot)

        with TemporaryDirectory() as unrelated:
            cwd = os.getcwd()
            os.chdir(unrelated)
            try:
                plan = store_removal_plan(paths, remove_profiles=False)
            finally:
                os.chdir(cwd)

        self.assertEqual((plan["status"], plan["homes"]), ("kept", [str(self.hermes)]))

    def test_shared_store_dry_run_does_not_delete_or_rewrite_files(self) -> None:
        """Mutation control, independently of whether the proposed plan is safe."""
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        _write(bot / "SOUL.md", "Bot profile identity.\n")
        self._setup_primary()
        _write(self.omh / "runtime" / "sentinel.json", '{"keep": true}\n')
        before = self._file_snapshot(self.root)

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--dry-run", "--keep-command", "--json"]
        )

        self.assertEqual(status, 0, stderr)
        self.assertTrue(json.loads(stdout)["dry_run"])
        self.assertEqual(self._file_snapshot(self.root), before)

    def test_shared_store_dry_run_does_not_plan_store_deletion(self) -> None:
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        _write(bot / "SOUL.md", "Bot profile identity.\n")
        self._setup_primary()

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--all", "--dry-run", "--keep-command", "--json"]
        )

        self.assertEqual(status, 0, stderr)
        self.assertNotIn(self.omh.resolve(),
                         {Path(value).resolve() for value in json.loads(stdout)["would_remove"]},
                         "dry-run advertises destructive shared-store removal")


class RegistrationOptOutTests(_Fixture):
    """F2: opt-out follows the managed skills registration, not the runtime."""

    def _separate_runtime_setup(self) -> tuple[Path, Path, list[Path]]:
        # The immutable parent-repros.py F2 flow: primary setup owns A/skills,
        # while an existing bot plugin binding continues to name runtime B.
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        runtime = self.root / "bot-runtime"
        foreign = [self.root / "personal-one" / "skills", self.root / "personal-two" / "skills"]
        for directory in foreign:
            _write(directory / "personal" / "SKILL.md", "# not owned by OMH\n")
        _write(runtime / "runtime" / "sentinel.json", '{"preserve_runtime_B": true}\n')
        _write(
            bot / "config.yaml",
            "plugins:\n  entries:\n    omh:\n      settings:\n"
            f"        omh_home: {runtime}\n"
            "skills:\n  external_dirs:\n"
            + "".join(f"    - {directory}\n" for directory in foreign),
        )
        self._setup_primary()
        config = read_config(bot / "config.yaml")
        self.assertIn((self.omh / "skills").resolve(), _external_paths(config))
        self.assertEqual(Path(plugin_omh_home_setting(
            read_config(self.hermes / "config.yaml"))).resolve(), self.omh.resolve())
        self.assertEqual(Path(plugin_omh_home_setting(config)).resolve(), runtime.resolve(),
                         "setup must retain bot's preexisting B binding")
        self.assertTrue((bot / "plugins" / "omh" / ".omh-plugin-manifest.json").is_file())
        return bot, runtime, foreign

    def test_registration_only_removes_store_registration_and_keeps_runtime(self) -> None:
        bot, runtime, foreign = self._separate_runtime_setup()
        plugin_before = self._file_snapshot(bot / "plugins" / "omh")
        runtime_sentinel = runtime / "runtime" / "sentinel.json"
        runtime_before = runtime_sentinel.read_bytes()

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--registration-only", "--json"]
        )
        payload = json.loads(stdout)
        config = read_config(bot / "config.yaml")

        self.assertEqual(status, 0, stderr)
        self.assertTrue(payload["config_changed"], payload.get("config_message"))
        self.assertNotIn((self.omh / "skills").resolve(), _external_paths(config))
        self.assertEqual(Path(plugin_omh_home_setting(config)).resolve(), runtime.resolve())
        self.assertTrue(runtime.is_dir())
        self.assertEqual(runtime_sentinel.read_bytes(), runtime_before)
        self.assertEqual(self._file_snapshot(bot / "plugins" / "omh"), plugin_before)
        for directory in foreign:
            self.assertIn(directory.resolve(), _external_paths(config))
            self.assertTrue((directory / "personal" / "SKILL.md").is_file())
        # Installed-but-unregistered is the product's persistent opt-out marker.
        rows = _sync_hermes_profiles(Namespace(
            omh_home=str(self.omh), hermes_home=str(self.hermes),
            scope=None, force=False, dry_run=False,
        ))
        self.assertEqual({row["profile"]: row for row in rows}["bot"]["status"], "unregistered_kept")
        self.assertNotIn((self.omh / "skills").resolve(),
                         _external_paths(read_config(bot / "config.yaml")))

    def test_registration_only_preserves_foreign_same_basename_directories(self) -> None:
        """Negative ownership control: basename 'skills' proves no ownership."""
        bot, runtime, foreign = self._separate_runtime_setup()
        snapshots = {directory: self._file_snapshot(directory) for directory in foreign}
        sentinel = runtime / "runtime" / "sentinel.json"
        before = sentinel.read_bytes()

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(bot), "uninstall", "--registration-only", "--json"]
        )

        self.assertEqual(status, 0, stderr)
        self.assertIsInstance(json.loads(stdout), dict)
        config = read_config(bot / "config.yaml")
        self.assertEqual(Path(plugin_omh_home_setting(config)).resolve(), runtime.resolve())
        self.assertEqual(sentinel.read_bytes(), before)
        for directory in foreign:
            self.assertIn(directory.resolve(), _external_paths(config))
            self.assertEqual(self._file_snapshot(directory), snapshots[directory])


class CorruptProfileUpdateTests(_Fixture):
    """F3: a bad profile is a named row, never an uncaught exception."""

    def _sync_rows(self, args: Namespace) -> list[dict]:
        try:
            return _sync_hermes_profiles(args)
        except (OSError, UnicodeError) as exc:
            self.fail(f"one unreadable profile aborted the whole sync: {type(exc).__name__}: {exc}")

    def test_invalid_utf8_profile_does_not_stop_later_profiles(self) -> None:
        bad = self.hermes / "profiles" / "a-bad"
        good = self.hermes / "profiles" / "z-good"
        bad.mkdir(parents=True)
        good.mkdir()
        _write(good / "SOUL.md", "Healthy profile identity.\n")
        ghost = self.hermes / "profiles" / "log-cache-shell"
        _write(ghost / "logs" / "sentinel", "Keep incidental runtime files.\n")
        (ghost / "cache").mkdir()
        ghost_before = self._file_snapshot(ghost)
        (bad / "config.yaml").write_bytes(b"plugins:\n  x: \xff\n")
        bad_before = self._file_snapshot(bad)

        args = Namespace(
            omh_home=str(self.omh),
            hermes_home=str(self.hermes),
            scope=None,
            force=False,
            dry_run=False,
        )
        rows = self._sync_rows(args)

        by_profile = {row["profile"]: row for row in rows}
        self.assertIn("a-bad", by_profile)
        self.assertEqual(by_profile["a-bad"]["status"], "failed")
        self.assertTrue(by_profile["a-bad"].get("error"))
        self.assertEqual(self._file_snapshot(bad), bad_before, "invalid UTF-8 profile was mutated")
        self.assertIn("z-good", by_profile)
        self.assertNotEqual(by_profile["z-good"]["status"], "failed")
        self.assertTrue((good / "plugins" / "omh").is_dir())
        self.assertNotIn("log-cache-shell", by_profile)
        self.assertEqual(self._file_snapshot(ghost), ghost_before)
        self.assertEqual(sorted(p.name for p in ghost.iterdir()), ["cache", "logs"])

    def test_oserror_profile_config_is_a_named_failure(self) -> None:
        good = self.hermes / "profiles" / "z-good"
        good.mkdir(parents=True)
        _write(good / "config.yaml", "model: healthy-model\n")
        bad = self.hermes / "profiles" / "a-bad"
        bad.mkdir()
        _write(bad / "config.yaml", "plugins: {}\n# do not replace bad config\n")
        bad_before = self._file_snapshot(bad)
        reads: list[Path] = []
        bad_reads: list[dict[str, bytes]] = []
        bad_config = (bad / "config.yaml").resolve()

        def read_one_profile(path: Path, *args, **kwargs):
            identity = Path(path).resolve()
            reads.append(identity)
            if identity == bad_config:
                bad_reads.append(self._file_snapshot(bad))
                self.assertEqual(bad_reads[-1], bad_before, "mutation before unreadable config preflight")
                raise OSError("permission denied for a-bad/config.yaml")
            return read_config(path, *args, **kwargs)

        args = Namespace(
            omh_home=str(self.omh),
            hermes_home=str(self.hermes),
            scope=None,
            force=False,
            dry_run=False,
        )
        with mock.patch("omh.commands.setup.read_config", side_effect=read_one_profile), mock.patch(
            "omh.commands.setup.install_plugin_bundle", wraps=setup_module.install_plugin_bundle
        ) as bundle:
            rows = self._sync_rows(args)

        by_profile = {row["profile"]: row for row in rows}
        self.assertEqual(by_profile["a-bad"]["status"], "failed")
        self.assertIn("permission denied for a-bad/config.yaml", by_profile["a-bad"]["error"])
        self.assertEqual(bad_reads, [bad_before], "fault must fire at the first and only bad read")
        self.assertEqual(self._file_snapshot(bad), bad_before, "bad profile state was mutated")
        self.assertFalse((bad / "plugins" / "omh").exists())
        self.assertNotIn(bad.resolve(),
                         [call.args[0].hermes_home.resolve() for call in bundle.call_args_list])
        self.assertIn(bad_config, reads)
        self.assertIn((good / "config.yaml").resolve(), reads, "fault injection must delegate healthy reads")
        self.assertNotEqual(by_profile["z-good"]["status"], "failed")
        self.assertTrue((good / "plugins" / "omh").is_dir(), "healthy profile was skipped")
        self.assertIn("healthy-model", read_config(good / "config.yaml"))
        self.assertIn((self.omh / "skills").resolve(),
                      _external_paths(read_config(good / "config.yaml")))


class UpdateJsonTests(_Fixture):
    """F4: automation sees the profile failure in the one final JSON result."""

    def test_update_json_reports_a_profile_refresh_failure(self) -> None:
        bot = self.hermes / "profiles" / "bot"
        bot.mkdir(parents=True)
        _write(bot / "SOUL.md", "Bot profile identity.\n")
        status, _, stderr = run_cli(
            [
                "--hermes-home",
                str(self.hermes),
                "--omh-home",
                str(self.omh),
                "setup",
                "--core",
                "--yes",
                "--no-interactive",
                "--no-menubar",
                "--json",
            ]
        )
        self.assertEqual(status, 0, stderr)
        protected = bot / "plugins" / "omh" / "local-note.txt"
        _write(protected, "Synthetic personal local addition.\n")

        status, stdout, stderr = run_cli(
            ["--hermes-home", str(self.hermes), "--omh-home", str(self.omh), "update", "--core", "--json"]
        )
        payload = json.loads(stdout)

        self.assertTrue(protected.is_file(), "the protected profile file was overwritten")
        self.assertIn("hermes_profiles", payload)
        failed = [row for row in payload["hermes_profiles"] if row.get("status") == "failed"]
        self.assertTrue(failed, payload["hermes_profiles"])
        self.assertTrue(any("local-note.txt" in str(row.get("error", "")) for row in failed))
        self.assertNotEqual(status, 0, "partial profile failure was reported as all-success")


class GenerationOwnershipTests(_Fixture):
    """F5: uninstall attributes a staged generation exactly as update does."""

    def _generation_layout(self) -> tuple[Path, Path, Path, Path]:
        store = self.root / "command-store"
        generations = store / "generations"
        generation = generations / "g1"
        generation_venv = generation / "venv"
        (generation_venv / "bin").mkdir(parents=True)
        python = generation_venv / "bin" / "python"
        _write(python, "#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        legacy = store / "venv"
        legacy.mkdir()
        current = store / "current"
        try:
            current.symlink_to(Path("generations") / "g1", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"generation directory symlink unavailable: {exc}")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (legacy / "bin").mkdir()
        entry = generation_venv / "bin" / "omh"
        _write(entry, "#!/bin/sh\nexit 0\n")
        entry.chmod(0o755)
        launcher = bin_dir / "omh"
        try:
            launcher.symlink_to(store / "current" / "venv" / "bin" / "omh")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"generation launcher symlink unavailable: {exc}")
        return store, generation, generation_venv, bin_dir

    @requires_symlinks
    def test_uninstall_removes_an_installer_owned_generation(self) -> None:
        self._setup_primary()
        store, generation, generation_venv, bin_dir = self._generation_layout()
        _write(generation_venv / "pyvenv.cfg", "home = synthetic-python\n")
        paths = OmhPaths(omh_home=self.omh, hermes_home=self.hermes)

        with mock.patch.dict(
            os.environ,
            {"OMH_VENV_DIR": str(store / "venv"), "OMH_BIN_DIR": str(bin_dir)},
            clear=False,
        ), mock.patch.object(sys, "executable", str(generation_venv / "bin" / "python")):
            result = uninstall_skill_pack(
                paths, remove_all=True, remove_command_package=True, force=True
            )

        self.assertTrue(result["command_package_removed"], result.get("command_package_kept"))
        self.assertFalse(generation.exists(), "installer-owned generation survived uninstall")
        self.assertFalse((store / "current").exists(), "current pointer survived uninstall")
        self.assertFalse((bin_dir / "omh").exists(), "launcher survived uninstall")
        self.assertFalse(self.omh.exists(), "store contents were not removed")

    @requires_symlinks
    def test_uninstall_preserves_a_foreign_command(self) -> None:
        """Negative ownership control: a foreign interpreter owns nothing here."""
        store, generation, _, bin_dir = self._generation_layout()
        paths = OmhPaths(omh_home=self.omh, hermes_home=self.hermes)

        with mock.patch.dict(
            os.environ,
            {"OMH_VENV_DIR": str(store / "venv"), "OMH_BIN_DIR": str(bin_dir)},
            clear=False,
        ), mock.patch.object(sys, "executable", "/usr/bin/true"):
            result = uninstall_skill_pack(
                paths, remove_all=True, remove_command_package=True, force=True
            )

        self.assertFalse(result["command_package_removed"])
        self.assertTrue(generation.is_dir())
        self.assertTrue((store / "current").exists())
        self.assertTrue((bin_dir / "omh").exists())


if __name__ == "__main__":
    unittest.main()
