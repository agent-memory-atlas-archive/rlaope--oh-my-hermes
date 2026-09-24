"""The Hermes Desktop half of the bundle: backend route, renderer file, doctor.

Hermes Desktop loads `desktop/plugin.js` uncompiled through its disk-plugin
door and imports `dashboard/plugin_api.py` by path inside the gateway process,
so neither file is reached by the bundle's own package machinery. These tests
drive both the way the host does: the backend's pure functions without FastAPI
(the host's dependency, not OMH's), and the renderer file under node with the
SDK and React shims replaced by recording fakes, the way
`tests/test_tui_widget_pack.py` drives the TUI widget.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _standalone_bundle import bundle_dir
from omh.install.plugin_pack import install_plugin_bundle
from omh.maintenance.doctor import DESKTOP_HALF_FILES, doctor_ok, run_doctor
from omh.paths import resolve_paths
from omh.plugin_bundle.omh.dashboard import plugin_api

NODE = shutil.which("node")

BUNDLE = bundle_dir()
PLUGIN_JS = BUNDLE / "desktop" / "plugin.js"
MANIFEST = BUNDLE / "dashboard" / "manifest.json"

# The specifiers Hermes' runtime loader resolves for a disk plugin
# (`apps/desktop/src/sdk/runtime.ts::sdkImportMap`), of which this plugin
# uses exactly two; anything else fails the loader's import fence.
ALLOWED_IMPORTS = frozenset({"@hermes/plugin-sdk", "react/jsx-runtime"})

# The loader's own specifier pattern (`runtime-loader.ts::importSpecifierRe`),
# so the fence is measured with the host's reading of the file, not another.
IMPORT_SPECIFIER = re.compile(r"""(from\s*|import\s*\(\s*|import\s+)(['"])([^'"]+)\2""")

LOADER_NOT_OBSERVED = {
    "observed": False,
    "ok": False,
    "reason": "hermes_not_installed",
    "registered_tools": [],
    "registered_hooks": [],
}

# Stands in for `@hermes/plugin-sdk`: the seven names the plugin imports,
# each answering from `globalThis.__omh` so a scenario sets the state and a
# render reads it back, and `useQuery` recording the options it was handed.
SDK_SHIM = r"""
export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const PANES_AREA = 'panes'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const Tip = ({ children }) => children
export const host = {
  state: {
    gateway: { get: () => globalThis.__omh.gateway },
    focusedStoredSessionId: { get: () => globalThis.__omh.stored }
  }
}
export const useValue = atom => atom.get()
export function useQuery(options) {
  globalThis.__omh.lastQuery = options
  return globalThis.__omh.query
}
"""

# Stands in for `react/jsx-runtime`: function components expand in place so
# their text is in the tree, as on screen; host elements become plain nodes.
JSX_SHIM = r"""
const expand = (type, props, key) => (typeof type === 'function' ? type(props || {}) : { type, props: props || {}, key })
export const jsx = expand
export const jsxs = expand
"""

# Registers the rewritten plugin against a recording context, then renders
# every contribution under each scenario and reports the text it produced,
# the query options in force, and the REST path the query function asked for.
HARNESS = r"""
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const [pluginPath, scenariosPath] = process.argv.slice(2)
globalThis.__omh = { gateway: 'open', stored: null, query: { data: undefined, error: null }, lastQuery: null }
const mod = await import(pathToFileURL(pluginPath).href)
const plugin = mod.default
const contributions = []
const restCalls = []
const ctx = {
  source: 'plugin:omh',
  rest: path => { restCalls.push(path); return Promise.resolve({}) },
  register: c => { contributions.push(c); return () => {} },
  registerMany: cs => { cs.forEach(c => contributions.push(c)); return () => {} },
  onDispose: () => {},
  onEvent: () => () => {},
  socket: () => () => {}
}
plugin.register(ctx)
const text = node =>
  node === null || node === undefined || node === false
    ? []
    : Array.isArray(node)
      ? node.flatMap(text)
      : typeof node === 'string'
        ? [node]
        : typeof node === 'object'
          ? text(node.props && node.props.children)
          : [String(node)]
const scenarios = JSON.parse(readFileSync(scenariosPath, 'utf8'))
const renders = {}
for (const [name, scenario] of Object.entries(scenarios)) {
  globalThis.__omh.gateway = scenario.gateway
  globalThis.__omh.stored = scenario.stored
  globalThis.__omh.query = {
    data: scenario.data,
    error: scenario.errorMessage ? new Error(scenario.errorMessage) : null
  }
  globalThis.__omh.lastQuery = null
  restCalls.length = 0
  const byId = {}
  for (const c of contributions) {
    byId[c.id] = text(c.render())
  }
  const query = globalThis.__omh.lastQuery
  await query.queryFn()
  renders[name] = {
    byId,
    refetchInterval: query.refetchInterval,
    enabled: query.enabled,
    queryKey: query.queryKey,
    restCalls: [...restCalls]
  }
}
const report = {
  plugin: { id: plugin.id, name: plugin.name, description: plugin.description, defaultEnabled: plugin.defaultEnabled },
  contributions: contributions.map(c => ({ id: c.id, area: c.area, title: c.title, order: c.order, data: c.data })),
  renders
}
process.stdout.write(`${JSON.stringify(report)}\n`, () => process.exit(0))
"""

SCENARIOS = {
    "open_with_payload": {
        "gateway": "open",
        "stored": "20260924_101010_abc123",
        "data": {
            "schema_version": "omh_hud/v1",
            "display": {
                "line": "[omh] v2.0.5 | plugin:ready | target:single",
                "widget_lines": ["[OMH] Parallel work ready  •  agents 1  •  run 1"],
                "todo_lines": ["Todo · demo   1/2", "[✓] first", "[ ] second"],
            },
        },
    },
    "no_todo_declared": {
        "gateway": "open",
        "stored": "20260924_101010_abc123",
        "data": {"display": {"line": "[omh] v2.0.5 | plugin:ready", "widget_lines": ["[OMH] idle"], "todo_lines": []}},
    },
    "reader_error": {
        "gateway": "open",
        "stored": None,
        "data": {"error": "RuntimeBindingError: OMH home is not configured", "schema_version": "omh_desktop_hud/v1"},
    },
    "transport_error": {"gateway": "open", "stored": None, "data": None, "errorMessage": "HTTP 404: Plugin not found"},
    "gateway_closed": {"gateway": "connecting", "stored": None, "data": None},
}


def _rewrite_specifiers(source: str, shim_urls: dict[str, str]) -> str:
    """The loader's rewrite: only mapped specifiers change, never other text."""
    return IMPORT_SPECIFIER.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{shim_urls.get(m.group(3), m.group(3))}{m.group(2)}",
        source,
    )


def _plugin_yaml_name() -> str:
    for line in (BUNDLE / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("plugin.yaml has no name line")


class DesktopBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        # The reader resolves the OMH store from the process env when the
        # home's config names none; a temp store keeps the read off ~/.omh.
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes_home = self.root / "hermes"
        self.hermes_home.mkdir()
        patcher = mock.patch.dict(os.environ, {"OMH_HOME": str(self.root / "omh")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_reader_loads_the_bundle_reader_by_path_under_a_private_package(self) -> None:
        reader = plugin_api.load_reader(BUNDLE)
        self.assertEqual(reader.__name__, "omh_desktop_bundle.runtime_reader")
        self.assertEqual(Path(reader.__file__), BUNDLE / "runtime_reader.py")
        self.assertTrue(callable(reader.read_omh_hud))
        # The reader's relative imports resolved to the bundle's own files
        # through the parent package, not to the installed `omh` package.
        self.assertEqual(sys.modules["omh_desktop_bundle"].__path__, [str(BUNDLE)])
        self.assertIn("omh_desktop_bundle.runtime_paths", sys.modules)
        self.assertIs(plugin_api.load_reader(BUNDLE), reader)

    def test_hud_payload_reads_an_empty_hermes_home_without_raising_or_writing(self) -> None:
        before = sorted(str(path) for path in self.root.rglob("*"))
        payload = plugin_api.hud_payload(self.hermes_home, "")
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), before)
        self.assertEqual(payload["schema_version"], "omh_hud/v1")
        self.assertNotIn("error", payload)
        self.assertIsInstance(payload["display"]["line"], str)
        self.assertTrue(payload["display"]["line"].startswith("[omh] "))
        self.assertIsInstance(payload["display"]["widget_lines"], list)
        self.assertIsInstance(payload["display"]["todo_lines"], list)
        self.assertEqual(payload["privacy"], "metadata_only")
        json.dumps(payload)

    def test_hud_payload_scopes_the_plan_to_the_session_the_app_names(self) -> None:
        payload = plugin_api.hud_payload(self.hermes_home, "  20260924_101010_abc123  ")
        self.assertNotIn("error", payload)
        # Naming a session makes the todo session-scoped: an empty home has
        # no record for it, which is `absent`, never a most-recent-TUI guess.
        self.assertEqual(payload["todo"]["status"], "absent")

    def test_hud_payload_answers_a_reader_failure_with_an_error_record(self) -> None:
        empty_bundle = self.root / "not-a-bundle"
        empty_bundle.mkdir()
        payload = plugin_api.hud_payload(self.hermes_home, "", bundle_root=empty_bundle)
        self.assertEqual(payload["schema_version"], "omh_desktop_hud/v1")
        self.assertTrue(payload["error"].startswith("FileNotFoundError: "))
        self.assertNotIn("display", payload)
        # A load that failed leaves no half-initialised module behind (#1623).
        self.assertNotIn("omh_desktop_bundle.runtime_reader", sys.modules)

    def test_hud_payload_answers_a_reader_exception_with_its_class_and_message(self) -> None:
        with mock.patch.object(plugin_api, "load_reader", side_effect=RuntimeError("store is unreadable")):
            payload = plugin_api.hud_payload(self.hermes_home, "")
        self.assertEqual(
            payload,
            {"error": "RuntimeError: store is unreadable", "schema_version": "omh_desktop_hud/v1"},
        )

    def test_resolve_hermes_home_falls_back_to_the_env_then_the_default(self) -> None:
        if "hermes_constants" in sys.modules:  # a Hermes checkout on the path answers itself
            self.skipTest("hermes_constants is importable here; the host answers the home")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}):
            self.assertEqual(plugin_api.resolve_hermes_home(), self.hermes_home)
        with mock.patch.dict(os.environ, {"HERMES_HOME": ""}):
            self.assertEqual(plugin_api.resolve_hermes_home(), Path.home() / ".hermes")

    def test_router_exists_exactly_when_fastapi_does(self) -> None:
        # OMH declares no dependency on FastAPI; the route is the host's
        # surface, and the pure functions above are what OMH tests.
        self.assertEqual(hasattr(plugin_api, "router"), plugin_api.APIRouter is not None)
        if plugin_api.APIRouter is not None:
            self.assertEqual([route.path for route in plugin_api.router.routes], ["/hud"])

    def test_manifest_names_the_plugin_and_its_api_file(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest, {"name": "omh", "api": "plugin_api.py"})
        self.assertEqual(manifest["name"], _plugin_yaml_name())
        self.assertTrue((MANIFEST.parent / manifest["api"]).is_file())


class DesktopPluginFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = PLUGIN_JS.read_text(encoding="utf-8")

    def test_default_export_carries_the_hermes_plugin_shape(self) -> None:
        self.assertIn("export const PLUGIN_ID = 'omh'", self.source)
        self.assertIn("export default {", self.source)
        for field in ("id: PLUGIN_ID", "name: 'oh-my-hermes'", "description: '", "defaultEnabled: false", "register(ctx)"):
            self.assertIn(field, self.source, field)

    def test_only_the_two_shimmed_specifiers_are_imported(self) -> None:
        specifiers = {match.group(3) for match in IMPORT_SPECIFIER.finditer(self.source)}
        self.assertEqual(specifiers, ALLOWED_IMPORTS)

    def test_no_jsx_syntax_in_an_uncompiled_module(self) -> None:
        # The loader blob-imports the file as-is; a `<` anywhere is the one
        # character JSX needs, and the module is written without it.
        self.assertNotIn("<", self.source)

    def test_the_renderer_reads_the_backend_route_and_the_manifest_names_match(self) -> None:
        self.assertIn("/hud", self.source)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn(f"export const PLUGIN_ID = '{manifest['name']}'", self.source)

    def test_the_file_is_ascii_utf8_text_the_bundle_copier_can_hash(self) -> None:
        PLUGIN_JS.read_bytes().decode("utf-8")
        self.assertTrue(self.source.endswith("\n"))
        self.assertNotIn("\r", self.source)


