"""Qt GUI tests. Run offscreen, so they need no display."""
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tdforge.gui.argform import overrides  # noqa: E402
from tdforge.gui.argform.spec import introspect  # noqa: E402
from tdforge.gui.project import Project  # noqa: E402
from tdforge.gui.qt import theme  # noqa: E402
from tdforge.gui.qt.form import CommandForm  # noqa: E402
from tdforge.gui.qt.panel import ToolPanel  # noqa: E402
from tdforge.gui.settings import Settings  # noqa: E402
from tests.test_argform import TOOLS, specs  # noqa: E402
from tests.test_template import make_template  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply(app)


def pump(cond, timeout=60):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(10):
        app.processEvents()


def run_panel(panel, timeout=120):
    panel.run()
    pump(lambda: panel.job is not None and panel.job.done, timeout)
    return panel


def entries(path):
    z = zipfile.ZipFile(path)
    return {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist()}


class Forms(unittest.TestCase):
    def test_every_command_builds(self):
        n = 0
        for name, parser, spec in specs():
            for path, sp in spec.walk():
                if sp.subs:
                    continue
                form = CommandForm(spec, name, path)
                want = {f.dest for s in form.chain for f in s.fields
                        if overrides.resolve_kind(name, " ".join(path), f) != "hide"}
                self.assertEqual(set(form.entries), want)
                n += 1
        self.assertGreaterEqual(n, 24)

    def test_exclusion_required_and_cmdline(self):
        _, parser, spec = [s for s in specs() if s[0] == "topdeco"][0]
        form = CommandForm(spec, "topdeco")
        self.assertTrue(form._cmdline.text().startswith("topdeco"))
        self.assertTrue(any("required" in e for e in form.validate()))
        form.set_values({"model": "m.3mf", "image": "i.png", "output": "o.3mf", "palette": "ff0000,00ff00"})
        self.assertEqual(form.validate(), [])
        self.assertEqual(parser.parse_args(form.argv()).palette, "ff0000,00ff00")
        # the other member of the exclusive set is switched off while one is chosen
        self.assertFalse(form.entries["filaments"].widget.edit.isEnabled())
        self.assertEqual(form.set_values({"nonexistent": 1}), ["nonexistent"])
        mk = CommandForm([x for x in specs() if x[0] == "munki"][0][2], "munki", ("measure-wedge",))
        self.assertTrue(mk._cmdline.text().startswith("munki measure-wedge"))

    def test_pattern_shows_only_its_parameters(self):
        _, parser, spec = [s for s in specs() if s[0] == "surfacecolor"][0]
        form = CommandForm(spec, "surfacecolor")
        vis = lambda: {d for d, e in form.entries.items() if e.visible}  # noqa: E731
        self.assertFalse(vis() & {"scale", "lat", "period", "expr"})
        form.set_values({"pattern": "stripes", "period": 3.0, "scale": 9.0})
        self.assertTrue({"axis", "period"} <= vis())
        self.assertNotIn("scale", form.values())       # hidden fields are not sent
        form.set_values({"pattern": "checker3d"})
        self.assertIn("scale", vis())
        self.assertNotIn("period", form.values())

    def test_project_fields_follow_the_bar_and_stay_out_of_presets(self):
        from tdforge.tools import stackforge
        with tempfile.TemporaryDirectory() as d:
            proj = Project(Settings(os.path.join(d, "s.json")))
            form = CommandForm(introspect(stackforge.build_parser(), "stackforge"), "stackforge", project=proj)
            proj.subscribe(form.refresh_project)
            self.assertIsNone(form.values().get("layer_height"))
            t = os.path.join(d, "t.3mf")
            make_template(t)
            proj.set("template", t)
            self.assertEqual(form.values()["layer_height"], proj.layers()[0])
            self.assertEqual(form.values()["template"], t)
            form.set_values({"width": 60.0})
            pv = form.preset_values()
            self.assertEqual(pv["width"], 60.0)
            for k in ("template", "layer_height", "first_layer_height", "db"):
                self.assertNotIn(k, pv)


