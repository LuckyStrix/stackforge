"""munki.py against a fake spotread (transcript shape captured from a real
ColorMunki Photo by the calibration-suite project), plus the pure maths.
Hardware tests run only with STACKFORGE_MUNKI=1."""
import os, stat, sys, tempfile, unittest
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import calibrate, munki, tdcolor

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
        p = os.path.join(self.d, "spotread")
        with open(p, "w") as f:
            f.write(FAKE)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.old = os.environ.get("PATH", "")
        os.environ["PATH"] = self.d + os.pathsep + self.old
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.old))

    def test_calibrates_then_reads(self):
        said = []
        with munki.SpotreadSession() as s:
            n = s.prepare(said.append, lambda _: "")
            self.assertEqual(n, 2)  # dial wrong once, human asked again
            self.assertEqual(s.measure()["xyz"], (10, 20, 30))
            self.assertEqual(s.measure()["xyz"], (20, 40, 60))

    def test_dead_reading_refused(self):
        os.environ["FAKE_MODE"] = "zeros"
        self.addCleanup(os.environ.pop, "FAKE_MODE")
        with munki.SpotreadSession() as s:
            s.prepare(lambda _: None, lambda _: "")
            with self.assertRaises(munki.DeadReading):
                s.measure()

    def test_human_can_quit(self):
        with munki.SpotreadSession() as s:
            with self.assertRaises(munki.SessionAborted):
                s.prepare(lambda _: None, lambda _: "q")

    def test_spectrum_parsed(self):
        os.environ["FAKE_MODE"] = "spectrum"
        self.addCleanup(os.environ.pop, "FAKE_MODE")
        with munki.SpotreadSession() as s:
            s.prepare(lambda _: None, lambda _: "")
            sp = s.measure()["spectrum"]
        self.assertEqual(sp["values"], [0.1, 0.2, 0.3, 0.4])
        self.assertEqual((sp["nm_from"], sp["nm_to"]), (380.0, 730.0))


class Maths(unittest.TestCase):
    def test_d50_white_is_white(self):
        rgb = munki.xyz_d50_to_srgb([96.42, 100.0, 82.49])
        self.assertTrue(np.allclose(rgb, 255, atol=2), rgb)

    def test_td_recovered_from_transmittance(self):
        t = np.array([0.2, 0.4, 0.6, 0.8, 1.0])
        td = np.array([0.3, 0.5, 0.9])
        T = np.exp(-t[:, None] / td[None, :])
        got, rms = munki.td_from_transmittance(t, T)
        self.assertTrue(np.allclose(got, td, rtol=1e-6), got)

    def test_td_ignores_saturated_chips(self):
        t = np.array([0.2, 0.4, 0.6, 3.0])
        T = np.exp(-t[:, None] / 0.5) * np.ones((1, 3))
        T[3] = 0.001  # meter floor, not a real transmittance
        got, _ = munki.td_from_transmittance(t, T)
        self.assertTrue(np.allclose(got, 0.5, rtol=0.05), got)

    def test_wedge_fit_roundtrip(self):
        """Simulated wedge at known td/colour -> calibrate.fit_td recovers td."""
        layer_h, td_true = 0.08, np.array([0.35, 0.35, 0.35])
        col = np.array([0.05, 0.4, 0.45])
        depth = (np.arange(10) + 1) * layer_h
        sets = []
        for base in (np.array([244, 245, 240.0]), np.array([25, 25, 25.0])):
            b = tdcolor.srgb_to_linear(base)
            T = np.exp(-depth[:, None] / td_true)
            lin = b * T + col * (1 - T)
            sets.append((tdcolor.linear_to_srgb(lin), base))
        td, *_ = calibrate.fit_td(sets, layer_h)
        self.assertAlmostEqual(float(td[0]), 0.35, delta=0.03)


@unittest.skipUnless(os.environ.get("STACKFORGE_MUNKI") == "1", "needs a ColorMunki; set STACKFORGE_MUNKI=1")
class Hardware(unittest.TestCase):
    def test_instrument_reads_something_nonzero(self):
        with munki.SpotreadSession(munki.REFLECT_ARGS) as s:
            s.prepare()
            input("Place the meter on white paper, press Enter ")
            self.assertGreater(s.measure()["xyz"][1], 50)


if __name__ == "__main__":
    unittest.main()
