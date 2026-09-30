"""Regressions for stackforge CLI options and the helpers it shares with the GUI."""
import os, subprocess, sys, tempfile, unittest, zipfile

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import stackforge as sf
import tdcolor
from filamentdb import DB

IDS = [f"polymaker-pla-pro-{c}" for c in ("black", "blue", "red", "yellow", "white")]


def run(*extra, img):
    return subprocess.run(
        [sys.executable, "stackforge.py", img, "--width", "6", "--max-layers", "3",
         "--base-layers", "3", *extra],
        cwd=ROOT, capture_output=True, text=True)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.img = os.path.join(self.d, "in.png")
        Image.linear_gradient("L").resize((24, 16)).convert("RGB").save(self.img)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_rank_keeps_a_base_listed_last(self):
        out = os.path.join(self.d, "out.3mf")
        r = run("--filaments", ",".join(IDS), "--base", "polymaker-pla-pro-white",
                "--no-rank", "--slots", "4", "-o", out, img=self.img)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("using Black, Blue, Red, White", r.stdout)
        # The plate itself is printed with the base's extruder: T4 here.
        with zipfile.ZipFile(out) as z:
            cfg = z.read("Metadata/model_settings.config").decode()
        self.assertIn('key="extruder" value="4"', cfg)

    def test_prusa_flavor_writes(self):
        out = os.path.join(self.d, "out.3mf")
        r = run("--filaments", ",".join(IDS[:4]), "--flavor", "prusa", "-o", out, img=self.img)
        self.assertEqual(r.returncode, 0, r.stderr)
        with zipfile.ZipFile(out) as z:
            self.assertIn("Metadata/Slic3r_PE_model.config", z.namelist())

    def test_slots_below_two_is_refused(self):
        r = run("--filaments", ",".join(IDS), "--slots", "1", img=self.img)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--slots", r.stderr)


class Gamut(unittest.TestCase):
    def test_base_index_refuses_a_missing_base(self):
        db = DB(os.path.join(ROOT, "filaments.json"))
        fils = db.resolve(",".join(IDS[:3]))
        g = sf.Gamut(fils, db.get(IDS[4]), 0.08, 2, verbose=False)
        with self.assertRaises(ValueError):
            g.base_index

    def test_dedup_keeps_darks(self):
        # Linear-light dedup merged everything from L* 0 to ~5 into one cell,
        # so dark colours a finer grid can reach were lost (p99 ~2.6 dE).
        db = DB(os.path.join(ROOT, "filaments.json"))
        fils = db.resolve(",".join([IDS[4]] + IDS[:3]))
        ref = sf.Gamut(fils, fils[0], 0.08, 8, grid=1024, cap=10**7, verbose=False)
        g = sf.Gamut(fils, fils[0], 0.08, 8, verbose=False)
        dark = ref.lab[ref.lab[:, 0] < 25]
        self.assertGreater(len(dark), 100)
        self.assertLess(np.percentile(g.tree.query(dark)[0], 99), 1.0)


class OpenImage(unittest.TestCase):
    def test_exif_orientation_and_alpha(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rot.png")
            im = Image.new("RGBA", (30, 10), (0, 0, 0, 0))
            exif = Image.Exif()
            exif[0x0112] = 6            # stored sideways: rotate 90 CW to view
            im.save(p, exif=exif)
            got = tdcolor.open_image(p)
            self.assertEqual(got.size, (10, 30))
            self.assertEqual(got.mode, "RGB")
            self.assertEqual(got.getpixel((0, 0)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
