"""The Spectra tab (viewer: overlay, layer slider, views, stack builder, readings, export) and the
plaque designer's spectral optics switch. Offscreen; no display needed."""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stackforge.core import spectral as sp  # noqa: E402
from stackforge.core.filamentdb import DB  # noqa: E402
from stackforge.gui import theme  # noqa: E402
from stackforge.gui.spectrum_chart import Axis, Series, SpectrumChart  # noqa: E402
from stackforge.gui.tabs.spectra import SpectraViewer, WATERMARK  # noqa: E402
from stackforge.tools import spectral as tool  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply(app)


class FakeProject:
    def __init__(self, db):
        self.db = db

    def get(self, key):
        return {"db": self.db, "layer_height": "0.08"}.get(key)

    def layers(self):
        return 0.08, 0.2

    def subscribe(self, _cb):
        pass


def pump():
    for _ in range(3):
        app.processEvents()


class Viewer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "f.json")
        db = DB(self.db)
        for f in sp.demo_filaments():
            if f.id in ("demo-orange", "demo-white"):
                db.filaments[f.id] = f
            elif f.id == "demo-red":
                f.spectral = None                       # one uncalibrated filament
                db.filaments[f.id] = f
        db.save()
        self.v = SpectraViewer(FakeProject(self.db))
        self.v.resize(1200, 800)

    def tearDown(self):
        self.v.close()
        self.v.deleteLater()
        pump()
        self.tmp.cleanup()

    def items(self):
        return {self.v.list.item(i).data(Qt.UserRole): self.v.list.item(i) for i in range(self.v.list.count())}

    def tick(self, *ids):
        for fid, it in self.items().items():
            it.setCheckState(Qt.Checked if fid in ids else Qt.Unchecked)
        pump()

    def test_only_calibrated_filaments_can_be_ticked(self):
        it = self.items()
        self.assertTrue(it["demo-orange"].flags() & Qt.ItemIsUserCheckable)
        self.assertFalse(it["demo-red"].flags() & Qt.ItemIsEnabled)
        self.assertIn("no spectral calibration", it["demo-red"].text())

    def test_overlay_and_layer_slider(self):
        self.tick("demo-orange", "demo-white")
        self.assertEqual(len([s for s in self.v.chart.series if not s.dashed]), 2)
        self.v.maxl.setValue(10)
        self.v.slider.setValue(4)
        pump()
        self.assertEqual(self.v.chart.layer, 4)
        self.assertEqual(self.v.strip.current, 4)
        self.assertEqual(len(self.v.strip.rows[0][1]), 11)               # 0..10 layers
        # Layer 0 is the base itself; layer 4 is more orange than layer 1.
        cur = self.v.chart.series[1].curves
        self.assertTrue(np.allclose(cur[0], 0.9))
        self.assertLess(cur[4][5], cur[1][5])                            # 430 nm absorbed

    def test_every_view_paints(self):
        self.tick("demo-orange")
        for k in range(self.v.view.count()):
            self.v.view.setCurrentIndex(k)
            pump()
            self.assertFalse(self.v.chart.grab().isNull())
        self.assertTrue(self.v.chart.axis.log)                           # the K/S view

    def test_stack_builder_draws_a_dashed_stack(self):
        self.tick("demo-orange")
        self.v.stack_box.setChecked(True)
        pump()
        dashed = [s for s in self.v.chart.series if s.dashed]
        self.assertEqual(len(dashed), 1)
        self.assertIn("D65", self.v.lbl_stack.text())
        n = len(self.v.rows)
        self.v.add_row("demo-white", 1)
        self.assertEqual(len(self.v.rows), n + 1)
        self.v.remove_row(self.v.rows[-1])
        self.assertEqual(len(self.v.rows), n)

    def test_demo_is_watermarked_and_never_saved(self):
        before = open(self.db).read()
        self.v.toggle_demo()
        pump()
        self.assertEqual(self.v.chart.watermark, WATERMARK)
        self.assertIn("demo-blue", self.items())
        self.v.toggle_demo()
        pump()
        self.assertEqual(self.v.chart.watermark, "")
        self.assertEqual(open(self.db).read(), before)

    def test_empty_library_explains_how_to_calibrate(self):
        db = DB(self.db)
        for f in db.filaments.values():
            f.spectral = None
        db.save()
        self.v.reload()
        self.assertIn("No spectrally calibrated", self.v.chart.empty)
        self.assertEqual(self.v.chart.series, [])

    def test_readings_overlay_and_export(self):
        path = os.path.join(self.tmp.name, "w.readings.json")
        tool.main(["demo-readings", "-o", path, "--steps", "5"])
        self.tick("demo-orange")
        self.assertTrue(self.v.load_readings(path))
        self.v.slider.setValue(2)
        pump()
        measured = [s for s in self.v.chart.series if s.points is not None]
        self.assertEqual(len(measured), 2)                               # over white and over black
        png = os.path.join(self.tmp.name, "chart.png")
        self.assertTrue(self.v.export_png(png))
        self.assertGreater(os.path.getsize(png), 1000)


class Chart(unittest.TestCase):
    def test_log_axis_with_integer_bounds_paints(self):
        """numpy 2 keeps ceil() of an int integral; 10 ** -3 then raised inside paintEvent and
        took the process down on Windows."""
        c = SpectrumChart()
        c.resize(600, 400)
        c.set_data([], 0, Axis("K/S", -3, 3, log=True))
        self.assertFalse(c.grab().isNull())
        from PySide6.QtGui import QColor
        c.set_data([Series("a", QColor("#ff8800"), [np.full(sp.NB, 0.5)])], 0, Axis("K/S", -3, 3, log=True))
        self.assertFalse(c.grab().isNull())

    def test_a_painting_error_is_drawn_not_raised(self):
        c = SpectrumChart()
        c.resize(300, 200)
        c.set_data([Series("bad", None, [np.full(sp.NB, 0.5)])], 0)     # colour None -> error
        self.assertFalse(c.grab().isNull())


class DesignerOptics(unittest.TestCase):
    def test_spectral_optics_disables_uncalibrated_filaments(self):
        from stackforge.gui.tabs.plaque import PlaqueDesigner
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            db = DB(path)
            for f in sp.demo_filaments():
                if f.id == "demo-red":
                    f.spectral = None
                db.filaments[f.id] = f
            db.save()
            des = PlaqueDesigner(FakeProject(path))
            try:
                des.db_path = path
                des._reload_filaments()
                des.c_optics.setCurrentText("spectral")
                pump()
                rows = {des.list.item(i).data(Qt.UserRole): des.list.item(i) for i in range(des.list.count())}
                self.assertFalse(rows["demo-red"].flags() & Qt.ItemIsEnabled)
                self.assertTrue(rows["demo-blue"].flags() & Qt.ItemIsEnabled)
                des._set_all(True)
                self.assertEqual(rows["demo-red"].checkState(), Qt.Unchecked)
                self.assertEqual(des.config_ns().optics, "spectral")
                self.assertTrue(des.c_illum.isEnabled())
                des.c_optics.setCurrentText("rgb")
                pump()
                rows = {des.list.item(i).data(Qt.UserRole): des.list.item(i) for i in range(des.list.count())}
                self.assertTrue(rows["demo-red"].flags() & Qt.ItemIsEnabled)   # rgb takes any filament
                self.assertFalse(des.c_illum.isEnabled())
            finally:
                des.close()
                des.deleteLater()
                pump()


if __name__ == "__main__":
    unittest.main()
