"""The host app builds, switches tabs and closes. Skipped without a display."""
import os
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class HostSmoke(unittest.TestCase):
    def test_host_builds_and_switches_tabs(self):
        import tkinter as tk
        from tdforge.gui.app import HostApp
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = d
            self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if old is None
                            else os.environ.__setitem__("XDG_CONFIG_HOME", old))
            try:
                app = HostApp()
            except tk.TclError:
                self.skipTest("no usable display")
            try:
                app.update()
                self.assertEqual(set(app.tabs), {"Plaque", "Filaments", "Paint"})
                app.show_tab("Filaments")
                app.update()
                self.assertIs(app.current(), app.tabs["Filaments"])
                app.show_tab("Plaque")
                app.update()
                app._close()
                self.assertTrue(os.path.exists(os.path.join(d, "tdforge", "settings.json")))
            finally:
                try:
                    app.destroy()
                except tk.TclError:
                    pass


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class PaintMatchesCli(unittest.TestCase):
    """The GUI only builds argv, so the same inputs must give the same 3MF as the CLI."""

    def _run_panel(self, root, tool, builder, values):
        import time
        from tdforge.gui.argform.spec import introspect
        from tdforge.gui.run.panel import ToolPanel
        panel = ToolPanel(root, introspect(builder(), tool), tool)
        panel.pack()
        panel.form.set_values(values)
        panel.run()
        t0 = time.monotonic()
        while panel.job and not panel.job.done and time.monotonic() - t0 < 120:
            root.update()
            time.sleep(0.02)
        for _ in range(10):
            root.update()
        self.assertEqual(panel.job.returncode, 0, panel.term.screen.text)

    @staticmethod
    def _entries(path):
        import hashlib
        import zipfile
        z = zipfile.ZipFile(path)
        return {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist()}

    def test_surfacecolor_and_topdeco(self):
        import subprocess
        import sys
        import tkinter as tk
        from PIL import Image
        from tdforge.tools import make_fixture, surfacecolor, topdeco
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no usable display")
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as d:
            make_fixture.main(["--out-dir", d])
            model = os.path.join(d, "badge.3mf")
            logo = os.path.join(d, "logo.png")
            Image.new("RGB", (8, 8), (255, 0, 0)).save(logo)
            cases = [
                ("surfacecolor", surfacecolor.build_parser,
                 {"model": model, "pattern": "stripes", "palette": "000000,ff0000,ffff00",
                  "period": 6.0, "resolution": 1.2},
                 ["-m", "tdforge.tools.surfacecolor", model, "--palette", "000000,ff0000,ffff00",
                  "--pattern", "stripes", "--period", "6", "--resolution", "1.2"]),
                ("topdeco", topdeco.build_parser,
                 {"model": model, "image": logo, "palette": "000000,ff0000,ffff00",
                  "resolution": 1.2},
                 ["-m", "tdforge.tools.topdeco", model, logo, "--palette", "000000,ff0000,ffff00",
                  "--resolution", "1.2"]),
            ]
            for tool, builder, values, cli in cases:
                gui_out, cli_out = (os.path.join(d, f"{tool}_{k}.3mf") for k in ("gui", "cli"))
                self._run_panel(root, tool, builder, {**values, "output": gui_out})
                r = subprocess.run([sys.executable, *cli, "-o", cli_out], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self._entries(gui_out), self._entries(cli_out), tool)
