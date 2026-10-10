"""Spectral optics: CIE colorimetry, the Kubelka-Munk layer model, the K/S fit from wedge
spectra, the spectral plaque gamut, and the calibrated-only rule. No display needed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

import numpy as np
from PIL import Image

from stackforge.core import colormath, wedgesheet
from stackforge.core import spectral as sp
from stackforge.core.filamentdb import DB, Filament
from stackforge.tools import plaque
from stackforge.tools import spectral as tool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def demo(fid):
    return {f.id: f for f in sp.demo_filaments()}[fid]


def demo_db(path, ids=None, with_spectral=True):
    db = DB(path)
    for f in sp.demo_filaments():
        if ids is None or f.id in ids:
            if not with_spectral:
                f.spectral = None
            db.filaments[f.id] = f
    db.save()
    return db


class Colorimetry(unittest.TestCase):
    def test_white_points_match_cie(self):
        # CIE 015 chromaticities of the illuminants (2 degree observer).
        for il, xy in (("D65", (0.3127, 0.3290)), ("D50", (0.3457, 0.3585)), ("A", (0.4476, 0.4074))):
            w = sp.white_xyz(il)
            self.assertAlmostEqual(w[1], 1.0, places=9)
            got = w[:2] / w.sum()
            self.assertLess(np.abs(got - xy).max(), 1e-3, (il, got))

    def test_perfect_reflector_is_white_under_every_light(self):
        for il in sp.ILLUMINANTS:
            self.assertTrue(np.allclose(sp.spectrum_to_linear(np.ones(sp.NB), il), 1.0, atol=1e-9))

    def test_flat_grey_is_neutral(self):
        lin = sp.spectrum_to_linear(np.full(sp.NB, 0.18))
        self.assertTrue(np.allclose(lin, 0.18, atol=1e-6))
        self.assertLess(np.abs(sp.spectrum_to_lab(np.full(sp.NB, 0.18), "D50")[1:]).max(), 1e-6)

    def test_data_files_are_the_cie_originals(self):
        import hashlib
        for name, md5 in (("CIE_xyz_1931_2deg.csv", "17cca777db64b17170f06f67ce9d3ab7"),
                          ("CIE_std_illum_D65.csv", "03d4eb9b837c60671627c946fb534deb"),
                          ("CIE_std_illum_D50.csv", "e72757c3078b58e78ba63051be4b27b0")):
            with open(sp.packaged(f"cie/{name}"), "rb") as fh:
                self.assertEqual(hashlib.md5(fh.read()).hexdigest(), md5, name)

    def test_wavelength_band_is_displayable(self):
        c = sp.wavelength_srgb([450, 550, 650])
        self.assertEqual(c.shape, (3, 3))
        self.assertGreater(c[0, 2], c[0, 0])      # 450 nm is blue
        self.assertGreater(c[1, 1], c[1, 2])      # 550 nm is green
        self.assertGreater(c[2, 0], c[2, 2])      # 650 nm is red


class KubelkaMunk(unittest.TestCase):
    K, S = np.full(sp.NB, 2.0), np.full(sp.NB, 10.0)

    def test_thin_layer_is_the_background(self):
        self.assertTrue(np.allclose(sp.layer(0.3, self.K, self.S, 1e-9), 0.3, atol=1e-6))

    def test_thick_layer_is_r_inf(self):
        self.assertTrue(np.allclose(sp.layer(0.3, self.K, self.S, 50.0), sp.r_inf(self.K, self.S)))

    def test_pure_absorber_limit(self):
        got = sp.layer(0.5, 3.0, 1e-12, 0.2)
        self.assertAlmostEqual(float(got), 0.5 * np.exp(-2 * 3.0 * 0.2), places=6)
        self.assertAlmostEqual(float(sp.transmittance(3.0, 1e-12, 0.2)), np.exp(-0.6), places=6)

    def test_pure_scatterer_limit(self):
        self.assertAlmostEqual(float(sp.layer(0.0, 0.0, 10.0, 0.2)), 2 / 3, places=9)
        self.assertAlmostEqual(float(sp.layer(0.0, 1e-12, 10.0, 0.2)), 2 / 3, places=6)

    def test_two_half_layers_are_one_layer(self):
        """The layer-combination rule is exact for KM: the stack state can be a spectrum."""
        Rg = np.linspace(0.05, 0.9, sp.NB)
        K, S = np.linspace(0.1, 30, sp.NB), np.linspace(1, 20, sp.NB)
        whole = sp.layer(Rg, K, S, 0.24)
        halves = sp.layer(sp.layer(Rg, K, S, 0.1), K, S, 0.14)
        self.assertTrue(np.allclose(whole, halves, atol=1e-12))

    def test_ks_ratio_inverts_r_inf(self):
        R = np.linspace(0.02, 0.95, 20)
        self.assertTrue(np.allclose(sp.r_inf(sp.ks_ratio(R) * 5.0, 5.0), R))

    def test_a_coloured_layer_filters_what_is_under_it(self):
        """Orange over blue goes dark, not purple: orange stops the blue light."""
        o, b, w = demo("demo-orange"), demo("demo-blue"), demo("demo-white")
        Rw = sp.r_inf(*sp.ks(w))
        blue = sp.stack(Rw, [(*sp.ks(b), 0.24)])
        both = sp.stack(blue, [(*sp.ks(o), 0.08)])
        i450, i650 = int((450 - 380) / 10), int((650 - 380) / 10)
        self.assertLess(both[i450], 0.5 * blue[i450])                  # orange ate the blue
        orange_alone = sp.stack(Rw, [(*sp.ks(o), 0.08)])
        self.assertLess(both[i650], orange_alone[i650])                # blue under it ate red
        lab = colormath.linear_to_lab(np.clip(sp.spectrum_to_linear(both), 0, 1))
        self.assertLess(lab[0], 60)                                   # dark
        self.assertGreater(lab[1], 0)                                 # warm, not purple-blue
        self.assertGreater(lab[2], 0)


class Records(unittest.TestCase):
    def test_validation(self):
        f = Filament(id="x")
        self.assertFalse(sp.is_calibrated(f))
        f.spectral = {"K": [1] * 5, "S": [1] * 5}
        self.assertFalse(sp.is_calibrated(f))
        f.spectral = sp.record(np.ones(sp.NB), np.ones(sp.NB))
        self.assertTrue(sp.is_calibrated(f))
        f.spectral["S"][3] = 0.0
        self.assertFalse(sp.is_calibrated(f))

    def test_require_calibrated_names_the_missing(self):
        with self.assertRaises(SystemExit) as cm:
            sp.require_calibrated([demo("demo-white"), Filament(id="plain-red", name="Red")])
        self.assertIn("plain-red", str(cm.exception))
        self.assertNotIn("demo-white", str(cm.exception))

    def test_spectral_block_survives_the_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.json")
            demo_db(path, ["demo-orange"])
            again = DB(path).filaments["demo-orange"]
            self.assertTrue(np.allclose(sp.ks(again)[0], sp.ks(demo("demo-orange"))[0], rtol=1e-5))


class Readings(unittest.TestCase):
    def reading(self, values, nm=(380, 730)):
        return {"spectrum": {"nm_from": nm[0], "nm_to": nm[1], "values": list(values)}}

    def test_scale_is_decided_over_the_whole_set(self):
        dark = self.reading(np.full(36, 0.8))            # 0.8 % on a 0..100 scale
        white = self.reading(np.full(36, 88.0))
        scale = sp.spectrum_scale([dark, white])
        self.assertEqual(scale, 100.0)
        self.assertAlmostEqual(float(sp.reading_spectrum(dark, scale)[0]), 0.008)
        self.assertEqual(sp.spectrum_scale([self.reading(np.full(36, 0.5))]), 1.0)
        # spotread documents 0..100: that is silent, 0..1 is called out.
        self.assertIsNone(sp.scale_note(100.0))
        self.assertIn("0..100", sp.scale_note(1.0))

    def test_resampled_onto_the_grid(self):
        r = self.reading(np.linspace(0, 100, 351), nm=(380, 730))     # 1 nm data
        R = sp.reading_spectrum(r, 100.0)
        self.assertEqual(R.shape, (sp.NB,))
        self.assertAlmostEqual(float(R[1]), 10 / 350, places=6)      # 390 nm

    def test_refused(self):
        no_range = {"spectrum": {"values": [50.0] * 36}}
        not_a_dict = {"spectrum": [50.0] * 36}
        for bad in ({}, self.reading([0.0] * 36), self.reading([1] * 36, nm=(400, 700)),
                    no_range, not_a_dict):
            with self.assertRaises(sp.SpectralError):
                sp.reading_spectrum(bad, 1.0)


class Fit(unittest.TestCase):
    def test_round_trip_recovers_k_and_s(self):
        data = tool.demo_readings("demo-orange", steps=10, noise=0.002, seed=1)
        ds, scale, warnings = tool.datasets_from_readings(data, "demo-orange")
        self.assertEqual(scale, 100.0)
        self.assertEqual(warnings, [])
        K, S, _ = tool.fit_ks(ds, 0.08)
        Kt, St = sp.ks(demo("demo-orange"))
        # Where light gets back out (R_inf > 0.3) both are well determined.
        ok = sp.r_inf(Kt, St) > 0.3
        self.assertLess(np.median(np.abs(np.log(K[ok] / Kt[ok]))), 0.25)
        self.assertLess(np.median(np.abs(np.log(S[ok] / St[ok]))), 0.25)
        self.assertLess(np.abs(sp.r_inf(K, S) - sp.r_inf(Kt, St)).max(), 0.03)
        self.assertLess(np.concatenate(tool.step_de(K, S, ds, 0.08)).mean(), 1.0)

    def test_held_out_is_reported(self):
        data = tool.demo_readings("demo-blue", steps=5, noise=0.002, seed=2)
        ds, _, _ = tool.datasets_from_readings(data, "demo-blue")
        _, _, x = tool.fit_ks(ds, 0.08)
        ho = np.concatenate(tool.held_out_de(ds, 0.08, tool.SMOOTH, x))
        self.assertEqual(len(ho), 10)
        self.assertTrue(np.isfinite(ho).all())

    def test_wrong_filament_is_not_silently_swapped(self):
        data = tool.demo_readings("demo-orange", steps=4)
        with self.assertRaises(SystemExit) as cm:
            tool.datasets_from_readings(data, "demo-blue")
        self.assertIn("demo-orange", str(cm.exception))


class FitCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.db = os.path.join(self.d, "f.json")
        demo_db(self.db, ["demo-orange", "demo-white", "demo-black"], with_spectral=False)
        self.readings = os.path.join(self.d, "w.readings.json")
        self.assertEqual(tool.main(["demo-readings", "-o", self.readings, "--steps", "6"]), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fit_writes_a_spectral_block_and_leaves_rgb_alone(self):
        before = DB(self.db).filaments["demo-orange"]
        png = os.path.join(self.d, "fit.png")
        rc = tool.main(["fit", "--readings", self.readings, "--filament", "demo-orange", "--db", self.db,
                        "--no-holdout", "--write", "--preview", png])
        self.assertEqual(rc, 0)
        after = DB(self.db).filaments["demo-orange"]
        self.assertTrue(sp.is_calibrated(after))
        self.assertTrue(after.spectral["synthetic"])
        self.assertEqual(after.spectral["bases"], ["demo-white", "demo-black"])
        # `color` is an RGB-model input too: a spectral fit must not move RGB plaques.
        self.assertEqual((after.color, after.td, after.td_rgb, after.provenance),
                         (before.color, before.td, before.td_rgb, before.provenance))
        self.assertEqual(after.spectral["layer_height_ref"], 0.08)
        self.assertTrue(os.path.exists(png))

    def test_one_base_is_refused(self):
        data = json.load(open(self.readings))
        data["strips"] = data["strips"][:1]
        wedgesheet.write(self.readings, data)
        rc = tool.main(["fit", "--readings", self.readings, "--filament", "demo-orange", "--db", self.db,
                        "--no-holdout", "--write"])
        self.assertEqual(rc, 1)
        self.assertFalse(sp.is_calibrated(DB(self.db).filaments["demo-orange"]))

    def test_inconsistent_scale_is_refused(self):
        data = json.load(open(self.readings))
        for s in data["strips"]:
            s["base_reading"]["xyz"] = [v * 3 for v in s["base_reading"]["xyz"]]
        wedgesheet.write(self.readings, data)
        rc = tool.main(["fit", "--readings", self.readings, "--filament", "demo-orange", "--db", self.db,
                        "--no-holdout", "--write"])
        self.assertEqual(rc, 1)

    def test_show(self):
        png = os.path.join(self.d, "show.png")
        self.assertEqual(tool.main(["show", "--demo", "--layers", "4", "--preview", png]), 0)
        self.assertTrue(os.path.exists(png))
        with self.assertRaises(SystemExit):           # not calibrated in this database
            tool.main(["show", "--filaments", "demo-orange", "--db", self.db])


class SpectralPlaque(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.db = os.path.join(self.d, "f.json")
        demo_db(self.db)
        self.img = os.path.join(self.d, "in.png")
        y, x = np.mgrid[0:16, 0:24]
        Image.fromarray(np.stack([x * 10, y * 15, 255 - x * 10], -1).astype(np.uint8)).save(self.img)

    def tearDown(self):
        self.tmp.cleanup()

    def fils(self, *ids):
        db = DB(self.db)
        return [db.filaments[i] for i in ids]

    def test_gamut_interface_matches_rgb(self):
        fils = self.fils("demo-white", "demo-black", "demo-orange", "demo-blue")
        g = plaque.SpectralGamut(fils, fils[0], 0.08, 4, verbose=False)
        self.assertEqual(g.colors.shape[1], 3)
        self.assertEqual(g.states.shape, (len(g.colors), sp.NB))
        self.assertEqual(g.base_index, 0)
        # The breadth-first search reaches exactly the colour cells brute force does.
        import itertools
        Rb0 = sp.r_inf(*sp.ks(fils[0]))
        every = [sp.stack(Rb0, [(*sp.ks(fils[k]), 0.08) for k in st])
                 for n in range(5) for st in itertools.product(range(4), repeat=n)]
        cells = np.unique(plaque.Gamut._keys(sp.spectrum_to_linear(np.array(every)), 192))
        self.assertEqual(len(np.unique(plaque.Gamut._keys(g.colors, 192))), len(cells))
        self.assertTrue(np.isfinite(g.lab).all())
        # A state's colour is its spectrum's colour, and stack() rebuilds the spectrum.
        i = len(g.colors) - 1
        st = g.stack(i)
        Rb = sp.r_inf(*sp.ks(fils[0]))
        R = sp.stack(Rb, [(*sp.ks(fils[k]), 0.08) for k in st])
        self.assertTrue(np.allclose(R, g.spectra(i), atol=1e-4))
        self.assertTrue(np.allclose(sp.spectrum_to_linear(R), g.colors[i], atol=1e-4))

    def test_in_place_layer_is_the_km_formula(self):
        fils = self.fils("demo-white", "demo-orange", "demo-blue")
        g = plaque.SpectralGamut(fils, fils[0], 0.08, 1, verbose=False)
        fc = np.random.default_rng(0).uniform(0.02, 0.95, (7, sp.NB)).astype(np.float32)
        R0, T2 = g.R0[:, None], g.T2[:, None]
        ref = (R0 + T2 * fc[None] / (1.0 - R0 * fc[None])).reshape(-1, sp.NB)
        self.assertTrue(np.array_equal(g._add_layer(fc), ref))

    def test_rgb_gamut_keeps_one_copy_of_its_states(self):
        fils = self.fils("demo-white", "demo-orange")
        g = plaque.Gamut(fils, fils[0], 0.08, 3, verbose=False)
        self.assertIs(g.states, g.colors)

    def test_layer_height_mismatch_is_reported(self):
        fils = self.fils("demo-white", "demo-orange")
        fils[1].spectral["layer_height_ref"] = 0.12
        self.assertEqual(sp.layer_mismatch(fils, 0.12), [])
        msg = sp.layer_mismatch(fils, 0.08)
        self.assertEqual(len(msg), 1)
        self.assertIn("demo-orange", msg[0])
        self.assertIn("0.12", msg[0])

    def test_a_base_that_never_goes_opaque_says_so(self):
        from types import SimpleNamespace
        clear = Filament(id="clear", brand="t", series="t", name="clear", color="#FFFFFF", td=50.0)
        clear.spectral = sp.record(np.full(sp.NB, 1e-5), np.full(sp.NB, 1e-3))
        a = SimpleNamespace(optics="spectral", first_layer_height=0.2, layer_height=0.08, base_layers=3)
        msg = plaque.base_warning(clear, a)
        self.assertIn("never becomes opaque", msg)
        self.assertNotIn("inf", msg)

    def test_uncalibrated_filament_is_refused(self):
        fils = self.fils("demo-white", "demo-orange")
        fils[1].spectral = None
        with self.assertRaises(SystemExit):
            plaque.SpectralGamut(fils, fils[0], 0.08, 2, verbose=False)

    def test_factory_keeps_rgb_the_default(self):
        from types import SimpleNamespace
        fils = self.fils("demo-white", "demo-orange")
        a = SimpleNamespace(layer_height=0.08, max_layers=3)
        g = plaque.make_gamut(fils, fils[0], a)
        self.assertIs(type(g), plaque.Gamut)
        ref = plaque.Gamut(fils, fils[0], 0.08, 3, verbose=False)
        self.assertTrue(np.array_equal(g.colors, ref.colors))
        a.optics = "spectral"
        self.assertIs(type(plaque.make_gamut(fils, fils[0], a)), plaque.SpectralGamut)

    def run_cli(self, *extra):
        return subprocess.run(
            [sys.executable, "-m", "stackforge.tools.plaque", self.img, "--db", self.db, "--width", "8",
             "--max-layers", "3", "--layer-height", "0.08", "--flavor", "prusa", *extra],
            cwd=ROOT, capture_output=True, text=True)

    def test_cli_writes_a_plaque(self):
        out = os.path.join(self.d, "p.3mf")
        r = self.run_cli("--filaments", "demo-white,demo-black,demo-orange,demo-blue", "--optics", "spectral",
                         "--illuminant", "D50", "-o", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("spectral optics under D50", r.stdout)
        self.assertTrue(zipfile.is_zipfile(out))

    def test_cli_refuses_uncalibrated(self):
        db = DB(self.db)
        db.filaments["demo-red"].spectral = None
        db.save()
        r = self.run_cli("--filaments", "demo-white,demo-red", "--optics", "spectral",
                         "-o", os.path.join(self.d, "p.3mf"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("demo-red", r.stdout + r.stderr)
        self.assertIn("spectral", r.stdout + r.stderr)

    def test_base_check_uses_km_transmittance(self):
        from types import SimpleNamespace
        a = SimpleNamespace(optics="spectral", first_layer_height=0.2, layer_height=0.08, base_layers=1)
        white = DB(self.db).filaments["demo-white"]
        self.assertIsNotNone(plaque.base_warning(white, a))        # 0.2 mm of white is see-through
        a.base_layers = None
        plaque.resolve_base_layers(a, white, log=lambda _m: None)
        self.assertIsNone(plaque.base_warning(white, a))
        self.assertLessEqual(plaque.base_transmittance(white, plaque.base_height(a), a), plaque.OPAQUE_T)


if __name__ == "__main__":
    unittest.main()
