"""Each TUI module that sits on an import cycle's path imports on its own, first, in a fresh interpreter.

A merge of two branches that each passed alone once left `panels` → `background` → `bot_board` → `prompts` →
`panels` with `panels` half-initialised, and every TUI test module failed to import. A test process imports `app`
early, so only a fresh interpreter importing one module first sees the order a cycle breaks on.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

# The modules on that cycle's path, and the app that imports them all.
FIRST_IMPORTS = ("omh.tui.prompts", "omh.tui.panels", "omh.tui.bot_board", "omh.tui.theme", "omh.tui.app")


class ImportOrderTests(unittest.TestCase):
    def test_each_module_imports_first_in_a_fresh_interpreter(self) -> None:
        for module in FIRST_IMPORTS:
            with self.subTest(module=module):
                # `-P`: the checkout's own `omh/` shim is not put first on the path, so this is the installed tree.
                done = subprocess.run([sys.executable, "-P", "-c", f"import {module}"], capture_output=True,
                                      text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_catch_mark_is_one_value_wherever_it_is_read(self) -> None:
        from omh.tui import panels, prompts, theme

        self.assertIs(panels.CATCH_MARK, theme.CATCH_MARK)
        self.assertIs(prompts.CATCH_MARK, theme.CATCH_MARK)


if __name__ == "__main__":
    unittest.main()