@unittest.skipUnless(NODE, "node is not installed; the widget harness needs it")
class DesktopPluginNodeTests(unittest.TestCase):
    def test_node_accepts_the_file_as_an_es_module(self) -> None:
        # `node --check` on a `.js` copy silently passes JSX (it is read as
        # CommonJS and the check is skipped); the `.mjs` copy is the real gate.
        with TemporaryDirectory() as tmp:
            copy = Path(tmp) / "plugin.mjs"
            copy.write_bytes(PLUGIN_JS.read_bytes())
            completed = subprocess.run(
                [NODE, "--check", str(copy)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _drive(self) -> dict:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sdk.mjs").write_text(SDK_SHIM, encoding="utf-8")
            (root / "jsx-runtime.mjs").write_text(JSX_SHIM, encoding="utf-8")
            shim_urls = {
                "@hermes/plugin-sdk": (root / "sdk.mjs").as_uri(),
                "react/jsx-runtime": (root / "jsx-runtime.mjs").as_uri(),
            }
            plugin = root / "plugin.mjs"
            plugin.write_text(_rewrite_specifiers(PLUGIN_JS.read_text(encoding="utf-8"), shim_urls), encoding="utf-8")
            harness = root / "harness.mjs"
            harness.write_text(HARNESS, encoding="utf-8")
            scenarios = root / "scenarios.json"
            scenarios.write_text(json.dumps(SCENARIOS), encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(harness), str(plugin), str(scenarios)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                cwd=str(root),
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_register_contributes_one_status_item_and_one_pane(self) -> None:
        report = self._drive()
        self.assertEqual(
            report["plugin"],
            {
                "id": "omh",
                "name": "oh-my-hermes",
                "description": "OMH status line and plan todo, read from the omh plugin backend inside the Hermes gateway.",
                "defaultEnabled": False,
            },
        )
        self.assertEqual(
            report["contributions"],
            [
                {"id": "status", "area": "statusBar.right", "order": 130},
                {"id": "hud", "area": "panes", "title": "omh", "data": {"placement": "right", "width": "300px"}},
            ],
        )

    def test_the_status_item_shows_the_reader_line_verbatim_and_polls_every_five_seconds(self) -> None:
        render = self._drive()["renders"]["open_with_payload"]
        self.assertEqual(render["byId"]["status"], ["[omh] v2.0.5 | plugin:ready | target:single"])
        self.assertEqual(render["refetchInterval"], 5000)
        self.assertTrue(render["enabled"])
        self.assertEqual(render["queryKey"], ["omh", "hud", "20260924_101010_abc123"])
        self.assertEqual(render["restCalls"], ["/hud?session=20260924_101010_abc123"])

    def test_the_pane_lists_the_widget_and_todo_lines_the_reader_produced(self) -> None:
        pane = self._drive()["renders"]["open_with_payload"]["byId"]["hud"]
        self.assertEqual(
            pane,
            [
                "oh-my-hermes",
                "session 20260924_101010_abc123",
                "[OMH] Parallel work ready  •  agents 1  •  run 1",
                "Todo · demo   1/2",
                "[✓] first",
                "[ ] second",
            ],
        )

    def test_an_undeclared_todo_is_said_rather_than_invented(self) -> None:
        pane = self._drive()["renders"]["no_todo_declared"]["byId"]["hud"]
        self.assertEqual(pane[-1], "no plan todo declared for this session")
        self.assertIn("[OMH] idle", pane)

    def test_a_reader_error_record_is_shown_as_such(self) -> None:
        render = self._drive()["renders"]["reader_error"]
        self.assertEqual(render["byId"]["status"], ["omh: reader error"])
        self.assertIn("reader error: RuntimeBindingError: OMH home is not configured", render["byId"]["hud"])
        self.assertEqual(render["restCalls"], ["/hud"])

    def test_a_transport_error_degrades_to_backend_unavailable(self) -> None:
        render = self._drive()["renders"]["transport_error"]
        self.assertEqual(render["byId"]["status"], ["omh: backend unavailable"])
        self.assertIn("backend unavailable: HTTP 404: Plugin not found", render["byId"]["hud"])

    def test_polling_waits_for_an_open_gateway(self) -> None:
        render = self._drive()["renders"]["gateway_closed"]
        self.assertFalse(render["enabled"])
        self.assertEqual(render["byId"]["status"], ["omh: gateway connecting"])
        self.assertIn("gateway connecting", render["byId"]["hud"])


class DoctorDesktopHalfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = resolve_paths(root / ".omh", root / ".hermes")

    def _checks(self) -> dict[str, object]:
        with mock.patch(
            "omh.maintenance.doctor.observe_real_loader_registration",
            return_value=dict(LOADER_NOT_OBSERVED),
        ):
            checks = run_doctor(self.paths)
        return {check.name: check for check in checks}

    def test_a_fresh_install_carries_the_desktop_half(self) -> None:
        install_plugin_bundle(self.paths)
        for relative in DESKTOP_HALF_FILES:
            self.assertTrue((self.paths.hermes_plugin_dir / relative).is_file(), relative)
        check = self._checks()["plugin_desktop_half"]
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertIn("Capabilities -> Plugins", check.message)
        # Enablement lives in the app's renderer storage and is not readable
        # from here, so the message never claims the half is on.
        self.assertNotIn("enabled", check.message.split("switched on")[0])

    def test_an_older_bundle_warns_toward_omh_update_without_flipping_the_exit_code(self) -> None:
        install_plugin_bundle(self.paths)
        blocking = lambda checks: {  # noqa: E731 - a two-use predicate
            name for name, check in checks.items() if not check.ok and check.severity == "blocking"
        }
        before = blocking(self._checks())
        shutil.rmtree(self.paths.hermes_plugin_dir / "desktop")
        (self.paths.hermes_plugin_dir / "dashboard" / "manifest.json").unlink()
        stale = self._checks()
        check = stale["plugin_desktop_half"]
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "warning")
        self.assertIn("missing desktop/plugin.js, dashboard/manifest.json", check.message)
        self.assertIn("omh update", check.next_action)
        self.assertTrue(doctor_ok([check]))
        # An installed bundle without the files is also one whose manifest
        # names files that are gone and no longer matches the current package;
        # both are the manifest checks' findings. The removal adds exactly
        # those two blocking checks, never this one.
        self.assertEqual(blocking(stale) - before, {"plugin_bundle_current", "plugin_manifest"})

    def test_the_check_is_absent_when_no_bundle_is_installed(self) -> None:
        self.assertNotIn("plugin_desktop_half", self._checks())


if __name__ == "__main__":
    unittest.main()
