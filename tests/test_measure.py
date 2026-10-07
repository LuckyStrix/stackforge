"""measure.py against a fake spotread (transcript shape captured from a real
ColorMunki Photo by the calibration-suite project), plus the pure maths.
Hardware tests run only with STACKFORGE_MUNKI=1."""
import os, shutil, stat, tempfile, unittest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from stackforge.tools import calibrate, measure
from stackforge.core import colormath

FAKE = r'''#!/usr/bin/env python3
import os, sys
def say(s): sys.stdout.write(s); sys.stdout.flush()
READY = ("\nPlace instrument on spot to be measured,\nHit ESC or Q to exit, instrument "
         "switch or any other key to take a reading: ")
MODE = os.environ.get("FAKE_MODE", "ok")
WRONG = int(os.environ.get("FAKE_DIAL_WRONG_TRIES", "1"))
try:
    import tty
    if sys.stdin.isatty(): tty.setcbreak(0)
except ImportError: pass
say("\nSpot read needs a calibration before continuing\n")
w = 0
while True:
    say("\nSet instrument sensor to calibration position,\n and then hit any key to continue,\n"
        " or hit Esc or Q to abort: ")
    k = os.read(0, 1)
    if k in (b"q", b""): sys.exit(0)
    if w >= WRONG: break
    w += 1
say("\nCalibration complete\n"); say(READY)
n = 0
while True:
    k = os.read(0, 1)
    if k in (b"q", b"\x1b", b""): sys.exit(0)
    n += 1
    if MODE == "zeros":
        say("\n Result is XYZ: 0.000000 0.000000 0.000000, D50 Lab: 0.0 0.0 0.0\n"); say(READY); continue
    say(f"\n Result is XYZ: {10*n:.6f} {20*n:.6f} {30*n:.6f}, D50 Lab: 1.0 2.0 3.0\n")
    if MODE == "spectrum":
        say("Spectrum from 380.000000 to 730.000000 nm in 4 steps\n 0.1 0.2\n 0.3 0.4\n")
    say(READY)
'''


