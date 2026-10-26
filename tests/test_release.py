"""The bundled app (packaging/): tools run as the executable itself, and a fresh library opens
the designer on RGB optics."""
import os
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stackforge.core.paths import packaged
from stackforge.gui import runner

RUN = ("import sys; from stackforge.gui import runner; "
       "sys.exit(runner.run_tool(sys.argv[1], sys.argv[2:]))")


def run_tool(*argv):
    return subprocess.run([sys.executable, "-c", RUN, *argv], capture_output=True, timeout=120,
                          env={**os.environ, "FILAMENT_DB": packaged("filaments.json")})


class RunTool(unittest.TestCase):
    def test_frozen_command_reenters_the_executable(self):
        sys.frozen = True
        try:
            self.assertEqual(runner.tool_command("plaque", ["-h"]),
                             [sys.executable, runner.RUN_TOOL, "plaque", "-h"])
        finally:
            del sys.frozen
        self.assertEqual(runner.tool_command("plaque", ["-h"])[1:4],
                         ["-u", "-m", "stackforge.tools.plaque"])

    def test_runs_the_tool_with_its_exit_code(self):
        ok = run_tool("filamentdb", "list")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(b"polymaker-pla-pro-white", ok.stdout)
        bad = run_tool("plaque", "--no-such-flag")
        self.assertEqual(bad.returncode, 2)
        self.assertIn(b"stackforge-plaque: error", bad.stderr)


class FreshLibrary(unittest.TestCase):
    def test_designer_opens_on_rgb_without_spectral_fits(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from stackforge.gui.project import Project
        from stackforge.gui.settings import PresetStore, Settings
        from stackforge.gui.tabs.plaque import PlaqueDesigner
        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as d:
            proj = Project(Settings(os.path.join(d, "s.json")))
            proj.settings.set("db", packaged("filaments.json"))     # no spectral blocks
            des = PlaqueDesigner(proj, PresetStore(os.path.join(d, "presets")))
            self.assertEqual(des.optics(), "rgb")
            self.assertTrue(all(des.list.item(i).flags() & Qt.ItemIsEnabled for i in range(des.list.count())))
            self.assertEqual({f.name for f in des.selected()}, {"White", "Black", "Blue", "Red"})
            des.close()
