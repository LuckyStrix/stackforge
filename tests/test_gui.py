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

from stackforge.core.paths import packaged  # noqa: E402
from stackforge.gui.argform import overrides  # noqa: E402
from stackforge.gui.argform.spec import introspect  # noqa: E402
from stackforge.gui.project import Project  # noqa: E402
from stackforge.gui import theme  # noqa: E402
from stackforge.gui.form import CommandForm  # noqa: E402
from stackforge.gui.panel import ToolPanel  # noqa: E402
from stackforge.gui.settings import Settings  # noqa: E402
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
    # done once the job exited AND the terminal has drained it (its timer stops then)
    pump(lambda: panel.job is not None and panel.job.done and not panel.term._timer.isActive(), timeout)
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
        _, parser, spec = [s for s in specs() if s[0] == "top_paint"][0]
        form = CommandForm(spec, "top_paint")
        self.assertTrue(form._cmdline.text().startswith("stackforge-top-paint"))
        self.assertTrue(any("required" in e for e in form.validate()))
        form.set_values({"model": "m.3mf", "image": "i.png", "output": "o.3mf", "palette": "ff0000,00ff00"})
        self.assertEqual(form.validate(), [])
        self.assertEqual(parser.parse_args(form.argv()).palette.lower(), "#ff0000,#00ff00")
        # the other member of the exclusive set is switched off while one is chosen
        self.assertFalse(form.entries["filaments"].widget.is_enabled())
        self.assertEqual(form.set_values({"nonexistent": 1}), ["nonexistent"])
        mk = CommandForm([x for x in specs() if x[0] == "measure"][0][2], "measure", ("measure-wedge",))
        self.assertTrue(mk._cmdline.text().startswith("stackforge-measure measure-wedge"))

    def test_pattern_shows_only_its_parameters(self):
        _, parser, spec = [s for s in specs() if s[0] == "paint"][0]
        form = CommandForm(spec, "paint")
        vis = lambda: {d for d, e in form.entries.items() if e.visible}  # noqa: E731
        self.assertFalse(vis() & {"scale", "lat", "period", "expr"})
        form.set_values({"pattern": "stripes", "period": 3.0, "scale": 9.0})
        self.assertTrue({"axis", "period"} <= vis())
        self.assertNotIn("scale", form.values())       # hidden fields are not sent
        form.set_values({"pattern": "checker3d"})
        self.assertIn("scale", vis())
        self.assertNotIn("period", form.values())

    def test_project_fields_follow_the_bar_and_stay_out_of_presets(self):
        from stackforge.tools import plaque
        with tempfile.TemporaryDirectory() as d:
            proj = Project(Settings(os.path.join(d, "s.json")))
            form = CommandForm(introspect(plaque.build_parser(), "plaque"), "plaque", project=proj)
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
    def test_panel_runs_make_samples(self):
        from stackforge.tools import make_samples
        with tempfile.TemporaryDirectory() as d:
            p = ToolPanel(introspect(make_samples.build_parser(), "make_samples"), "make_samples")
            p.form.set_values({"out_dir": d})
            run_panel(p)
            self.assertEqual(p.job.returncode, 0)
            self.assertTrue(os.path.exists(os.path.join(d, "fabric.3mf")))
            self.assertIn("triangles", p.term.screen.text)

    def test_terminal_answers_input_prompts(self):
        from stackforge.gui.terminal import TerminalView
        from stackforge.gui.runner import Job
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
        from stackforge.tools import make_samples, paint, top_paint
        with tempfile.TemporaryDirectory() as d:
            make_samples.main(["--out-dir", d])
            model, logo = os.path.join(d, "badge.3mf"), os.path.join(d, "logo.png")
            Image.new("RGB", (8, 8), (255, 0, 0)).save(logo)
            pal = "000000,ff0000,ffff00"
            cases = [
                ("paint", paint.build_parser,
                 {"model": model, "pattern": "stripes", "palette": pal, "period": 6.0, "resolution": 1.2,
                  "layer_height": 0.2},
                 ["-m", "stackforge.tools.paint", model, "--palette", pal, "--pattern", "stripes",
                  "--period", "6", "--resolution", "1.2", "--layer-height", "0.2"]),
                ("top_paint", top_paint.build_parser,
                 {"model": model, "image": logo, "palette": pal, "resolution": 1.2, "layer_height": 0.2},
                 ["-m", "stackforge.tools.top_paint", model, logo, "--palette", pal, "--resolution", "1.2",
                  "--layer-height", "0.2"]),
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


class Pickers(unittest.TestCase):
    """Filaments and colours are chosen by looking at them, never by typing an id."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.proj = Project(Settings(os.path.join(self.d.name, "s.json")))
        self.proj.settings.set("db", packaged("filaments.json"))

    def form(self, tool, builder, command=()):
        from importlib import import_module  # noqa: F401
        return CommandForm(introspect(builder(), tool), tool, command, project=self.proj)

    def test_no_filament_id_is_typed_anywhere(self):
        """Every flag that names a filament or a colour list gets a picker, not a text box."""
        from stackforge.gui import pickers  # noqa: F401
        want = {"filament_id", "filament_ids", "filament_or_hex", "hex_list", "color"}
        n = 0
        for name, parser, spec in specs():
            for path, sp in spec.walk():
                for f in sp.fields:
                    h = (f.help or "").lower()
                    names_filament = ("filament id" in h or "ids from the filament" in h
                                      or "database ids" in h or "filament ids" in h)
                    kind = overrides.resolve_kind(name, " ".join(path), f)
                    if names_filament:
                        self.assertIn(kind, want, (name, path, f.dest, f.help))
                        n += 1
        self.assertGreater(n, 5)

    def test_filament_combo_and_checklist_values(self):
        from stackforge.tools import plaque
        form = self.form("plaque", plaque.build_parser)
        base, fils = form.entries["base"].widget, form.entries["filaments"].widget
        base.set("polymaker-pla-pro-white")
        self.assertEqual(base.get(), "polymaker-pla-pro-white")
        self.assertIn("White", base.widget.currentText())
        fils.set("polymaker-pla-pro-white,polymaker-pla-pro-red")
        self.assertEqual(fils.get(), "polymaker-pla-pro-white,polymaker-pla-pro-red")
        self.assertEqual(form.values()["filaments"], "polymaker-pla-pro-white,polymaker-pla-pro-red")

    def test_checklist_dialog_orders_and_selects(self):
        from stackforge.gui.pickers import FilamentChecklist, FilamentSource
        src = FilamentSource(self.proj)
        dlg = FilamentChecklist(src.filaments(), ["polymaker-pla-pro-red", "polymaker-pla-pro-white"])
        self.assertEqual(dlg.chosen(), ["polymaker-pla-pro-red", "polymaker-pla-pro-white"])
        dlg.list.setCurrentRow(1)
        dlg._move(-1)
        self.assertEqual(dlg.chosen()[0], "polymaker-pla-pro-white")

    def test_palette_and_color_fields(self):
        from stackforge.tools import top_paint
        form = self.form("top_paint", top_paint.build_parser)
        pal = form.entries["palette"].widget
        pal.set("#ff0000 00ff00")
        self.assertEqual(pal.get(), "#FF0000,#00FF00")
        from stackforge.core import filamentdb
        add = self.form("filamentdb", filamentdb.build_parser, ("add",))
        c = add.entries["color"].widget
        c.set("#336699")
        self.assertEqual(c.get(), "#336699")

    def test_calibrate_base_accepts_a_filament_or_a_colour(self):
        from stackforge.tools import calibrate
        fit = self.form("calibrate", calibrate.build_parser, ("fit",))
        base = fit.entries["base"].widget
        base.set("#E8E8EE")
        self.assertEqual(base.get(), "#E8E8EE")
        base.set("polymaker-pla-pro-white")
        self.assertEqual(base.get(), "polymaker-pla-pro-white")

    def test_labels_are_words_not_flags(self):
        from stackforge.tools import paint
        form = self.form("paint", paint.build_parser)
        labels = {e.label.text() for e in form.entries.values()}
        self.assertIn("Palette (colours)", labels)
        self.assertFalse(any(t.startswith("--") for t in labels))
        self.assertEqual(form.entries["scale"].label.toolTip().splitlines()[0], "--scale")


from stackforge.tools import plaque  # noqa: E402


class Designer(unittest.TestCase):
    def setUp(self):
        from stackforge.gui.tabs.plaque import PlaqueDesigner
        from stackforge.gui.settings import PresetStore
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.proj = Project(Settings(os.path.join(self.d.name, "s.json")))
        self.proj.settings.set("db", packaged("filaments.json"))
        self.des = PlaqueDesigner(self.proj, PresetStore(os.path.join(self.d.name, "presets")),
                                  image_path=os.path.join(root, "docs", "plaque_target.png"))
        self.des.show()
        self.addCleanup(self.des.close)

    def test_filament_checklist_defaults_and_controls(self):
        d = self.des
        self.assertEqual(d.list.count(), len(d.db.filaments))
        self.assertEqual({f.name for f in d.selected()}, {"White", "Black", "Blue", "Red"})
        self.assertEqual(d._base_id(), "polymaker-pla-pro-white")
        self.assertEqual(d.base.currentText(), "Polymaker PLA Pro White")
        d._set_all(False)
        self.assertEqual(d.selected(), [])
        d.filter.setText("teal")
        d._set_all(True)                      # only the visible rows
        self.assertEqual([f.name for f in d.selected()], ["Teal"])
        d.filter.setText("")
        d._select_measured()
        self.assertTrue(all(f.provenance == "measured" for f in d.selected()))

    def test_generate_goes_stale_exports_and_presets_roundtrip(self):
        from unittest import mock
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        d = self.des
        t = os.path.join(self.d.name, "t.3mf")
        make_template(t)
        self.proj.set("template", t)
        d.s_width.setValue(40)
        d.s_maxl.setValue(6)
        d._generate()
        pump(lambda: d.result is not None, 90)
        self.assertIsNotNone(d.result)
        self.assertTrue(d.btn_export.isEnabled())
        # auto base layers: opaque on the template's grid
        self.assertIsNone(plaque.base_warning(d.result["base"], d.result["args"]))
        out = os.path.join(self.d.name, "plaque.3mf")
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(out, "")), \
                mock.patch.object(QMessageBox, "information") as info:
            d._export()
            pump(lambda: info.called, 90)
        self.assertTrue(os.path.exists(out))
        self.assertIn("T1", info.call_args[0][2])
        self.assertIn("Flash Studio", info.call_args[0][2])
        self.assertEqual(zipfile.ZipFile(out).testzip(), None)
        d._preset_values()
        with mock.patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("mine", True)):
            d._save_preset()
        d.s_width.setValue(90)                # changing an input marks the result stale
        self.assertIsNone(d.result)
        self.assertIn("out of date", d.views.tabText(1))
        self.assertFalse(d.btn_export.isEnabled())
        d.preset_combo.setCurrentText("mine")
        d._apply_preset()
        self.assertEqual(d.s_width.value(), 40)

    def test_generate_refuses_without_a_layer_grid(self):
        from unittest import mock
        from PySide6.QtWidgets import QMessageBox
        d = self.des
        with mock.patch.object(QMessageBox, "warning") as warn:
            d._generate()
        self.assertIn("Slicer project", warn.call_args[0][2])
        self.assertFalse(d.worker.busy)
        self.proj.set("template", os.path.join(self.d.name, "gone.3mf"))
        with mock.patch.object(QMessageBox, "warning") as warn:
            d._generate()
        self.assertIn("moved or deleted", warn.call_args[0][2])

    def test_plaque_bigger_than_the_bed_is_refused(self):
        from unittest import mock
        from PySide6.QtWidgets import QMessageBox
        t = os.path.join(self.d.name, "t.3mf")
        make_template(t)
        self.proj.set("template", t)
        self.des.s_width.setValue(400)
        with mock.patch.object(QMessageBox, "warning") as warn:
            self.des._generate()
        self.assertIn("does not fit", warn.call_args[0][2])
        self.assertFalse(self.des.worker.busy)

    def test_layers_come_from_the_project_bar(self):
        t = os.path.join(self.d.name, "t.3mf")
        make_template(t)
        self.proj.set("template", t)
        a = self.des.config_ns()
        self.assertEqual((a.layer_height, a.first_layer_height), self.proj.layers())


class Host(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        from stackforge.gui.app import HostWindow
        self.win = HostWindow(Settings(os.path.join(self.d.name, "settings.json")))
        self.win.show()
        self.addCleanup(self.win.close)
        app.processEvents()

    def test_builds_switches_and_saves_settings(self):
        self.assertEqual(set(self.win.tabs), {"Plaque", "Paint", "Filaments", "Calibrate", "Measure", "Spectra", "Tools"})
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

    def test_a_failed_run_says_why(self):
        tt = self.win.tabs["Tools"].tabs
        fx = tt.panels["make fixtures"]
        fx.form.set_values({"out_dir": os.path.join(self.d.name, "no", "such", "dir")})
        fx.run()
        pump(lambda: fx.job is not None and fx.job.done and not fx.term._timer.isActive())
        if fx.job.returncode == 0:
            self.skipTest("make_samples created the directory")
        box = fx._failure_box
        self.assertIn("failed", box.text())
        box.close()

    def test_calibrate_fit_saves_by_default(self):
        self.assertTrue(self.win.tabs["Calibrate"].tabs.panels["fit"].form.values()["write"])

    def test_measure_hex_list_flows_to_calibrate_fit(self):
        from stackforge.gui.tabs.measure import HEX_LINE
        line = 'calibrate.py fit --filament <id> --base <hex or id> --measured "#AABBCC,#112233"'
        self.assertEqual(HEX_LINE.search(line).group(1), "#AABBCC,#112233")
        cal = self.win.tabs["Calibrate"]
        self.assertEqual(cal.set_measured("#AABBCC,#112233,#445566"), "A")
        self.assertIs(cal.nb.currentWidget(), cal.guided)
        self.assertEqual(cal.guided.wedge_a.text.toPlainText(), "#AABBCC,#112233,#445566")
        self.assertEqual(cal.tabs.panels["fit"].form.values()["measured"], "#AABBCC,#112233,#445566")

    def test_measure_base_and_readings_file_flow_to_calibrate(self):
        from stackforge.gui.tabs.measure import BASE_LINE, READINGS_LINE
        line = 'Next: stackforge-calibrate fit --filament <id> --base "#0C0E0C" --measured "#1"'
        self.assertEqual(BASE_LINE.search(line).group(1), "#0C0E0C")
        out = "...\nreadings file: C:\\Users\\x\\wedge_teal.readings.json\nNext: ..."
        self.assertEqual(READINGS_LINE.findall(out), ["C:\\Users\\x\\wedge_teal.readings.json"])
        cal = self.win.tabs["Calibrate"]
        self.assertEqual(cal.set_measured("#AABBCC,#112233,#445566", "#0C0E0C"), "A")
        self.assertEqual(cal.guided.wedge_a.base.currentText(), "#0C0E0C")

    def test_filament_editor_hands_over_to_calibrate(self):
        fil = self.win.tabs["Filaments"]
        fil.ed.select("polymaker-pla-pro-blue")
        fil.ed.request_calibrate()
        cal = self.win.tabs["Calibrate"]
        self.assertIs(self.win.current(), cal)
        self.assertEqual(cal.guided.fil_combo.currentData(), "polymaker-pla-pro-blue")

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
