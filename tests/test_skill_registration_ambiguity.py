"""A Hermes home that names two OMH-managed skills directories (#1857).

`omh update` migrates such a home to one managed path (pinned in
`tests/test_plugin_distribution.py`). These tests pin the readers of the same
state: the shared candidate/entry helpers, the doctor rows for the primary
home and for each affected bot profile, the update post-check that re-reads
every home after the write, and the doctor row for the pre-pointer skills
copy left on disk once nothing registers it.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _platform_support import requires_symlinks

from omh.commands import setup as _setup_module
from omh.config_adapter import external_dirs, write_config
from omh.install.skill_registration import (
    ambiguous_registration_message,
    managed_skill_dir_candidates,
    migrate_managed_registration,
    registered_managed_entries,
    registration_home_label,
)
from omh.maintenance import doctor as _doctor_module
from omh.maintenance.doctor import recommended_next_action, run_doctor
from omh.paths import OmhPaths


def _config_naming(*dirs: Path | str) -> str:
    items = "".join(f"    - {Path(item).as_posix()}\n" for item in dirs)
    return f"skills:\n  external_dirs:\n{items}"


def _managed_paths(root: Path, generation: Path) -> OmhPaths:
    """The primary home as a managed command install resolves it: skills_dir redirected."""
    return OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes", managed_skills_dir=generation)


class MigrateManagedRegistrationTests(unittest.TestCase):
    """The one text mutation setup's apply step runs, on its own."""

    def test_the_older_managed_entry_is_retired_as_the_pointer_is_registered(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, root / "gen" / "skills")
            old = paths.omh_home / "skills"
            pointer.mkdir(parents=True)
            candidates = managed_skill_dir_candidates(paths, current=pointer)

            migration = migrate_managed_registration(_config_naming(root / "mine", old), pointer, candidates)

            self.assertEqual(external_dirs(migration.text), [(root / "mine").as_posix(), pointer.as_posix()])
            self.assertEqual((migration.added, migration.retired), (True, [old.as_posix()]))
            self.assertEqual(migration.registration, "migrated")
            self.assertEqual(registered_managed_entries(migration.text, candidates), [pointer.as_posix()])

    def test_an_older_entry_is_retired_however_the_file_spells_it(self) -> None:
        # Hermes expands `~` and resolves every entry, so a spelling through
        # `~`, a trailing slash or a symlink is the same directory to it. The
        # migration matches by real path too -- the rule the ambiguity reader
        # and `external_dir_registered` use -- so no spelling is left for
        # `omh update` to report as an ambiguity it can never clear.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            pointer.mkdir(parents=True)
            paths = _managed_paths(root, root / "gen" / "skills")
            old = paths.omh_home / "skills"
            old.mkdir(parents=True)
            candidates = managed_skill_dir_candidates(paths, current=pointer)
            spellings = {"trailing slash": old.as_posix() + "/"}
            with mock.patch.dict(os.environ, {"HOME": str(root)}):
                spellings["tilde"] = "~/.omh/skills"
                self.assertEqual(Path(spellings["tilde"]).expanduser(), old)
                for label, spelled in spellings.items():
                    with self.subTest(label):
                        # Written raw: `_config_naming` would normalize the
                        # spelling away through `Path`, which is the point.
                        before = f"skills:\n  external_dirs:\n    - {spelled}\n    - {pointer.as_posix()}\n"
                        self.assertEqual(external_dirs(before), [spelled, pointer.as_posix()])
                        migration = migrate_managed_registration(before, pointer, candidates)
                        self.assertEqual(migration.retired, [spelled])
                        self.assertEqual(external_dirs(migration.text), [pointer.as_posix()])
                        self.assertEqual(registered_managed_entries(migration.text, candidates), [pointer.as_posix()])

    @requires_symlinks
    def test_an_older_entry_spelled_through_a_link_is_retired(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            pointer.mkdir(parents=True)
            paths = _managed_paths(root, root / "gen" / "skills")
            old = paths.omh_home / "skills"
            old.mkdir(parents=True)
            link = root / "store-link"
            link.symlink_to(paths.omh_home, target_is_directory=True)
            linked = (link / "skills").as_posix()

            migration = migrate_managed_registration(
                _config_naming(linked, pointer), pointer, managed_skill_dir_candidates(paths, current=pointer)
            )

            self.assertEqual((migration.retired, migration.registration), ([linked], "migrated"))
            self.assertEqual(external_dirs(migration.text), [pointer.as_posix()])

    def test_no_candidates_means_additive_only(self) -> None:
        # The caller that must stay additive (an unmanaged command) passes no
        # candidates: today's path goes in and nothing is retired, whatever
        # else the home names.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            own = paths.omh_home / "skills"

            migration = migrate_managed_registration(_config_naming(pointer), own, [])

            self.assertEqual(external_dirs(migration.text), [pointer.as_posix(), own.as_posix()])
            self.assertEqual((migration.retired, migration.registration), ([], "added"))

    def test_a_home_already_on_the_pointer_alone_is_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, root / "gen" / "skills")
            before = _config_naming(pointer)

            migration = migrate_managed_registration(before, pointer, managed_skill_dir_candidates(paths, current=pointer))

            self.assertEqual((migration.text, migration.added, migration.retired), (before, False, []))
            self.assertEqual(migration.registration, "unchanged")

    def test_a_home_naming_nothing_managed_gets_the_pointer_added(self) -> None:
        # The opt-out decision is the caller's; the mutation itself always
        # writes today's path, and says so as `added`, not `migrated`.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, root / "gen" / "skills")

            migration = migrate_managed_registration(
                _config_naming(root / "mine"), pointer, managed_skill_dir_candidates(paths, current=pointer)
            )

            self.assertEqual(external_dirs(migration.text), [(root / "mine").as_posix(), pointer.as_posix()])
            self.assertEqual((migration.added, migration.retired, migration.registration), (True, [], "added"))


