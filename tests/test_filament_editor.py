"""The Qt filament editor: field binding, dirty tracking, saving, and the optics helpers behind it."""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from stackforge.core import optics  # noqa: E402
from stackforge.core.filamentdb import DB  # noqa: E402
from stackforge.core.paths import packaged  # noqa: E402
from stackforge.gui import theme  # noqa: E402
from stackforge.gui.filaments.calibrate import blocked, fit_lines  # noqa: E402
from stackforge.gui.filaments.editor import FilamentEditor  # noqa: E402
from stackforge.tools import calibrate  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply(app)


class Optics(unittest.TestCase):
    def setUp(self):
        self.fil = DB(packaged("filaments.json")).filaments["polymaker-pla-pro-red"]

    def test_stack_starts_at_base_and_approaches_filament(self):
        ramp = optics.stack_colors(self.fil, "#F4F5F0", 40, 0.08)
        self.assertEqual(ramp.shape, (41, 3))
        self.assertTrue(np.allclose(ramp[0], [0xF4, 0xF5, 0xF0], atol=1.5))
        self.assertLess(np.abs(ramp[-1] - self.fil.rgb()).max(), 4)

    def test_opaque_at_and_best_layers(self):
        n = optics.opaque_at(self.fil, 0.08)
        self.assertIsNotNone(n)
        self.assertLess(self.fil.transmittance(n * 0.08).max(), 0.01)
        best, de = optics.best_layers(self.fil, "#F4F5F0", 0.08)
        self.assertGreater(best, 0)
        self.assertGreater(de, 0)

    def test_optics_lines_flag_estimates(self):
        lines = optics.optics_lines(self.fil, 0.08)
        self.assertEqual(lines[0][1], "head")
        self.assertIn("warn", [k for _, k in lines])      # provenance is not 'measured'


class Calibration(unittest.TestCase):
    def test_single_low_contrast_wedge_is_blocked(self):
        self.assertTrue(blocked(10, 1))
        self.assertFalse(blocked(10, 2))
        self.assertFalse(blocked(30, 1))

    def test_fit_report_round_trip(self):
        fil = DB(packaged("filaments.json")).filaments["polymaker-pla-pro-red"]
        lh, steps = 0.08, 8
        base = np.array([244.0, 245.0, 240.0])
        n = np.arange(1, steps + 1)[:, None]
        T = np.exp(-(n * lh) / 0.2)
        from stackforge.core import colormath
        lin = colormath.srgb_to_linear(base) * T + colormath.srgb_to_linear(fil.rgb()) * (1 - T)
        meas = colormath.linear_to_srgb(lin)
        fit = calibrate.fit_td([(meas, base)], lh, False, fil.rgb())
        lines = fit_lines(fil, [(meas, base)], lh, False, fit)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("fit dE", text)
        self.assertAlmostEqual(float(np.ravel(fit[0])[0]), 0.2, delta=0.03)


