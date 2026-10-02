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
                self.assertEqual(set(app.tabs), {"Plaque", "Paint", "Filaments", "Calibrate", "Measure", "Tools"})
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


    def test_every_command_of_every_tool_is_reachable(self):
        """Coverage proof: a form exists in the app for each leaf command of each CLI."""
        import tkinter as tk
        from tdforge.gui.app import HostApp
        from tdforge.gui.run.panel import ToolPanel
        from tests.test_argform import TOOLS
        from tdforge.gui.argform.spec import introspect
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = d
            self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if old is None
                            else os.environ.__setitem__("XDG_CONFIG_HOME", old))
            try:
                app = HostApp()
            except tk.TclError:
                self.skipTest("no usable display")
            self.addCleanup(app.destroy)
            found = set()

            def walk(w):
                if isinstance(w, ToolPanel):
                    found.add((w.tool, w.command))
                for c in w.winfo_children():
                    walk(c)
            walk(app)
            # tools with a dedicated hand-built view are covered by their "All options" form
            want = {(name, path) for name, mod in TOOLS.items()
                    for path, sp in introspect(mod.build_parser(), name).walk() if not sp.subs}
            missing = want - found
            self.assertEqual(missing, set())

    def test_ctrl_enter_runs_only_the_visible_form(self):
        import time
        import tkinter as tk
        from tdforge.gui.app import HostApp
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME")
            try:
                app = HostApp()
            except tk.TclError:
                self.skipTest("no usable display")
            self.addCleanup(app.destroy)
            app.show_tab("Tools")
            app.update()
            fx = app.tabs["Tools"].tabs.panels["make fixtures"]
            app.tabs["Tools"].tabs.select(fx)
            fx.form.set_values({"out_dir": d})
            app.update()
            app.event_generate("<Control-Return>")
            t0 = time.monotonic()
            while (fx.job is None or not fx.job.done) and time.monotonic() - t0 < 60:
                app.update()
                time.sleep(0.02)
            self.assertEqual(fx.job.returncode, 0)
            self.assertTrue(os.path.exists(os.path.join(d, "badge.3mf")))
            other = app.tabs["Tools"].tabs.panels["halftone compare"]
            self.assertIsNone(other.job)

    def test_measure_hex_list_flows_to_calibrate_fit(self):
        import tkinter as tk
        from tdforge.gui.app import HostApp
        from tdforge.gui.tabs.measure import HEX_LINE
        line = 'calibrate.py fit --filament <id> --base <hex or id> --measured "#AABBCC,#112233"'
        self.assertEqual(HEX_LINE.search(line).group(1), "#AABBCC,#112233")
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME")
            try:
                app = HostApp()
            except tk.TclError:
                self.skipTest("no usable display")
            self.addCleanup(app.destroy)
            app.tabs["Calibrate"].set_measured("#AABBCC,#112233")
            self.assertEqual(app.tabs["Calibrate"].tabs.panels["fit"].form.values()["measured"],
                             "#AABBCC,#112233")


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


if __name__ == "__main__":
    unittest.main()