class RegisteredManagedEntriesTests(unittest.TestCase):
    def test_two_managed_directories_are_both_named_in_config_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            old = paths.omh_home / "skills"
            old.mkdir(parents=True)
            pointer.mkdir(parents=True)
            entries = registered_managed_entries(
                _config_naming(old, root / "mine", pointer),
                managed_skill_dir_candidates(paths, current=pointer),
            )
            self.assertEqual(entries, [old.as_posix(), pointer.as_posix()])

    @requires_symlinks
    def test_the_pointer_and_the_generation_it_resolves_to_count_once(self) -> None:
        # Hermes reads copies that resolve to one SKILL.md as one skill, so
        # the pointer and the generation behind it are not the ambiguity.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            generation.mkdir(parents=True)
            pointer = root / "current"
            pointer.symlink_to(root / "gen", target_is_directory=True)
            paths = _managed_paths(root, generation)
            entries = registered_managed_entries(
                _config_naming(pointer / "skills", generation),
                managed_skill_dir_candidates(paths, current=pointer / "skills"),
            )
            self.assertEqual(entries, [(pointer / "skills").as_posix()])

    def test_an_entry_whose_directory_is_missing_is_not_counted(self) -> None:
        # Hermes skips an external directory that is not on disk, so nothing
        # can be ambiguous through it: a stale entry alone is not a finding.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            pointer.mkdir(parents=True)
            paths = _managed_paths(root, root / "gen" / "skills")
            gone = paths.omh_home / "skills"
            entries = registered_managed_entries(
                _config_naming(gone, pointer), managed_skill_dir_candidates(paths, current=pointer)
            )
            self.assertEqual(entries, [pointer.as_posix()])

    def test_a_foreign_directory_is_never_a_managed_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            entries = registered_managed_entries(
                _config_naming(root / "mine", root / "theirs"),
                managed_skill_dir_candidates(paths, current=None),
            )
            self.assertEqual(entries, [])

    def test_the_message_names_both_paths_and_the_consequence(self) -> None:
        # The label spells the config path as `str(Path)` does on the
        # platform (`\h\config.yaml` on Windows), so the expectation is
        # built from the same object; the entries are config text and stay
        # as written.
        config = Path("/h/config.yaml")
        message = ambiguous_registration_message(registration_home_label(config), ["/a", "/b"])
        self.assertEqual(
            message,
            f"{config} names 2 OMH-managed skills directories in skills.external_dirs (/a and /b); "
            "Hermes refuses a bare skill name that resolves to two different files, "
            "so OMH skills fail to load by name",
        )
        self.assertEqual(registration_home_label(config, profile="miku"), f"profile miku ({config})")