class FakeSpotread(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        p = os.path.join(self.d, "spotread")
        with open(p, "w") as f:
            f.write(FAKE)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.old = os.environ.get("PATH", "")
        os.environ["PATH"] = self.d + os.pathsep + self.old
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old))

    def test_calibrates_then_reads(self):
        said = []
        with measure.SpotreadSession() as s:
            n = s.prepare(said.append, lambda _: "")
            self.assertEqual(n, 2)  # dial wrong once, human asked again
            self.assertEqual(s.measure()["xyz"], (10, 20, 30))
            self.assertEqual(s.measure()["xyz"], (20, 40, 60))

    def test_dead_reading_refused(self):
        os.environ["FAKE_MODE"] = "zeros"
        self.addCleanup(os.environ.pop, "FAKE_MODE")
        with measure.SpotreadSession() as s:
            s.prepare(lambda _: None, lambda _: "")
            with self.assertRaises(measure.DeadReading):
                s.measure()

    def test_human_can_quit(self):
        with measure.SpotreadSession() as s:
            with self.assertRaises(measure.SessionAborted):
                s.prepare(lambda _: None, lambda _: "q")

    def test_spectrum_parsed(self):
        os.environ["FAKE_MODE"] = "spectrum"
        self.addCleanup(os.environ.pop, "FAKE_MODE")
        with measure.SpotreadSession() as s:
            s.prepare(lambda _: None, lambda _: "")
            sp = s.measure()["spectrum"]
        self.assertEqual(sp["values"], [0.1, 0.2, 0.3, 0.4])
        self.assertEqual((sp["nm_from"], sp["nm_to"]), (380.0, 730.0))


    def _wrapper(self):
        """A fake argyll-nospos: marks the environment and runs the command, as the real one does."""
        p = os.path.join(self.d, "argyll-nospos")
        with open(p, "w") as f:
            f.write('#!/bin/sh\nexport ARGYLL_NOSPOS=1\nexec "$@"\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def test_nospos_runs_through_wrapper(self):
        wrapper = self._wrapper()
        asked = []
        with measure.SpotreadSession(nospos=True) as s:
            s.prepare(lambda _: None, lambda q: asked.append(q) or "")
            self.assertEqual(s.command, [wrapper, "spotread"])
            self.assertEqual(s.measure()["xyz"], (10, 20, 30))
        self.assertEqual(asked[-1], measure.NOSPOS_DIAL_BACK)  # told to turn the dial back

    def test_nospos_implied_under_wrapper(self):
        os.environ["ARGYLL_NOSPOS"] = "1"
        self.addCleanup(os.environ.pop, "ARGYLL_NOSPOS")
        said = []
        with measure.SpotreadSession() as s:
            s.prepare(said.append, lambda _: "")
            self.assertTrue(s.nospos)
            self.assertEqual(len(s.command), 1)   # PATH already has the patched build
        self.assertIn(measure.NOSPOS_NOTE, said)

    def test_system_build_unchanged(self):
        asked = []
        with measure.SpotreadSession() as s:
            s.prepare(lambda _: None, lambda q: asked.append(q) or "")
            self.assertFalse(s.nospos)
        self.assertNotIn(measure.NOSPOS_DIAL_BACK, asked)


class Guards(unittest.TestCase):
    def test_white_paper_accepted(self):
        measure.check_white({"xyz": (88.0, 91.0, 75.0)})

    def test_off_position_calibration_refused(self):
        for y in (40.0, 180.0):
            with self.assertRaises(measure.MeasureError):
                measure.check_white({"xyz": (y, y, y)})

    def test_same_reading_flagged(self):
        a = {"xyz": (40.0, 42.0, 35.0)}
        self.assertIsNotNone(measure.same_reading(a, {"xyz": (40.02, 42.01, 35.0)}))
        self.assertIsNone(measure.same_reading(a, {"xyz": (30.0, 32.0, 30.0)}))

    def test_lab_white(self):
        self.assertTrue(np.allclose(measure.xyz_d50_to_lab([96.422, 100, 82.521]), [100, 0, 0], atol=1e-3))

    def test_wedge_reversals(self):
        self.assertEqual(measure.wedge_reversals([90, 80, 72, 65, 60]), [])
        self.assertEqual(measure.wedge_reversals([20, 30, 38, 45]), [])          # light over black
        self.assertEqual(measure.wedge_reversals([90, 80, 95, 65, 60]), [3])     # step 3 misread
        self.assertEqual(measure.wedge_reversals([60, 60.5, 60, 60.4]), [])      # flat: no trend


class Maths(unittest.TestCase):
    def test_d50_white_is_white(self):
        rgb = measure.xyz_d50_to_srgb([96.42, 100.0, 82.49])
        self.assertTrue(np.allclose(rgb, 255, atol=2), rgb)

    def test_td_recovered_from_transmittance(self):
        t = np.array([0.2, 0.4, 0.6, 0.8, 1.0])
        td = np.array([0.3, 0.5, 0.9])
        T = np.exp(-t[:, None] / td[None, :])
        got, rms = measure.td_from_transmittance(t, T)
        self.assertTrue(np.allclose(got, td, rtol=1e-6), got)

    def test_td_ignores_saturated_chips(self):
        t = np.array([0.2, 0.4, 0.6, 3.0])
        T = np.exp(-t[:, None] / 0.5) * np.ones((1, 3))
        T[3] = 0.001  # meter floor, not a real transmittance
        got, _ = measure.td_from_transmittance(t, T)
        self.assertTrue(np.allclose(got, 0.5, rtol=0.05), got)

    def test_wedge_fit_roundtrip(self):
        """Simulated wedge at known td/colour -> calibrate.fit_td recovers td."""
        layer_h, td_true = 0.08, np.array([0.35, 0.35, 0.35])
        col = np.array([0.05, 0.4, 0.45])
        depth = (np.arange(10) + 1) * layer_h
        sets = []
        for base in (np.array([244, 245, 240.0]), np.array([25, 25, 25.0])):
            b = colormath.srgb_to_linear(base)
            T = np.exp(-depth[:, None] / td_true)
            lin = b * T + col * (1 - T)
            sets.append((colormath.linear_to_srgb(lin), base))
        td, *_ = calibrate.fit_td(sets, layer_h)
        self.assertAlmostEqual(float(td[0]), 0.35, delta=0.03)


class Calibrate(unittest.TestCase):
    def test_wedge_step_heights(self):
        plate, decals, w, base_h = calibrate.build_wedge(4, 0.1, 5, 10.0, 14.0, 0.0, rows=2)
        self.assertAlmostEqual(base_h, 0.5)
        self.assertEqual(len(decals), 2)
        for ext, v, t in decals:
            self.assertAlmostEqual(v[:, 2].max(), 0.5 + 4 * 0.1)   # deepest step = 4 layers

    def test_hinged_wedge_thins_only_the_gaps(self):
        plate, decals, w, base_h = calibrate.build_wedge(3, 0.1, 8, 10.0, 14.0, 6.0, hinge_layers=3)
        self.assertAlmostEqual(base_h, 0.8)
        self.assertAlmostEqual(w, 3 * 10.0 + 2 * 6.0)
        v = plate.verts
        self.assertAlmostEqual(v[:, 2].max(), 0.8)
        # the gap between step 1 and step 2 (x 10..16) is only the hinge tall
        gap_pts = v[(v[:, 0] > 10.0) & (v[:, 0] < 16.0)]
        self.assertEqual(len(gap_pts), 0)          # no vertex strictly inside the gap
        strip = v[v[:, 2] <= 0.3 + 1e-9]
        self.assertAlmostEqual(strip[:, 0].max(), w)
        # steps still start at the full base height
        for ext, dv, t in decals:
            self.assertAlmostEqual(dv[:, 2].min(), 0.8)

    def test_hinge_needs_a_gap_and_fewer_layers_than_the_base(self):
        with self.assertRaises(ValueError):
            calibrate.build_wedge(3, 0.1, 8, 10.0, 14.0, 0.0, hinge_layers=3)
        with self.assertRaises(ValueError):
            calibrate.build_wedge(3, 0.1, 8, 10.0, 14.0, 6.0, hinge_layers=8)

    def test_chips_are_standalone_and_stepped(self):
        chips = calibrate.build_chips(3, 0.08, 20.0, 20.0, 2.0)
        tops = [c.verts[:, 2].max() for c in chips]
        self.assertTrue(np.allclose(tops, [0.08, 0.16, 0.24]))
        self.assertTrue(all(c.verts[:, 2].min() == 0 for c in chips))


@unittest.skipUnless(os.environ.get("STACKFORGE_MUNKI") == "1", "needs a ColorMunki; set STACKFORGE_MUNKI=1")
class Hardware(unittest.TestCase):
    def test_instrument_reads_something_nonzero(self):
        with measure.SpotreadSession(measure.REFLECT_ARGS) as s:
            s.prepare()
            input("Place the meter on white paper, press Enter ")
            self.assertGreater(s.measure()["xyz"][1], 50)


if __name__ == "__main__":
    unittest.main()


class FirstLayerGridTests(unittest.TestCase):
    def test_wedge_sits_on_the_slicer_grid(self):
        plate, decals, w, base_h = calibrate.build_wedge(4, 0.08, 5, 14.0, 14.0, 0.0, first_layer_h=0.25)
        self.assertAlmostEqual(base_h, 0.25 + 4 * 0.08)
        for ext, v, t in decals:
            self.assertAlmostEqual(v[:, 2].min(), base_h)
            self.assertAlmostEqual(v[:, 2].max(), base_h + 4 * 0.08)

    def test_chips_follow_first_layer(self):
        chips = calibrate.build_chips(3, 0.08, 20.0, 20.0, 2.0, first_layer_h=0.25)
        self.assertTrue(np.allclose([c.verts[:, 2].max() for c in chips], [0.25, 0.33, 0.41]))

    def test_fit_uses_first_layer_depth(self):
        from stackforge.core import colormath as cm
        w, col = np.array([244., 245, 240]), np.array([40., 150, 148])
        depth = 0.25 + np.arange(8) * 0.08
        T = np.exp(-depth / 0.3)[:, None]
        meas = cm.linear_to_srgb(cm.srgb_to_linear(w) * T + cm.srgb_to_linear(col) * (1 - T))
        td, *_ = calibrate.fit_td([(meas, w)], 0.08, first_layer_h=0.25)
        self.assertAlmostEqual(float(td[0]), 0.3, places=2)
        td_bad, *_ = calibrate.fit_td([(meas, w)], 0.08)
        self.assertGreater(abs(float(td_bad[0]) - 0.3), 0.03)   # grid ignored: visibly wrong
