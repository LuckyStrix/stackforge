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
            cfg = z.read("Metadata/Slic3r_PE_model.config").decode()
        # No Prusa profile is carried over, so these must be per-object.
        self.assertIn('type="object" key="fill_density" value="100%"', cfg)
        self.assertIn('type="object" key="layer_height" value="0.08"', cfg)

    def test_nonsense_sizes_are_refused(self):
        for flag, val in (("--base-layers", "0"), ("--resolution", "0"),
                          ("--layer-height", "-0.1"), ("--max-layers", "0")):
            r = run("--filaments", ",".join(IDS[:4]), flag, val,
                    "-o", os.path.join(self.d, "o.3mf"), img=self.img)
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn(flag, r.stderr)

    def test_rank_and_no_rank_conflict(self):
        r = run("--filaments", ",".join(IDS), "--rank", "--no-rank", img=self.img)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not allowed with", r.stderr)

    def test_blurred_error_is_reported(self):
        r = run("--filaments", ",".join(IDS[:4]), "--dither", "blue",
                "-o", os.path.join(self.d, "o.3mf"), img=self.img)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("blurred dE", r.stdout)

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


class Trim(unittest.TestCase):
    def test_drops_uniform_base_layers_but_keeps_one(self):
        labels = np.zeros((4, 2, 2), np.int16)
        labels[2, 0, 0] = 1
        got, n = sf.trim_base_layers(labels, 0)
        self.assertEqual(n, 2)
        self.assertEqual(got[0, 0, 0], 1)
        got, n = sf.trim_base_layers(np.zeros((3, 2, 2), np.int16), 0)
        self.assertEqual((n, len(got)), (2, 1))


class ContactSheet(unittest.TestCase):
    def test_keeps_aspect(self):
        # A 3:1 target used to be squashed into a square thumbnail.
        tgt = np.zeros((10, 30, 3), np.uint8)
        tgt[:, :15] = 255
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sheet.png")
            sf.contact_sheet(p, [], tgt, cols=1, thumb=90, pad=0)
            sheet = np.asarray(Image.open(p).convert("L"))
        self.assertEqual(sheet.shape[1], 90)
        self.assertGreater(sheet[15, 10], 200)      # inside the 90x30 thumbnail
        self.assertLess(sheet[15, 80], 60)
        self.assertLess(abs(int(sheet[40, 10]) - 28), 5)   # below it: background


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