class DoctorAmbiguityCheckTests(unittest.TestCase):
    def _checks(self, paths: OmhPaths, pointer: Path) -> dict[str, object]:
        with mock.patch.object(_doctor_module, "managed_current_workflow_pack_dir", return_value=pointer):
            return {check.name: check for check in run_doctor(paths) if check.name.startswith("external_dir_")}

    def test_a_primary_home_naming_two_managed_directories_is_a_warning_row(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            old = paths.omh_home / "skills"
            old.mkdir(parents=True)
            pointer.mkdir(parents=True)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(old, pointer))

            check = self._checks(paths, pointer)["external_dir_ambiguity"]

            self.assertTrue(check.ok)
            self.assertEqual(check.severity, "warning")
            self.assertEqual(check.next_action, "run `omh update`")
            self.assertEqual(
                check.message,
                ambiguous_registration_message(str(paths.hermes_config_path), [old.as_posix(), pointer.as_posix()]),
            )
            self.assertIn("Hermes refuses a bare skill name that resolves to two different files", check.message)

    def test_a_primary_home_naming_one_managed_directory_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            pointer.mkdir(parents=True)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))

            check = self._checks(paths, pointer)["external_dir_ambiguity"]

            self.assertEqual((check.ok, check.severity), (True, "ok"))
            self.assertIn(pointer.as_posix(), check.message)

    def test_each_affected_bot_profile_gets_its_own_row_and_a_clean_one_gets_none(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            old = paths.omh_home / "skills"
            old.mkdir(parents=True)
            pointer.mkdir(parents=True)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))
            for name, config in (("miku", _config_naming(old, pointer)), ("clean", _config_naming(pointer))):
                profile = paths.hermes_home / "profiles" / name
                profile.mkdir(parents=True)
                write_config(profile / "config.yaml", config)

            checks = self._checks(paths, pointer)

            self.assertEqual(checks["external_dir_ambiguity"].severity, "ok")
            self.assertNotIn("external_dir_ambiguity:clean", checks)
            row = checks["external_dir_ambiguity:miku"]
            self.assertEqual((row.ok, row.severity, row.next_action), (True, "warning", "run `omh update`"))
            profile_config = paths.hermes_home / "profiles" / "miku" / "config.yaml"
            self.assertTrue(row.message.startswith(f"profile miku ({profile_config}) names 2 OMH-managed"))
            self.assertIn(old.as_posix(), row.message)
            self.assertIn(pointer.as_posix(), row.message)

    def test_the_rows_group_under_hermes_registration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            (paths.omh_home / "skills").mkdir(parents=True)
            pointer.mkdir(parents=True)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(paths.omh_home / "skills", pointer))
            with mock.patch.object(_doctor_module, "managed_current_workflow_pack_dir", return_value=pointer):
                check_dicts = [check.__dict__ for check in run_doctor(paths)]
            group = _setup_module._doctor_group("hermes_registration", check_dicts, ("external_dir",))
            names = [check["name"] for check in check_dicts if check["name"].startswith("external_dir")]
            self.assertIn("external_dir_ambiguity", names)
            self.assertIn("external_dir_unregistered_copy", names)
            self.assertEqual(group["total"], len(names))
            # A warning row never counts as failed: it carries its own next action.
            self.assertNotIn("external_dir_ambiguity", group["failed"])

    def test_the_ambiguity_next_action_is_promoted_to_doctor_s_headline(self) -> None:
        # A home that has lost every OMH skill by name gets the row's action
        # as the summary "Next" line, for the primary home and for a profile
        # row named `external_dir_ambiguity:<profile>` alike -- a blocking
        # failure still outranks it, as it does every warning.
        label = registration_home_label(Path("/h/config.yaml"))
        ambiguous = _doctor_module._ambiguity_check("external_dir_ambiguity", label, ["/a", "/b"])
        profile_row = _doctor_module._ambiguity_check(
            "external_dir_ambiguity:miku", registration_home_label(Path("/p/config.yaml"), profile="miku"), ["/a", "/b"]
        )
        clean = _doctor_module._ambiguity_check("external_dir_ambiguity", label, ["/a"])
        passing = _doctor_module.Check("external_dir", True, "ok")
        self.assertEqual(recommended_next_action([passing, ambiguous]), "run `omh update`")
        self.assertEqual(recommended_next_action([passing, clean, profile_row]), "run `omh update`")
        self.assertNotEqual(recommended_next_action([passing, clean]), "run `omh update`")
        blocking = _doctor_module.Check("external_dir", False, "missing", next_action="Run `omh setup`")
        self.assertEqual(recommended_next_action([blocking, ambiguous]), "Run `omh setup`")


