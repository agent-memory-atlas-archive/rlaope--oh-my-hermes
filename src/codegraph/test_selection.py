"""Reverse-import test selection over a local codegraph.

`omh codegraph build` records which Python file imports which
(`imports_internal` edges). Walking those edges backwards from a changed file
gives every file that depends on it, and the test modules among them are the
smallest set a static reader can name as "the tests that exercise this change".

Everything here is pure traversal over the codegraph payload: nothing is
imported, executed, or spawned. The result is a starting point for the
smallest test that proves a claim. It is not a substitute for the full suite,
and its blind spots ship inside the payload rather than being left to the
reader to remember.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from .schema import CLAIM_BOUNDARY


CODEGRAPH_TEST_SELECTION_SCHEMA_VERSION = "codegraph_test_selection/v1"

TEST_SELECTION_CLAIM_BOUNDARY = (
    "A selected subset is a starting point for the smallest test that proves a claim; "
    "it is never a substitute for the full suite before claiming done. " + CLAIM_BOUNDARY
)

# unittest's default discovery pattern is `test*.py`, so a reached file counts
# as a test when discovery would collect it. The directory component keeps a
# source module that happens to start with `test` (this file, for one) out of
# the selection.
TEST_MODULE_PREFIX = "test"
TEST_DIRECTORY_NAMES = frozenset({"tests", "test"})
TEST_MODULE_RULE = (
    "a scanned Python file under a directory named tests or test whose basename matches "
    "unittest's default discovery pattern test*.py"
)

# Fixed by design: the same block ships with every payload, so a reader who
# sees an empty selection also sees what this traversal cannot see.
TEST_SELECTION_BLIND_SPOTS: tuple[str, ...] = (
    "dynamic imports: a module reached through importlib, __import__, spec_from_file_location, "
    "or runpy at run time is not an import edge",
    "fixtures loaded by path: a file a test reads with open(), Path.read_text, json.load, or through "
    "a subprocess is not an import edge",
    "generated artifacts gated by byte comparison: a file whose gate diffs it against its producer "
    "(a docs --check target) is reached by regeneration, not by an import",
    "extra import roots: a module importable only through a PYTHONPATH entry other than the repository "
    "root, such as a helper under tests/ imported by its bare name, resolves to no edge",
    "spawned commands: a test that drives a console script or a `python -m` module never imports the "
    "code it exercises",
    "non-Python changes: a changed file that is not a Python module (config, YAML, JSON, Markdown) has "
    "no importer to trace",
)

CHANGED_PATH_STATUSES = ("scanned", "not_python", "directory", "missing", "not_scanned")
_STATUS_REASONS = {
    "not_python": "not a Python module; no import edge can reach it",
    "directory": "a directory; pass the changed files under it",
    "missing": (
        "no such file under the repository root; the graph is built from the current tree, "
        "so a deleted module's former importers are not edges"
    ),
    "not_scanned": "a Python file the scanner did not index (excluded directory or symlink)",
}
_UNREFERENCED_REASON = "nothing in the graph imports this module"
_CHANGED_TEST_REASON = "a changed test module is selected itself at distance 0"

MAX_TEXT_REACHED_SOURCE_FILES = 12


def select_tests_for_changes(graph: dict[str, Any], changed: list[str]) -> dict[str, Any]:
    repo_root = Path(str(graph["repo_root"]))
    scanned_paths = {str(record["path"]) for record in graph.get("files", []) if isinstance(record, dict)}
    reverse = _reverse_import_index(graph)
    changed_paths = _normalized_changed_paths(repo_root, changed)

    reached: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for rel_path in changed_paths:
        status = _changed_path_status(repo_root, rel_path, scanned_paths)
        entry: dict[str, Any] = {
            "path": rel_path,
            "status": status,
            "is_test_module": False,
            "direct_importer_count": 0,
            "reached_file_count": 0,
            "selected_test_count": 0,
            "reason": _STATUS_REASONS.get(status, ""),
        }
        if status != "scanned":
            entries.append(entry)
            continue
        is_test = _is_test_module(rel_path)
        distances = _reverse_closure(reverse, rel_path)
        entry["is_test_module"] = is_test
        entry["direct_importer_count"] = len(reverse.get(rel_path, ()))
        entry["reached_file_count"] = len(distances)
        if is_test:
            distances = {rel_path: 0, **distances}
            entry["reason"] = _CHANGED_TEST_REASON
        elif not entry["direct_importer_count"]:
            entry["reason"] = _UNREFERENCED_REASON
        entry["selected_test_count"] = sum(1 for path in distances if _is_test_module(path))
        for path, distance in distances.items():
            record = reached.setdefault(path, {"distance": distance, "changed_paths": set()})
            record["distance"] = min(int(record["distance"]), distance)
            record["changed_paths"].add(rel_path)
        entries.append(entry)

    selected_tests = sorted(
        (
            {
                "path": path,
                "distance": int(record["distance"]),
                "changed_paths": sorted(record["changed_paths"]),
            }
            for path, record in reached.items()
            if _is_test_module(path)
        ),
        key=lambda item: str(item["path"]),
    )
    reached_source_files = sorted(path for path in reached if not _is_test_module(path))
    unreferenced_paths = sorted(
        entry["path"]
        for entry in entries
        if entry["status"] == "scanned" and not entry["direct_importer_count"] and not entry["is_test_module"]
    )
    unscanned_paths = sorted(entry["path"] for entry in entries if entry["status"] != "scanned")
    return {
        "schema_version": CODEGRAPH_TEST_SELECTION_SCHEMA_VERSION,
        "repo_root": str(repo_root),
        "generated_at": graph["generated_at"],
        "changed_paths": entries,
        "selected_tests": selected_tests,
        "selected_test_paths": [str(item["path"]) for item in selected_tests],
        "reached_source_files": reached_source_files,
        "unreferenced_paths": unreferenced_paths,
        "unscanned_paths": unscanned_paths,
        "test_module_rule": TEST_MODULE_RULE,
        "stats": {
            "changed_path_count": len(entries),
            "scanned_changed_path_count": sum(1 for entry in entries if entry["status"] == "scanned"),
            "selected_test_count": len(selected_tests),
            "reached_source_file_count": len(reached_source_files),
            "unreferenced_path_count": len(unreferenced_paths),
            "unscanned_path_count": len(unscanned_paths),
        },
        "blind_spots": list(TEST_SELECTION_BLIND_SPOTS),
        "claim_boundary": TEST_SELECTION_CLAIM_BOUNDARY,
    }


def render_test_selection_text(payload: dict[str, Any]) -> str:
    stats = payload["stats"]
    lines = [
        "OMH codegraph test selection",
        f"Repo: {payload['repo_root']}",
        f"Changed paths ({stats['changed_path_count']})",
    ]
    for entry in payload["changed_paths"]:
        lines.append(f"  - {entry['path']}: {_changed_entry_summary(entry)}")
    selected = payload["selected_tests"]
    lines.append(f"Selected tests ({len(selected)})")
    if selected:
        for item in selected:
            lines.append(f"  - {item['path']} (distance {item['distance']})")
    else:
        lines.append("  - none")
    reached = payload["reached_source_files"]
    if reached:
        lines.append(f"Reached source files ({len(reached)})")
        for path in reached[:MAX_TEXT_REACHED_SOURCE_FILES]:
            lines.append(f"  - {path}")
        if len(reached) > MAX_TEXT_REACHED_SOURCE_FILES:
            lines.append(f"  - ... {len(reached) - MAX_TEXT_REACHED_SOURCE_FILES} more")
    lines.append("Blind spots")
    for spot in payload["blind_spots"]:
        lines.append(f"  - {spot}")
    lines.extend(
        [
            "Boundary",
            f"  {payload['claim_boundary']}",
            "For machine-readable output, rerun with `--json`.",
        ]
    )
    return "\n".join(lines)


def _changed_entry_summary(entry: dict[str, Any]) -> str:
    if entry["status"] != "scanned":
        return f"{entry['reason']}; no test selected"
    counts = (
        f"direct importers {entry['direct_importer_count']}, files reached {entry['reached_file_count']}, "
        f"tests selected {entry['selected_test_count']}"
    )
    if entry["reason"]:
        return f"{entry['reason']}; {counts}"
    return counts


def _reverse_import_index(graph: dict[str, Any]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("kind") != "imports_internal":
            continue
        reverse.setdefault(str(edge["to"]), set()).add(str(edge["from"]))
    return reverse


def _reverse_closure(reverse: dict[str, set[str]], start: str) -> dict[str, int]:
    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        path, distance = queue.popleft()
        for importer in sorted(reverse.get(path, ())):
            if importer == start or importer in distances:
                continue
            distances[importer] = distance + 1
            queue.append((importer, distance + 1))
    return distances


def _is_test_module(rel_path: str) -> bool:
    path = Path(rel_path)
    if not any(part in TEST_DIRECTORY_NAMES for part in path.parts[:-1]):
        return False
    return path.name.startswith(TEST_MODULE_PREFIX) and path.name.endswith(".py")


def _normalized_changed_paths(repo_root: Path, changed: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in changed:
        text = str(raw).strip()
        if not text:
            raise ValueError("changed path must not be empty")
        candidate = Path(text).expanduser()
        target = candidate if candidate.is_absolute() else repo_root / candidate
        try:
            rel = target.resolve().relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"changed path is outside the repository root: {text}") from exc
        if not rel.parts:
            raise ValueError(f"changed path must name a file, not the repository root: {text}")
        normalized.append(rel.as_posix())
    return sorted(dict.fromkeys(normalized))


def _changed_path_status(repo_root: Path, rel_path: str, scanned_paths: set[str]) -> str:
    if rel_path in scanned_paths:
        return "scanned"
    target = repo_root / rel_path
    if target.is_dir():
        return "directory"
    if not rel_path.endswith(".py"):
        return "not_python"
    if not target.exists():
        return "missing"
    return "not_scanned"
