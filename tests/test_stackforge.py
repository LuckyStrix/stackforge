"""Regressions for stackforge CLI options and the helpers it shares with the GUI."""
import os, subprocess, sys, tempfile, unittest, zipfile

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from tdforge.tools import stackforge as sf
from tdforge.core import tdcolor
from tdforge.core.filamentdb import DB
from tdforge.core.paths import packaged

IDS = [f"polymaker-pla-pro-{c}" for c in ("black", "blue", "red", "yellow", "white")]


def run(*extra, img):
    return subprocess.run(
        [sys.executable, "-m", "tdforge.tools.stackforge", img, "--width", "6", "--max-layers", "3",
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
        # The plate itself is printed with the base's extruder: T4 here, and
        # the colour modifiers are the other three.
        with zipfile.ZipFile(out) as z:
            cfg = z.read("Metadata/model_settings.config").decode()
        import re
        parts = re.findall(r'subtype="(\w+)">.*?key="extruder" value="(\d+)"', cfg, re.S)
        self.assertEqual([e for st, e in parts if st == "normal_part"], ["4"])
        self.assertTrue({e for st, e in parts if st == "modifier_part"} <= {"1", "2", "3"})

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
        import argparse
        base = dict(slots=4, width=6, resolution=0.6, layer_height=None,
                    first_layer_height=None, max_layers=3, base_layers=3, grid=192,
                    rank_samples=100, top=5)
        self.assertEqual(sf.check_args(argparse.Namespace(**base)), [])
        for key, val in (("base_layers", 0), ("resolution", 0), ("layer_height", -0.1),
                         ("max_layers", 0), ("top", -1), ("slots", 1)):
            probs = sf.check_args(argparse.Namespace(**dict(base, **{key: val})))
            self.assertTrue(any(key.replace("_", "-") in p for p in probs), key)

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
        db = DB(packaged("filaments.json"))
        fils = db.resolve(",".join(IDS[:3]))
        g = sf.Gamut(fils, db.get(IDS[4]), 0.08, 2, verbose=False)
        with self.assertRaises(ValueError):
            g.base_index

    def test_dedup_keeps_darks(self):
        # Linear-light dedup merged everything from L* 0 to ~5 into one cell,
        # so dark colours a finer grid can reach were lost (p99 ~2.6 dE).
        db = DB(packaged("filaments.json"))
        fils = db.resolve(",".join([IDS[4]] + IDS[:3]))
        ref = sf.Gamut(fils, fils[0], 0.08, 8, grid=1024, cap=10**7, verbose=False)
        g = sf.Gamut(fils, fils[0], 0.08, 8, verbose=False)
        dark = ref.lab[ref.lab[:, 0] < 25]
        self.assertGreater(len(dark), 100)
        self.assertLess(np.percentile(g.tree.query(dark)[0], 99), 1.0)


class Model(unittest.TestCase):
    def setUp(self):
        self.db = DB(packaged("filaments.json"))

    def test_stack_replays_to_its_colour(self):
        fils = self.db.resolve(",".join([IDS[4]] + IDS[:3]))
        g = sf.Gamut(fils, fils[0], 0.08, 6, verbose=False)
        for i in range(0, len(g.colors), max(1, len(g.colors) // 300)):
            c = fils[0].linear()
            for f in g.stack(i):
                c = c * g.trans[f] + g.cols[f] * (1 - g.trans[f])
            np.testing.assert_allclose(c, g.colors[i], atol=1e-12)

    def test_translucent_filament_keeps_accumulating(self):
        # One layer of it moved the colour less than a dedup cell, so it was
        # dropped and 16 layers of it were unreachable (dE 3.5).
        from tdforge.core.filamentdb import Filament
        base = Filament(id="w", color="#F4F5F0", td=0.13)
        nat = Filament(id="n", color="#E8E2D2", td=1.67)
        g = sf.Gamut([base, nat], base, 0.08, 16, verbose=False)
        c = base.linear()
        for _ in range(16):
            t = nat.transmittance(0.08)
            c = c * t + nat.linear() * (1 - t)
        self.assertLess(g.tree.query(tdcolor.linear_to_lab(c))[0], 0.5)

    def test_colour_histogram_is_exact_and_binned(self):
        img = np.array([[[1, 2, 3], [1, 2, 3], [9, 9, 9]]], np.uint8)
        rgb, n = sf.colour_histogram(img, 10)
        self.assertEqual(sorted(zip(map(tuple, rgb.astype(int)), n)),
                         [((1, 2, 3), 2.0), ((9, 9, 9), 1.0)])
        rng = np.random.default_rng(0)
        big = rng.integers(0, 256, (50, 50, 3)).astype(np.uint8)
        rgb, n = sf.colour_histogram(big, 500)
        self.assertLessEqual(len(rgb), 500)
        self.assertEqual(n.sum(), 2500)

    def test_blue_dither_helps_a_shallow_stack(self):
        fils = self.db.resolve(",".join([IDS[4]] + IDS[:3]))
        g = sf.Gamut(fils, fils[0], 0.08, 2, verbose=False)
        img = tdcolor.fit_image(os.path.join(ROOT, "docs", "halftone_target.png"), 80, 60, "cover")
        de = {m: tdcolor.blurred_de(np.round(g.srgb()[sf.solve_image(g, img, m)]), img)[0]
              for m in ("none", "blue")}
        self.assertLess(de["blue"], de["none"] - 0.5)

    def test_thin_fraction(self):
        lab = np.zeros((1, 5, 5), np.int16)
        lab[0, 2, :] = 1                     # a one-pixel-high stripe
        self.assertEqual(sf.thin_fraction(lab, 0), 1.0)
        lab[0, 1:4, :] = 1                   # three pixels high: interior row is not thin
        self.assertLess(sf.thin_fraction(lab, 0), 1.0)


class Db(unittest.TestCase):
    def test_bad_entries_are_named(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"id": "x", "color": "notahex"}, {"color": "#fff"},
                        {"id": "y", "color": "#fff", "td": "thick"}):
                p = os.path.join(d, "db.json")
                with open(p, "w") as f:
                    json.dump({"version": 1, "filaments": [bad]}, f)
                with self.assertRaises(SystemExit):
                    DB(p)

    def test_nan_td_rgb_is_refused(self):
        from tdforge.core.filamentdb import Filament
        f = Filament(id="x", color="#ffffff", td_rgb=[0.3, None, 0.2])
        with self.assertRaises(SystemExit):
            f.td_vec()


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

    def test_partial_alpha_blends_toward_pad(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(p)
            got = tdcolor.fit_image(p, 2, 2, pad=(0, 0, 255))[0, 0]
            self.assertTrue(abs(int(got[0]) - 128) <= 1 and abs(int(got[2]) - 127) <= 1, got)

    def test_sixteen_bit_and_key_colour(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g16.png")
            Image.fromarray(np.full((2, 2), 0x8000, np.uint16)).save(p)
            self.assertEqual(tdcolor.open_image(p).getpixel((0, 0)), (128, 128, 128))
            p = os.path.join(d, "key.png")
            Image.new("RGB", (2, 2), (1, 2, 3)).save(p, transparency=(1, 2, 3))
            self.assertEqual(tdcolor.open_image(p, (9, 9, 9)).getpixel((0, 0)), (9, 9, 9))


if __name__ == "__main__":
    unittest.main()