class UpdatePostCheckTests(unittest.TestCase):
    def test_the_post_check_names_every_home_that_still_names_two(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            old = omh_home / "skills"
            old.mkdir(parents=True)
            pointer.mkdir(parents=True)
            write_config(hermes_home / "config.yaml", _config_naming(old, pointer))
            profile = hermes_home / "profiles" / "miku"
            profile.mkdir(parents=True)
            write_config(profile / "config.yaml", _config_naming(old, pointer))
            clean = hermes_home / "profiles" / "clean"
            clean.mkdir(parents=True)
            write_config(clean / "config.yaml", _config_naming(pointer))
            args = argparse.Namespace(omh_home=str(omh_home), hermes_home=str(hermes_home))

            with mock.patch.object(_setup_module, "managed_current_workflow_pack_dir", return_value=pointer):
                lines = _setup_module._registration_ambiguity_lines(args)

            self.assertEqual(len(lines), 2, lines)
            self.assertTrue(lines[0].startswith(f"{hermes_home.resolve() / 'config.yaml'} names 2 OMH-managed"))
            self.assertTrue(lines[1].startswith(f"profile miku ({profile.resolve() / 'config.yaml'}) names 2 OMH-managed"))
            for line in lines:
                # Entries are named the way the file spells them.
                self.assertIn(f"({old.as_posix()} and {pointer.as_posix()})", line)
                self.assertIn("so OMH skills fail to load by name", line)

    def test_the_post_check_is_quiet_for_homes_naming_one(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pointer = root / "current" / "skills"
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            write_config(hermes_home / "config.yaml", _config_naming(pointer))
            args = argparse.Namespace(omh_home=str(root / ".omh"), hermes_home=str(hermes_home))

            with mock.patch.object(_setup_module, "managed_current_workflow_pack_dir", return_value=pointer):
                self.assertEqual(_setup_module._registration_ambiguity_lines(args), [])


class DoctorUnregisteredCopyCheckTests(unittest.TestCase):
    def _check(self, paths: OmhPaths, pointer: Path | None, *, managed_root: Path | None = None):
        # The managed command root is read through `OMH_VENV_DIR` (root =
        # its parent), so each case sees its own generations directory and
        # never the machine's.
        root = managed_root if managed_root is not None else paths.omh_home.parent / "no-managed-root"
        with (
            mock.patch.dict(os.environ, {"OMH_VENV_DIR": str(root / "venv")}),
            mock.patch.object(_doctor_module, "managed_current_workflow_pack_dir", return_value=pointer),
        ):
            return next(check for check in run_doctor(paths) if check.name == "external_dir_unregistered_copy")

    def _frozen_copy(self, paths: OmhPaths) -> Path:
        copy = paths.omh_home / "skills"
        (copy / "guide").mkdir(parents=True)
        (copy / "guide" / "SKILL.md").write_text("---\nname: guide\n---\n", encoding="utf-8")
        frozen = datetime(2026, 9, 2, 10, 55, 11, tzinfo=UTC).timestamp()
        os.utime(copy, (frozen, frozen))
        return copy

    def test_an_unregistered_copy_is_named_with_its_frozen_time_as_safe_to_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))
            profile = paths.hermes_home / "profiles" / "miku"
            profile.mkdir(parents=True)
            write_config(profile / "config.yaml", _config_naming(pointer))
            copy = self._frozen_copy(paths)

            check = self._check(paths, pointer)

            self.assertEqual((check.ok, check.severity), (True, "warning"))
            self.assertEqual(
                check.message,
                f"unregistered managed skills copy at {copy} (directory mtime 2026-09-02T10:55:11Z); "
                f"neither {paths.hermes_config_path} nor its profiles register it and no retained generation "
                "links it, safe to delete",
            )
            self.assertEqual(
                check.next_action,
                f"remove {copy}; no manifest records that copy, so `omh update` never deletes it",
            )

    @requires_symlinks
    def test_a_copy_backing_a_retained_generation_is_never_called_deletable(self) -> None:
        # The lazy self-update migration links `generations/bootstrap-legacy/
        # skills` to `<omh_home>/skills`, and that generation is always
        # retained as the rollback target: the copy is a live fallback pack
        # for as long as the generations exist, and only `omh uninstall`
        # collects them. An unregistered copy in that state is reported as
        # retained, with no remove action.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))
            copy = self._frozen_copy(paths)
            managed_root = root / "share" / "omh"
            bootstrap = managed_root / "generations" / "bootstrap-legacy"
            bootstrap.mkdir(parents=True)
            (bootstrap / "skills").symlink_to(copy, target_is_directory=True)
            other = managed_root / "generations" / "20260924020459433021-edfca25300"
            (other / "skills").mkdir(parents=True)
            (managed_root / "self-update.json").write_text(
                json.dumps({"retained_generations": ["bootstrap-legacy", other.name], "previous_known_good": {"id": other.name}}),
                encoding="utf-8",
            )

            check = self._check(paths, pointer, managed_root=managed_root)

            self.assertEqual((check.ok, check.severity, check.next_action), (True, "ok", ""))
            self.assertEqual(
                check.message,
                f"unregistered managed skills copy at {copy} (directory mtime 2026-09-02T10:55:11Z); "
                f"neither {paths.hermes_config_path} nor its profiles register it, and it is retained as the "
                f"bootstrap-legacy fallback pack under {(managed_root / 'generations').resolve()}, collected only by `omh uninstall`",
            )
            self.assertNotIn("safe to delete", check.message)

    def test_a_copy_a_profile_still_registers_is_the_migration_s_job(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))
            profile = paths.hermes_home / "profiles" / "miku"
            profile.mkdir(parents=True)
            copy = self._frozen_copy(paths)
            write_config(profile / "config.yaml", _config_naming(copy, pointer))

            check = self._check(paths, pointer)

            self.assertEqual((check.ok, check.severity), (True, "ok"))
            self.assertEqual(
                check.message,
                f"pre-pointer skills copy at {copy} is still registered by profile miku ({profile / 'config.yaml'}); "
                f"`omh update` migrates that registration to {generation}",
            )

    def test_an_unmanaged_install_serves_skills_from_that_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            paths.hermes_home.mkdir()
            copy = self._frozen_copy(paths)
            write_config(paths.hermes_config_path, _config_naming(copy))

            check = self._check(paths, None)

            self.assertEqual((check.ok, check.severity), (True, "ok"))
            self.assertEqual(check.message, f"managed skills are served from {copy}")

    def test_no_copy_on_disk_is_nothing_to_report(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            generation = root / "gen" / "skills"
            pointer = root / "current" / "skills"
            paths = _managed_paths(root, generation)
            paths.hermes_home.mkdir()
            write_config(paths.hermes_config_path, _config_naming(pointer))

            check = self._check(paths, pointer)

            self.assertEqual((check.ok, check.severity), (True, "ok"))
            self.assertEqual(check.message, f"no pre-pointer skills copy at {paths.omh_home / 'skills'}")


if __name__ == "__main__":
    unittest.main()
