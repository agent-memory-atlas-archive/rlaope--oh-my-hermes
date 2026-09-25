from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from omh.codegraph import (
    TEST_SELECTION_BLIND_SPOTS,
    build_codegraph,
    render_test_selection_text,
    select_tests_for_changes,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sample_repo(root: Path) -> None:
    # The src layout this repository uses: the package name comes from
    # pyproject's package-dir mapping, so `omh.pkg.leaf` must resolve to
    # `src/pkg/leaf.py` for any test edge to exist at all.
    _write(
        root / "pyproject.toml",
        """
[tool.setuptools.package-dir]
"omh.pkg" = "src/pkg"
"omh.empty" = "src/empty"
""".lstrip(),
    )
    _write(root / "src" / "pkg" / "__init__.py", "SETTING = 1\n")
    _write(root / "src" / "empty" / "__init__.py", "")
    # Importing this module writes a marker; the selection must never do so.
    _write(
        root / "src" / "pkg" / "leaf.py",
        """
from pathlib import Path

Path(__file__).with_name("IMPORTED").write_text("imported", encoding="utf-8")


def leaf():
    return 1
""".lstrip(),
    )
    _write(
        root / "src" / "pkg" / "importer.py",
        """
from .leaf import leaf


def use():
    return leaf()
""".lstrip(),
    )
    _write(root / "src" / "pkg" / "orphan.py", "def orphan():\n    return 0\n")
    # A source module whose name starts with `test`: reached by the closure,
    # never selected, because it lives outside a tests directory.
    _write(
        root / "src" / "pkg" / "test_support.py",
        """
from .leaf import leaf


def support():
    return leaf()
""".lstrip(),
    )
    _write(
        root / "tests" / "test_leaf.py",
        """
from omh.pkg.leaf import leaf


def test_leaf():
    assert leaf() == 1
""".lstrip(),
    )
    _write(
        root / "tests" / "test_importer.py",
        """
from omh.pkg.importer import use


def test_use():
    assert use() == 1
""".lstrip(),
    )
    # Loads the leaf by filesystem path: a real dependency the import graph
    # cannot see. The selection must not find it and must say why.
    _write(
        root / "tests" / "test_by_path.py",
        """
import importlib.util
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pkg" / "leaf.py"


def test_leaf_by_path():
    spec = importlib.util.spec_from_file_location("leaf_by_path", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.leaf() == 1
""".lstrip(),
    )
    _write(root / "docs" / "notes.md", "# notes\n")


def _graph(root: Path) -> dict:
    return build_codegraph(root, generated_at="2026-01-01T00:00:00Z")


def _symlink(link: Path, target: str, test: unittest.TestCase) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        test.skipTest(f"symlink creation unavailable: {exc}")


class TestSelectionTests(unittest.TestCase):
    def test_leaf_change_selects_direct_and_transitive_tests_with_distances(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/leaf.py"])

            self.assertFalse((root / "src" / "pkg" / "IMPORTED").exists(), "selection imported repo code")

        self.assertEqual(payload["schema_version"], "codegraph_test_selection/v1")
        self.assertEqual(payload["generated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(
            payload["selected_tests"],
            [
                {"path": "tests/test_importer.py", "distance": 2, "changed_paths": ["src/pkg/leaf.py"]},
                {"path": "tests/test_leaf.py", "distance": 1, "changed_paths": ["src/pkg/leaf.py"]},
            ],
        )
        self.assertEqual(payload["selected_test_paths"], ["tests/test_importer.py", "tests/test_leaf.py"])
        self.assertEqual(payload["reached_source_files"], ["src/pkg/importer.py", "src/pkg/test_support.py"])
        self.assertEqual(payload["unreferenced_paths"], [])
        self.assertEqual(payload["unscanned_paths"], [])
        self.assertEqual(payload["scanner_warnings"], [])
        [entry] = payload["changed_paths"]
        self.assertEqual(entry["path"], "src/pkg/leaf.py")
        self.assertEqual(entry["resolved_path"], "src/pkg/leaf.py")
        self.assertEqual(entry["status"], "scanned")
        self.assertFalse(entry["is_test_module"])
        self.assertEqual(entry["direct_importer_count"], 3)
        self.assertEqual(entry["reached_file_count"], 4)
        self.assertEqual(entry["selected_test_count"], 2)
        self.assertEqual(entry["reason"], "")
        self.assertEqual(payload["stats"]["selected_test_count"], 2)
        self.assertEqual(payload["stats"]["reached_source_file_count"], 2)
        self.assertEqual(payload["stats"]["scanner_warning_count"], 0)
        self.assertIn("under a directory named tests or test", payload["test_module_rule"])
        self.assertIn("run a selected path by file", payload["test_module_rule"])
        self.assertIn("never a substitute for the full suite", payload["claim_boundary"])
        self.assertIn("Static local analysis is not execution/review/CI/merge evidence", payload["claim_boundary"])

    def test_module_nothing_imports_is_reported_plainly_not_expanded_to_the_suite(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/orphan.py"])

        self.assertEqual(payload["selected_tests"], [])
        self.assertEqual(payload["selected_test_paths"], [])
        self.assertEqual(payload["reached_source_files"], [])
        self.assertEqual(payload["unreferenced_paths"], ["src/pkg/orphan.py"])
        [entry] = payload["changed_paths"]
        self.assertEqual(entry["status"], "scanned")
        self.assertEqual(entry["direct_importer_count"], 0)
        self.assertEqual(entry["selected_test_count"], 0)
        self.assertEqual(entry["reason"], "nothing in the graph imports this module")
        self.assertEqual(payload["stats"]["selected_test_count"], 0)
        self.assertEqual(payload["stats"]["unreferenced_path_count"], 1)

        text = render_test_selection_text(payload)
        self.assertIn("src/pkg/orphan.py: nothing in the graph imports this module", text)
        self.assertIn("Selected tests (0)\n  - none", text)
        self.assertNotIn("tests/test_leaf.py", text)
        self.assertNotIn("tests/test_importer.py", text)

    def test_package_init_is_credited_with_the_importers_of_its_modules(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            graph = _graph(root)

            # No test imports omh.pkg itself; every test that imports a module
            # under src/pkg/ executes src/pkg/__init__.py all the same.
            self.assertEqual(
                [edge for edge in graph["edges"] if edge["kind"] == "imports_internal" and edge["to"] == "src/pkg/__init__.py"],
                [],
            )
            payload = select_tests_for_changes(graph, ["src/pkg/__init__.py"])
            empty = select_tests_for_changes(graph, ["src/empty/__init__.py"])

        self.assertEqual(
            payload["selected_tests"],
            [
                {"path": "tests/test_importer.py", "distance": 1, "changed_paths": ["src/pkg/__init__.py"]},
                {"path": "tests/test_leaf.py", "distance": 1, "changed_paths": ["src/pkg/__init__.py"]},
            ],
        )
        self.assertEqual(payload["reached_source_files"], ["src/pkg/importer.py", "src/pkg/test_support.py"])
        self.assertEqual(payload["unreferenced_paths"], [])
        [entry] = payload["changed_paths"]
        self.assertEqual(entry["direct_importer_count"], 4)
        self.assertEqual(entry["reached_file_count"], 4)
        self.assertEqual(entry["selected_test_count"], 2)
        self.assertEqual(
            entry["reason"],
            "a package __init__.py, credited with the importers of the 4 scanned files under src/pkg/",
        )

        # An empty package is still never given the "nothing imports" verdict.
        self.assertEqual(empty["selected_tests"], [])
        self.assertEqual(empty["unreferenced_paths"], [])
        [empty_entry] = empty["changed_paths"]
        self.assertEqual(empty_entry["direct_importer_count"], 0)
        self.assertEqual(
            empty_entry["reason"],
            "a package __init__.py, credited with the importers of the 0 scanned files under src/empty/; "
            "none has a recorded importer",
        )
        self.assertNotIn("nothing in the graph imports", render_test_selection_text(empty))

    def test_path_loaded_test_is_a_blind_spot_not_a_selection(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/leaf.py"])

        self.assertNotIn("tests/test_by_path.py", payload["selected_test_paths"])
        self.assertEqual(payload["blind_spots"], list(TEST_SELECTION_BLIND_SPOTS))
        self.assertEqual(len(payload["blind_spots"]), 7)
        joined = "\n".join(payload["blind_spots"])
        self.assertIn("spec_from_file_location", joined)
        self.assertIn("fixtures loaded by path", joined)
        self.assertIn("byte comparison", joined)
        self.assertIn("PYTHONPATH=tests", joined)
        self.assertIn("__path__", joined)
        # The block is fixed: an empty selection carries exactly the same list.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            empty = select_tests_for_changes(_graph(root), ["src/pkg/orphan.py"])
        self.assertEqual(empty["blind_spots"], payload["blind_spots"])
        self.assertEqual(empty["claim_boundary"], payload["claim_boundary"])

    def test_non_python_directory_missing_and_test_module_paths_are_classified(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            (root / ".venv").mkdir()
            _write(root / ".venv" / "excluded.py", "def excluded():\n    return 0\n")

            payload = select_tests_for_changes(
                _graph(root),
                ["docs/notes.md", "src/pkg/gone.py", "tests/test_leaf.py", ".venv/excluded.py", "src/pkg"],
            )

        by_path = {entry["path"]: entry for entry in payload["changed_paths"]}
        self.assertEqual(
            sorted(by_path),
            [".venv/excluded.py", "docs/notes.md", "src/pkg", "src/pkg/gone.py", "tests/test_leaf.py"],
        )
        self.assertEqual(by_path["docs/notes.md"]["status"], "not_python")
        self.assertEqual(by_path["src/pkg"]["status"], "directory")
        self.assertEqual(by_path["src/pkg/gone.py"]["status"], "missing")
        self.assertIn("former importers are not edges", by_path["src/pkg/gone.py"]["reason"])
        self.assertEqual(by_path[".venv/excluded.py"]["status"], "not_scanned")
        self.assertIn("excluded directory", by_path[".venv/excluded.py"]["reason"])
        self.assertNotIn("symlink", by_path[".venv/excluded.py"]["reason"])
        self.assertEqual(by_path["tests/test_leaf.py"]["status"], "scanned")
        self.assertTrue(by_path["tests/test_leaf.py"]["is_test_module"])
        self.assertEqual(by_path["tests/test_leaf.py"]["selected_test_count"], 1)
        self.assertEqual(
            payload["unscanned_paths"],
            [".venv/excluded.py", "docs/notes.md", "src/pkg", "src/pkg/gone.py"],
        )
        self.assertEqual(
            payload["selected_tests"],
            [{"path": "tests/test_leaf.py", "distance": 0, "changed_paths": ["tests/test_leaf.py"]}],
        )
        self.assertEqual(payload["stats"]["scanned_changed_path_count"], 1)
        self.assertEqual(payload["stats"]["unscanned_path_count"], 4)

        text = render_test_selection_text(payload)
        self.assertIn("docs/notes.md: not a Python module; no import edge can reach it; no test selected", text)
        self.assertIn("tests/test_leaf.py (distance 0)", text)

    def test_changed_paths_keep_the_callers_spelling_with_the_resolved_path_beside_it(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            graph = _graph(root)
            absolute = str((root / "src" / "pkg" / "leaf.py").resolve())

            payload = select_tests_for_changes(
                graph, [absolute, "./src/pkg/leaf.py", "src/pkg/../pkg/leaf.py", "src/pkg/leaf.py", "src/pkg/leaf.py"]
            )
            with self.assertRaises(ValueError) as outside:
                select_tests_for_changes(graph, ["../outside.py"])
            with self.assertRaises(ValueError) as empty:
                select_tests_for_changes(graph, ["  "])
            with self.assertRaises(ValueError) as repo_root:
                select_tests_for_changes(graph, ["."])

        spellings = sorted([absolute, "./src/pkg/leaf.py", "src/pkg/../pkg/leaf.py", "src/pkg/leaf.py"])
        self.assertEqual([entry["path"] for entry in payload["changed_paths"]], spellings)
        self.assertEqual({entry["resolved_path"] for entry in payload["changed_paths"]}, {"src/pkg/leaf.py"})
        self.assertEqual({entry["selected_test_count"] for entry in payload["changed_paths"]}, {2})
        self.assertEqual(payload["stats"]["changed_path_count"], 4)
        self.assertEqual(payload["selected_tests"][0]["changed_paths"], spellings)
        self.assertIn("outside the repository root", str(outside.exception))
        self.assertIn("must not be empty", str(empty.exception))
        self.assertIn("not the repository root", str(repo_root.exception))

    def test_symlink_input_keeps_its_spelling_and_names_the_target(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            _symlink(root / "src" / "pkg" / "link.py", "leaf.py", self)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/link.py"])

        [entry] = payload["changed_paths"]
        self.assertEqual(entry["path"], "src/pkg/link.py")
        self.assertEqual(entry["resolved_path"], "src/pkg/leaf.py")
        self.assertEqual(entry["status"], "scanned")
        self.assertEqual(entry["reason"], "resolved through a symlink to src/pkg/leaf.py")
        self.assertEqual(entry["selected_test_count"], 2)
        self.assertEqual(payload["selected_tests"][0]["changed_paths"], ["src/pkg/link.py"])
        self.assertIn("src/pkg/link.py: resolved through a symlink to src/pkg/leaf.py; ", render_test_selection_text(payload))
        self.assertTrue(any("skipped_symlink: src/pkg/link.py" in warning for warning in payload["scanner_warnings"]))

    def test_symlink_loop_is_classified_missing_without_a_traceback(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            _symlink(root / "src" / "pkg" / "loop.py", "loop.py", self)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/loop.py"])
            status, stdout, stderr = run_cli(
                ["codegraph", "tests", "--repo", str(root), "--changed", "src/pkg/loop.py"],
                output_json=False,
            )

        [entry] = payload["changed_paths"]
        self.assertEqual(entry["path"], "src/pkg/loop.py")
        self.assertEqual(entry["resolved_path"], "src/pkg/loop.py")
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(payload["unscanned_paths"], ["src/pkg/loop.py"])
        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertNotIn("Traceback", stdout)
        self.assertIn("src/pkg/loop.py: ", stdout)

    def test_case_variant_and_backslash_spellings_map_to_the_scanned_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            case_insensitive = (root / "SRC" / "pkg" / "leaf.py").is_file()

            payload = select_tests_for_changes(_graph(root), ["src\\pkg\\leaf.py", "SRC/pkg/leaf.py"])

        by_path = {entry["path"]: entry for entry in payload["changed_paths"]}
        backslash = by_path["src\\pkg\\leaf.py"]
        self.assertEqual(backslash["status"], "scanned")
        self.assertEqual(backslash["resolved_path"], "src/pkg/leaf.py")
        self.assertEqual(backslash["selected_test_count"], 2)
        variant = by_path["SRC/pkg/leaf.py"]
        if case_insensitive:
            self.assertEqual(variant["status"], "scanned")
            self.assertEqual(variant["resolved_path"], "src/pkg/leaf.py")
            self.assertEqual(variant["selected_test_count"], 2)
            self.assertIn("spelled differently from the scanned path src/pkg/leaf.py", variant["reason"])
            self.assertEqual(payload["selected_tests"][0]["changed_paths"], ["SRC/pkg/leaf.py", "src\\pkg\\leaf.py"])
        else:
            self.assertEqual(variant["status"], "missing")
            self.assertEqual(payload["selected_tests"][0]["changed_paths"], ["src\\pkg\\leaf.py"])

    def test_unparseable_test_module_surfaces_as_a_scanner_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            _write(root / "tests" / "test_broken.py", "from omh.pkg.leaf import leaf\n\n\ndef test_x(:\n    pass\n")

            payload = select_tests_for_changes(_graph(root), ["src/pkg/leaf.py"])

        self.assertNotIn("tests/test_broken.py", payload["selected_test_paths"])
        self.assertEqual(len(payload["scanner_warnings"]), 1)
        self.assertTrue(payload["scanner_warnings"][0].startswith("parse_error: tests/test_broken.py: SyntaxError"))
        self.assertEqual(payload["stats"]["scanner_warning_count"], 1)
        text = render_test_selection_text(payload)
        self.assertIn("Scanner warnings (1)\n  - parse_error: tests/test_broken.py", text)

    def test_two_changed_paths_merge_by_minimum_distance(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/leaf.py", "src/pkg/importer.py"])

        by_path = {item["path"]: item for item in payload["selected_tests"]}
        self.assertEqual(by_path["tests/test_importer.py"]["distance"], 1)
        self.assertEqual(by_path["tests/test_importer.py"]["changed_paths"], ["src/pkg/importer.py", "src/pkg/leaf.py"])
        self.assertEqual(by_path["tests/test_leaf.py"]["distance"], 1)
        self.assertEqual(by_path["tests/test_leaf.py"]["changed_paths"], ["src/pkg/leaf.py"])
        self.assertEqual(payload["reached_source_files"], ["src/pkg/importer.py", "src/pkg/test_support.py"])

    def test_source_module_named_like_a_test_is_reached_but_not_selected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            payload = select_tests_for_changes(_graph(root), ["src/pkg/leaf.py", "src/pkg/test_support.py"])

        self.assertNotIn("src/pkg/test_support.py", payload["selected_test_paths"])
        self.assertIn("src/pkg/test_support.py", payload["reached_source_files"])
        by_path = {entry["path"]: entry for entry in payload["changed_paths"]}
        self.assertFalse(by_path["src/pkg/test_support.py"]["is_test_module"])
        self.assertEqual(by_path["src/pkg/test_support.py"]["reason"], "nothing in the graph imports this module")
        self.assertEqual(payload["unreferenced_paths"], ["src/pkg/test_support.py"])


class TestSelectionCliTests(unittest.TestCase):
    def test_json_payload_and_text_output(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            status, stdout, stderr = run_cli(
                ["codegraph", "tests", "--repo", str(root), "--changed", "src/pkg/leaf.py", "--json"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)

            text_status, text, text_stderr = run_cli(
                ["codegraph", "tests", "--repo", str(root), "--changed", "src/pkg/leaf.py", "src/pkg/orphan.py"],
                output_json=False,
            )
            self.assertFalse((root / "src" / "pkg" / "IMPORTED").exists(), "the command imported repo code")

        self.assertEqual(payload["schema_version"], "codegraph_test_selection/v1")
        self.assertEqual(payload["selected_test_paths"], ["tests/test_importer.py", "tests/test_leaf.py"])
        self.assertEqual(payload["blind_spots"], list(TEST_SELECTION_BLIND_SPOTS))
        self.assertIn("never a substitute for the full suite", payload["claim_boundary"])

        self.assertEqual(text_status, 0, text_stderr)
        self.assertEqual(text_stderr, "")
        self.assertIn("OMH codegraph test selection", text)
        self.assertIn("Changed paths (2)", text)
        self.assertIn(
            "src/pkg/leaf.py: direct importers 3, files reached 4, tests selected 2",
            text,
        )
        self.assertIn("src/pkg/orphan.py: nothing in the graph imports this module", text)
        self.assertIn("Selected tests (2)", text)
        self.assertIn("tests/test_leaf.py (distance 1)", text)
        self.assertIn("tests/test_importer.py (distance 2)", text)
        self.assertIn("Reached source files (2)\n  - src/pkg/importer.py\n  - src/pkg/test_support.py", text)
        self.assertNotIn("Scanner warnings", text)
        self.assertIn("Blind spots", text)
        self.assertIn("spec_from_file_location", text)
        self.assertIn("never a substitute for the full suite", text)
        self.assertIn("For machine-readable output, rerun with `--json`.", text)

    def test_missing_and_non_python_paths_still_exit_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)

            status, stdout, stderr = run_cli(
                ["codegraph", "tests", "--repo", str(root), "--changed", "src/pkg/gone.py", "docs/notes.md", "--json"],
                output_json=False,
            )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual([entry["status"] for entry in payload["changed_paths"]], ["not_python", "missing"])
        self.assertEqual(payload["selected_tests"], [])
        self.assertEqual(payload["unscanned_paths"], ["docs/notes.md", "src/pkg/gone.py"])

    def test_path_outside_the_repository_is_a_cli_error_not_a_traceback(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as elsewhere:
            root = Path(tmp)
            _sample_repo(root)
            outside = Path(elsewhere) / "outside.py"
            outside.write_text("def outside():\n    return 0\n", encoding="utf-8")

            status, stdout, stderr = run_cli(
                ["codegraph", "tests", "--repo", str(root), "--changed", str(outside)],
                output_json=False,
            )

        self.assertNotEqual(status, 0)
        self.assertEqual(stdout, "")
        self.assertIn("omh:", stderr)
        self.assertIn("outside the repository root", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