class Editor(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.path = os.path.join(self.d.name, "f.json")
        shutil.copy(packaged("filaments.json"), self.path)
        self.ed = FilamentEditor(self.path, catalog_path=packaged("polymaker_catalog.json"))
        self.addCleanup(self.ed.deleteLater)

    def test_edits_mark_dirty_and_reach_the_entry(self):
        ed = self.ed
        self.assertFalse(ed.dirty)
        ed.e_name.setText("Renamed")
        ed.e_color.setText("#123456")
        ed.s_td.setValue(0.5)
        self.assertTrue(ed.dirty)
        fil = ed.fil()
        self.assertEqual((fil.name, fil.color, fil.td), ("Renamed", "#123456", 0.5))

    def test_tab_shows_unsaved_dot(self):
        from stackforge.gui.tabs.filaments import FilamentsTab
        from stackforge.gui.project import Project
        from stackforge.gui.settings import Settings
        proj = Project(Settings(os.path.join(self.d.name, "s.json")))
        proj.settings.set("db", self.path)
        tab = FilamentsTab(proj)
        self.addCleanup(tab.deleteLater)
        self.assertEqual(tab.nb.tabText(0), "Editor")
        tab.ed.e_name.setText("x")
        self.assertEqual(tab.nb.tabText(0), "Editor •")
        self.assertTrue(tab.ed.save())
        self.assertEqual(tab.nb.tabText(0), "Editor")

    def test_selecting_loads_fields_without_dirtying(self):
        ed = self.ed
        other = sorted(ed.db.filaments)[3]
        ed.select(other)
        self.assertFalse(ed.dirty)
        self.assertEqual(ed.e_id.text(), other)
        self.assertEqual(ed.s_td.value(), ed.db.filaments[other].td)

    def test_bad_colour_is_not_stored_and_blocks_save_if_it_gets_in(self):
        ed = self.ed
        before = ed.fil().color
        ed.e_color.setText("#12")
        self.assertEqual(ed.fil().color, before)
        ed.fil().color = "nope"
        self.assertTrue(ed.problems())

    def test_per_channel_starts_from_scalar_td_and_clears(self):
        ed = self.ed
        ed.s_td.setValue(0.4)
        ed.k_perch.setChecked(True)
        self.assertEqual(ed.fil().td_rgb, [0.4, 0.4, 0.4])
        ed.k_perch.setChecked(False)
        self.assertIsNone(ed.fil().td_rgb)

    def test_new_duplicate_rename_save_and_revert(self):
        ed = self.ed
        n = len(ed.db.filaments)
        ed.new_filament()
        ed.duplicate()
        self.assertEqual(len(ed.db.filaments), n + 2)
        ed.e_id.setText("My Teal")
        ed._commit_id()
        self.assertEqual(ed.current, "my-teal")
        self.assertTrue(ed.save())
        self.assertFalse(ed.dirty)
        self.assertIn("my-teal", DB(self.path).filaments)
        ed.fil().td = 9.0
        ed._set_dirty(True)
        old = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        self.addCleanup(setattr, QMessageBox, "question", old)
        ed.revert()
        self.assertFalse(ed.dirty)
        self.assertNotEqual(ed.db.filaments["my-teal"].td, 9.0)

    def test_match_by_eye_applies_a_td_and_marks_matched(self):
        ed = self.ed
        ed.select("polymaker-pla-pro-red")
        ed.match.refresh()
        old = ed.fil().td
        ed.match.apply(old * 1.5, 1.5)
        self.assertEqual(ed.fil().provenance, "matched")
        self.assertAlmostEqual(ed.fil().td, round(old * 1.5, 4))
        self.assertTrue(ed.dirty)

    def test_shared_layer_height_reaches_every_page(self):
        ed = self.ed
        ed.layer.set(0.12)
        self.assertEqual(ed.layer.value, 0.12)
        txt = ed.look.report.toPlainText()
        self.assertIn("0.12", txt)

    def _tab(self):
        from stackforge.gui.tabs.filaments import FilamentsTab
        from stackforge.gui.project import Project
        from stackforge.gui.settings import Settings
        from tests.test_template import make_template
        proj = Project(Settings(os.path.join(self.d.name, "s.json")))
        proj.settings.set("db", self.path)
        tpl = os.path.join(self.d.name, "t.3mf")
        make_template(tpl)
        proj.set("template", tpl)
        tab = FilamentsTab(proj)
        self.addCleanup(tab.deleteLater)
        return tab

    def _page(self, tab):
        from stackforge.gui.filaments.calibrate import CalibratePage
        page = CalibratePage(tab.project)
        self.addCleanup(page.deleteLater)
        return page

    def test_wedge_is_written_on_the_project_grid_with_the_template(self):
        import json
        import zipfile
        from unittest import mock
        from PySide6.QtWidgets import QFileDialog
        tab = self._tab()
        self.assertEqual(tab.ed.layer.value, 0.12)           # the editor follows the project
        page = self._page(tab)
        self.assertEqual(page.layer.value, 0.12)             # and so does calibration
        self.assertTrue(page.select("polymaker-pla-pro-blue"))
        out = os.path.join(self.d.name, "w.3mf")
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(out, "")), \
                mock.patch.object(QMessageBox, "information"):
            page.write_wedge()
        with zipfile.ZipFile(out) as z:
            prof = json.loads(z.read("Metadata/project_settings.config"))
            self.assertIn("Metadata/model_settings.config", z.namelist())
            model = z.read("3D/3dmodel.model").decode()
        self.assertEqual(prof["sparse_infill_density"], "100%")
        self.assertEqual(prof["layer_height"], "0.12")
        import re
        z = np.array([float(v) for v in re.findall(r'z="([-\d.eE]+)"', model)])
        # base top and every step edge on first + k*layer
        self.assertTrue(np.allclose(((z[z > 0] - 0.2) / 0.12 + 1e-6) % 1, 0, atol=1e-4))

    def test_white_gets_one_wedge_over_the_dark_base(self):
        import re
        import zipfile
        from unittest import mock
        from PySide6.QtWidgets import QFileDialog
        page = self._page(self._tab())
        self.assertEqual(page.wbase.currentText(), "polymaker-pla-pro-white")
        self.assertEqual(page.wbase2.currentText(), "polymaker-pla-pro-black")
        page.select("polymaker-pla-pro-white")
        out = os.path.join(self.d.name, "w.3mf")
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(out, "")), \
                mock.patch.object(QMessageBox, "information") as info:
            page.write_wedge()
        with zipfile.ZipFile(out) as z:
            cfg = z.read("Metadata/model_settings.config").decode()
        self.assertEqual(set(re.findall(r'key="name" value="(wedge_over_[^"]+)"', cfg)),
                         {"wedge_over_polymaker-pla-pro-black"})
        msg = info.call_args[0][2]
        self.assertIn("Extruder 1 = ", msg)
        self.assertNotIn("Extruder 3", msg)
        self.assertEqual(page.wedge_a.base.currentText(), "polymaker-pla-pro-black")

    def test_wedge_settings_saved_as_default_load_next_time(self):
        from stackforge.gui.filaments.calibrate import CalibratePage
        from stackforge.gui.settings import PresetStore
        tab = self._tab()
        store = PresetStore(os.path.join(self.d.name, "presets"))
        page = CalibratePage(tab.project, presets=store)
        self.addCleanup(page.deleteLater)
        page.gap.setValue(8)
        page.hinge.setValue(3)
        page.steps.setValue(10)
        page.wbase2.setCurrentText("polymaker-pla-pro-dark-blue")
        page.save_defaults()
        again = CalibratePage(tab.project, presets=store)       # the next start
        self.addCleanup(again.deleteLater)
        self.assertEqual((again.gap.value(), again.hinge.value(), again.steps.value()), (8, 3, 10))
        self.assertTrue(again.hinge.isEnabled())
        self.assertEqual(again.wbase2.currentText(), "polymaker-pla-pro-dark-blue")
        again.restore_defaults()
        self.assertEqual((again.gap.value(), again.steps.value()), (0, 12))
        self.assertEqual(again.wbase2.currentText(), "polymaker-pla-pro-black")
        self.assertEqual(store.names("calibrate", ("guided",)), [])

    def test_wedge_refused_without_a_slicer_project(self):
        from unittest import mock
        tab = self._tab()
        page = self._page(tab)
        tab.project.set("template", "")
        with mock.patch.object(QMessageBox, "warning") as warn:
            page.write_wedge()
        self.assertIn("project", warn.call_args[0][2])

    def test_reload_after_a_fit_keeps_both_writes(self):
        tab = self._tab()
        ed = tab.ed
        ed.select("polymaker-pla-pro-red")
        ed.e_name.setText("My red")                           # unsaved edit here
        disk = DB(self.path)                                  # meanwhile a fit writes blue
        disk.filaments["polymaker-pla-pro-blue"].td = 0.777
        disk.save()
        tab.reload_db()
        self.assertEqual(ed.db.filaments["polymaker-pla-pro-blue"].td, 0.777)
        self.assertEqual(ed.db.filaments["polymaker-pla-pro-red"].name, "My red")
        self.assertTrue(ed.dirty)
        self.assertTrue(ed.save())
        again = DB(self.path)
        self.assertEqual(again.filaments["polymaker-pla-pro-blue"].td, 0.777)
        self.assertEqual(again.filaments["polymaker-pla-pro-red"].name, "My red")

    def test_guided_fit_saves_to_the_library_and_the_editor_sees_it(self):
        from stackforge.core import colormath as cm
        tab = self._tab()
        page = self._page(tab)
        page.on_saved = lambda _fid: tab.reload_db()
        tab.ed.select("polymaker-pla-pro-red")
        tab.ed.e_name.setText("My red")                      # unsaved edit in the editor
        page.select("polymaker-pla-pro-teal")
        fil = page.fil()
        n = np.arange(1, 9)[:, None]
        for box, base in ((page.wedge_a, [244., 245, 240]), (page.wedge_b, [26., 26, 28])):
            T = np.exp(-(n * 0.12) / 0.3)
            lin = cm.srgb_to_linear(np.array(base)) * T + fil.linear() * (1 - T)
            box.text.setPlainText(",".join(cm.to_hex(c) for c in cm.linear_to_srgb(lin)))
            box.base.setCurrentText(cm.to_hex(base))
        page.do_fit()
        self.assertTrue(page.apply_btn.isEnabled())
        page.apply_fit()
        disk = DB(self.path).filaments["polymaker-pla-pro-teal"]
        self.assertEqual(disk.provenance, "measured")
        self.assertAlmostEqual(disk.td, 0.3, delta=0.02)
        self.assertEqual(tab.ed.db.filaments["polymaker-pla-pro-teal"].provenance, "measured")
        self.assertEqual(tab.ed.db.filaments["polymaker-pla-pro-red"].name, "My red")

    def test_measured_readings_fill_wedge_a_then_b(self):
        tab = self._tab()
        page = self._page(tab)
        self.assertEqual(page.add_measured("#AABBCC,#112233,#445566"), "A")
        self.assertEqual(page.steps.value(), 3)
        self.assertEqual(page.add_measured("#010203,#040506,#070809"), "B")
        self.assertEqual(page.wedge_b.text.toPlainText(), "#010203,#040506,#070809")

    def test_every_page_paints(self):
        ed = self.ed
        ed.resize(1100, 700)
        for name in ("details", "look", "match"):
            ed.show_page(name)
            self.assertFalse(ed.grab().isNull())


if __name__ == "__main__":
    unittest.main()