class Panels(unittest.TestCase):
    def test_panel_runs_make_fixture(self):
        from tdforge.tools import make_fixture
        with tempfile.TemporaryDirectory() as d:
            p = ToolPanel(introspect(make_fixture.build_parser(), "make_fixture"), "make_fixture")
            p.form.set_values({"out_dir": d})
            run_panel(p)
            self.assertEqual(p.job.returncode, 0)
            self.assertTrue(os.path.exists(os.path.join(d, "fabric.3mf")))
            self.assertIn("triangles", p.term.screen.text)

    def test_terminal_answers_input_prompts(self):
        from tdforge.gui.qt.terminal import TerminalView
        from tdforge.gui.run.runner import Job
        term = TerminalView()
        job = Job([sys.executable, "-u", "-c", "x = input('press Enter... '); print('got', repr(x))"])
        term.attach(job)
        pump(lambda: "press Enter" in term.screen.text, 20)
        term.entry.setText("")
        term._send()
        pump(lambda: job.done and not term._timer.isActive(), 20)
        self.assertIn("got ''", term.screen.text)

    def test_paint_output_matches_cli(self):
        from PIL import Image
        from tdforge.tools import make_fixture, surfacecolor, topdeco
        with tempfile.TemporaryDirectory() as d:
            make_fixture.main(["--out-dir", d])
            model, logo = os.path.join(d, "badge.3mf"), os.path.join(d, "logo.png")
            Image.new("RGB", (8, 8), (255, 0, 0)).save(logo)
            pal = "000000,ff0000,ffff00"
            cases = [
                ("surfacecolor", surfacecolor.build_parser,
                 {"model": model, "pattern": "stripes", "palette": pal, "period": 6.0, "resolution": 1.2},
                 ["-m", "tdforge.tools.surfacecolor", model, "--palette", pal, "--pattern", "stripes",
                  "--period", "6", "--resolution", "1.2"]),
                ("topdeco", topdeco.build_parser,
                 {"model": model, "image": logo, "palette": pal, "resolution": 1.2},
                 ["-m", "tdforge.tools.topdeco", model, logo, "--palette", pal, "--resolution", "1.2"]),
            ]
            for tool, builder, values, cli in cases:
                gui_out, cli_out = (os.path.join(d, f"{tool}_{k}.3mf") for k in ("gui", "cli"))
                p = ToolPanel(introspect(builder(), tool), tool)
                p.form.set_values({**values, "output": gui_out})
                run_panel(p)
                self.assertEqual(p.job.returncode, 0, p.term.screen.text)
                r = subprocess.run([sys.executable, *cli, "-o", cli_out], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(entries(gui_out), entries(cli_out), tool)


class Host(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        from tdforge.gui.qt.app import HostWindow
        self.win = HostWindow(Settings(os.path.join(self.d.name, "settings.json")))
        self.win.show()
        self.addCleanup(self.win.close)
        app.processEvents()

    def test_builds_switches_and_saves_settings(self):
        self.assertEqual(set(self.win.tabs), {"Plaque", "Paint", "Filaments", "Calibrate", "Measure", "Tools"})
        self.win.show_tab("Tools")
        self.assertIs(self.win.current(), self.win.tabs["Tools"])
        self.win.close()
        self.assertEqual(self.win.settings.get("last_tab"), "Tools")
        self.assertTrue(os.path.exists(os.path.join(self.d.name, "settings.json")))

    def test_every_command_of_every_tool_is_reachable(self):
        found = {(p.tool, p.command) for p in self.win.findChildren(ToolPanel)}
        want = {(name, path) for name, mod in TOOLS.items()
                for path, sp in introspect(mod.build_parser(), name).walk() if not sp.subs}
        self.assertEqual(want - found, set())

    def test_ctrl_enter_runs_only_the_visible_form(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.win.show_tab("Tools")
        tt = self.win.tabs["Tools"].tabs
        fx, other = tt.panels["make fixtures"], tt.panels["halftone compare"]
        tt.select(fx)
        fx.form.set_values({"out_dir": self.d.name})
        app.processEvents()
        QTest.keyClick(self.win, Qt.Key_Return, Qt.ControlModifier)
        pump(lambda: fx.job is not None and fx.job.done)
        self.assertEqual(fx.job.returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.d.name, "badge.3mf")))
        self.assertIsNone(other.job)

    def test_measure_hex_list_flows_to_calibrate_fit(self):
        from tdforge.gui.qt.tabs.measure import HEX_LINE
        line = 'calibrate.py fit --filament <id> --base <hex or id> --measured "#AABBCC,#112233"'
        self.assertEqual(HEX_LINE.search(line).group(1), "#AABBCC,#112233")
        self.win.tabs["Calibrate"].set_measured("#AABBCC,#112233")
        self.assertEqual(self.win.tabs["Calibrate"].tabs.panels["fit"].form.values()["measured"],
                         "#AABBCC,#112233")

    def test_project_bar_derives_layers_from_template(self):
        t = os.path.join(self.d.name, "t.3mf")
        make_template(t)
        bar = self.win.bar
        bar.template.setText(t)
        bar.template.editingFinished.emit()
        self.assertEqual(bar.lh.text(), "0.12")
        self.assertTrue(bar.lh.isReadOnly())
        bar.override.setChecked(True)
        self.assertFalse(bar.lh.isReadOnly())
        bar.lh.setText("0.2")
        bar.lh.editingFinished.emit()
        self.assertEqual(self.win.project.get("layer_height"), "0.2")


if __name__ == "__main__":
    unittest.main()
