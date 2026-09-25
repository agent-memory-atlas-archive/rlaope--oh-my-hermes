"""A plugin directory `hermes plugins install omh` put in place is Hermes'.

Both installers replace `$HERMES_HOME/plugins/omh` wholesale, so the one that
wrote last owns it. A catalog install records itself in Hermes' own files
(`plugins/.install-metadata.json` and the `.hermes-catalog.json` sidecar); OMH
reads those, leaves the directory alone under setup and update -- `--force`
included, because overwriting would leave Hermes' pin describing files it no
longer matches -- and doctor reports it as host-managed rather than drift.
Every test runs against a temporary Hermes home.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.install.plugin_pack import PLUGIN_MANAGED_MANIFEST, host_managed_plugin

CATALOG_SHA = "0123456789abcdef0123456789abcdef01234567"
SOURCE = "https://github.com/rlaope/oh-my-hermes.git#src/plugin_bundle/omh"


class HostManagedPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.hermes_home = root / ".hermes"
        self.base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(self.hermes_home)]
        status, _, stderr = run_cli(self.base + ["setup"])
        self.assertEqual(status, 0, stderr)
        self.plugin_dir = self.hermes_home / "plugins" / "omh"
        self.marker = self.plugin_dir / "installed_by_hermes.txt"

    def _become_hermes_install(self, *, catalog: bool = True) -> None:
        """Reproduce what `hermes plugins install omh` leaves behind: the bundle
        tree with no OMH manifest, a metadata entry, and (catalog) the sidecar."""
        (self.plugin_dir / PLUGIN_MANAGED_MANIFEST).unlink()
        self.marker.write_text("hermes\n", encoding="utf-8")
        metadata = {"omh": {"pinned": catalog, "revision": CATALOG_SHA, "source": SOURCE}}
        (self.plugin_dir.parent / ".install-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        if catalog:
            sidecar = {"catalog_name": "omh", "repo": "https://github.com/rlaope/oh-my-hermes", "sha": CATALOG_SHA}
            (self.plugin_dir / ".hermes-catalog.json").write_text(json.dumps(sidecar), encoding="utf-8")

    def _assert_untouched(self) -> None:
        self.assertTrue(self.marker.is_file())
        self.assertFalse((self.plugin_dir / PLUGIN_MANAGED_MANIFEST).exists())

    def test_setup_leaves_a_catalog_install_in_place_even_with_force(self) -> None:
        self._become_hermes_install()
        for extra in ([], ["--force"]):
            status, stdout, stderr = run_cli(self.base + ["setup", *extra])
            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["steps"]["plugin"]["status"], "host_managed")
            self.assertEqual(payload["steps"]["plugin"]["host_install"]["installer"], "hermes_catalog")
            self.assertEqual(payload["steps"]["plugin"]["host_install"]["revision"], CATALOG_SHA)
            self.assertEqual(payload["operator_summary"]["plugin_mode"], "host_managed")
            self._assert_untouched()

    def test_update_leaves_a_catalog_install_in_place_and_says_who_updates_it(self) -> None:
        self._become_hermes_install()
        status, _, stderr = run_cli(self.base + ["update"])
        self.assertEqual(status, 0, stderr)
        self.assertIn("hermes plugins update omh", stderr)
        self._assert_untouched()

    def test_doctor_reports_a_catalog_install_as_host_managed_not_drift(self) -> None:
        self._become_hermes_install()
        status, stdout, _ = run_cli(self.base + ["doctor"])
        checks = {check["name"]: check for check in json.loads(stdout)["checks"]}
        for name in ("plugin_manifest", "plugin_bundle_current"):
            self.assertTrue(checks[name]["ok"], checks[name])
            self.assertIn("installed by Hermes", checks[name]["message"])
            self.assertIn("hermes plugins update omh", checks[name]["message"])
        self.assertTrue(checks["plugin_import_smoke"]["ok"], checks["plugin_import_smoke"])
        self.assertEqual(status, 0, [c for c in checks.values() if not c["ok"]])

    def test_a_git_install_recorded_only_in_hermes_metadata_is_host_managed(self) -> None:
        self._become_hermes_install(catalog=False)
        host = host_managed_plugin(self.plugin_dir)
        self.assertIsNotNone(host)
        self.assertEqual(host["installer"], "hermes_git")
        status, _, stderr = run_cli(self.base + ["update"])
        self.assertEqual(status, 0, stderr)
        self._assert_untouched()

    def test_an_omh_manifest_wins_over_a_stale_hermes_record(self) -> None:
        # OMH wrote last (Hermes' metadata outlived a removed catalog tree), so
        # OMH still owns the directory and update refreshes it.
        metadata = {"omh": {"pinned": True, "revision": CATALOG_SHA, "source": SOURCE}}
        (self.plugin_dir.parent / ".install-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.assertIsNone(host_managed_plugin(self.plugin_dir))
        stray = self.plugin_dir / "stray_from_an_older_version.py"
        stray.write_text("# stray\n", encoding="utf-8")
        status, _, stderr = run_cli(self.base + ["update"])
        self.assertEqual(status, 0, stderr)
        self.assertFalse(stray.exists())

    def test_an_unrecorded_foreign_directory_is_still_refused(self) -> None:
        # Negative control: no OMH manifest AND no Hermes record is neither
        # owner's, so the existing ownership guard still refuses it.
        shutil.rmtree(self.plugin_dir)
        self.plugin_dir.mkdir()
        self.marker.write_text("someone else\n", encoding="utf-8")
        self.assertIsNone(host_managed_plugin(self.plugin_dir))
        status, stdout, stderr = run_cli(self.base + ["setup"])
        self.assertNotEqual(status, 0)
        self.assertIn("does not look like an OMH-managed install", stdout + stderr)
        self.assertTrue(self.marker.is_file())

    def test_a_catalog_sidecar_without_a_name_is_not_a_hermes_record(self) -> None:
        # Hermes' own reader (`read_catalog_sidecar`) requires `catalog_name`.
        (self.plugin_dir / PLUGIN_MANAGED_MANIFEST).unlink()
        (self.plugin_dir / ".hermes-catalog.json").write_text(json.dumps({"sha": CATALOG_SHA}), encoding="utf-8")
        self.assertIsNone(host_managed_plugin(self.plugin_dir))


if __name__ == "__main__":
    unittest.main()
