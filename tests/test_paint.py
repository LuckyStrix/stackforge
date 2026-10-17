import os, re, subprocess, sys, tempfile, unittest, zipfile
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from stackforge.tools import make_samples as mf
from stackforge.tools import paint as sc
from stackforge.core import threemf


def cube_item(size=10.0):
    v, t = mf.box(0, 0, 0, size, size, size)
    return threemf.Item("cube", v, t)


def badge_item():
    """A dome (open at the bottom) sitting in a plate: overlapping, never unioned."""
    v, t = mf.merge([mf.box(0, 0, 0, 70, 70, 3), mf.dome(35, 35, 30, 12)])
    return threemf.Item("badge", v, t)


class Voxelize(unittest.TestCase):
    def test_cube_volume(self):
        edges = sc.layer_edges(10.0, 0.2, 0.2)
        occ = sc.voxelize(cube_item(), (0, 0, 10, 10), 0.5, edges)
        vol = occ.sum() * 0.5 * 0.5 * 0.2
        self.assertAlmostEqual(vol, 1000.0, delta=40.0)

    def test_overlapping_open_shell_counts_as_solid(self):
        """The bug this guards: parity flips wrongly where a shell overlaps another,
        leaving the dome hollow above the plate (~20k vs ~29k mm^3)."""
        edges = sc.layer_edges(12.0, 0.2, 0.2)
        occ = sc.voxelize(badge_item(), (0, 0, 70, 70), 0.8, edges)
        vol = occ.sum() * 0.8 * 0.8 * 0.2
        self.assertGreater(vol, 27_000)
        self.assertLess(vol, 31_000)
        # the middle of the dome, above the plate, must be solid
        k = int(6.0 / 0.2)
        self.assertTrue(occ[k, 44, 44])

    def test_shell_depth_is_true_distance(self):
        edges = sc.layer_edges(10.0, 0.2, 0.2)
        occ = sc.voxelize(cube_item(), (0, 0, 10, 10), 0.5, edges)
        shell = sc.shell_mask(occ, 0.5, 0.2, 1.0)
        self.assertLess(shell.sum(), occ.sum())
        self.assertFalse(shell[25, 10, 10])          # centre of the cube
        self.assertTrue(shell[25, 10, 1])            # 0.5 mm from a side wall
        self.assertEqual(sc.shell_mask(occ, 0.5, 0.2, 0).sum(), occ.sum())


class Patterns(unittest.TestCase):
    def setUp(self):
        self.cd = sc.Coords(np.array([1.0, 7.0, 13.0]), np.array([1.0, 1.0, 1.0]),
                            np.array([1.0, 1.0, 1.0]), (20, 20, 20))

    def ns(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_checker3d_alternates(self):
        got = sc.evaluate("checker3d", self.cd, self.ns(scale=6.0), np.zeros((2, 3)))
        self.assertEqual(got.tolist(), [0, 1, 0])

    def test_expr_bool_and_rejects_dunder(self):
        got = sc.evaluate("expr", self.cd, self.ns(expr="x > 5"), np.zeros((2, 3)))
        self.assertEqual(got.tolist(), [0, 1, 1])
        with self.assertRaises(SystemExit):
            sc.evaluate("expr", self.cd, self.ns(expr="__import__('os')"), np.zeros((2, 3)))

    def test_expr_whitelist_rejects_escapes(self):
        for bad in ("x.__class__", "().__class__", "[c for c in (1,)]", "(lambda: 1)()",
                    "open('f')", "x[0]", "'abc'", "sin.__call__(x)", "y if z else w2",
                    "__import__('os').system('true')", "getattr(x, 'real')"):
            with self.assertRaises(SystemExit, msg=bad):
                sc.evaluate("expr", self.cd, self.ns(expr=bad), np.zeros((2, 3)))

    def test_expr_huge_power_does_not_hang(self):
        with self.assertRaises(SystemExit):   # float overflow, reported; bigints would hang
            sc.evaluate("expr", self.cd, self.ns(expr="9 ** 9 ** 9"), np.zeros((2, 3)))

    def test_expr_functions_and_constants(self):
        got = sc.evaluate("expr", self.cd, self.ns(expr="where(sin(x * pi) > 0.5, 1, 0)"),
                          np.zeros((2, 3)))
        self.assertEqual(got.shape, self.cd.x.shape)

    def test_index_wraps_to_palette_size(self):
        got = sc.evaluate("expr", self.cd, self.ns(expr="x * 10"), np.zeros((3, 3)))
        self.assertTrue(((got >= 0) & (got < 3)).all())


class ImagePatterns(unittest.TestCase):
    def test_spherical_and_planar_pick_filament_by_image_colour(self):
        import argparse
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lr.png")
            im = np.zeros((4, 8, 3), np.uint8)
            im[:, :4] = 255                      # left half white, right half black
            Image.fromarray(im).save(path)
            pal = np.array([[255, 255, 255], [0, 0, 0]], float)
            a = argparse.Namespace(image=path, lon_offset=0.0, axis="z")
            # azimuth -pi..pi maps to u 0..1: theta = -pi/2 is the left (white) half
            cd = sc.Coords(np.array([10.0, 10.0]), np.array([0.0, 20.0]), np.array([10.0, 10.0]),
                           (20, 20, 20))
            got = sc.evaluate("image-spherical", cd, a, pal)
            self.assertEqual(got.tolist(), [0, 1] if cd.theta[0] < 0 else [1, 0])
            self.assertEqual(sc.evaluate("image-planar", sc.Coords(
                np.array([2.0, 18.0]), np.array([10.0, 10.0]), np.array([0.0, 0.0]),
                (20, 20, 20)), a, pal).tolist(), [0, 1])


class Ranking(unittest.TestCase):
    def ns(self, **kw):
        import argparse
        return argparse.Namespace(**{"dither": "ordered", "dither_strength": 1.0, "pattern": "texture",
                                     "rank_by": "mean", **kw})

    PAL = np.array([[230, 230, 230], [237, 213, 166], [103, 135, 173], [120, 85, 60],
                    [20, 20, 20]], float)        # white (base), light brown, blue, mid brown, black

    def test_mid_brown_target_picks_the_brown_not_the_blue(self):
        """The bug that prompted ranking: a dark skin tone went to blue, the darkest
        filament given. With a mid brown among the candidates it must win."""
        from stackforge.core import colormath
        target = colormath.srgb_to_linear(np.tile([[141, 119, 99]], (500, 1)).astype(float))
        for dither in ("ordered", "off"):
            best = sc.rank_sets({0: self.PAL}, [0], 2, target, self.ns(dither=dither))[0]
            self.assertEqual(best["keep"], [0, 3], dither)

    def test_every_set_keeps_the_base_and_has_slots_members(self):
        from stackforge.core import colormath
        target = colormath.srgb_to_linear(np.array([[200, 50, 50], [50, 50, 200]], float))
        ranked = sc.rank_sets({2: self.PAL}, [2], 3, target, self.ns())
        self.assertEqual(len(ranked), 6)                          # C(4, 2)
        for r in ranked:
            self.assertEqual(r["keep"][0], 2)                    # base first: extruder 1
            self.assertEqual(len(set(r["keep"])), 3)
        self.assertEqual([r["mean"] for r in ranked], sorted(r["mean"] for r in ranked))

    def run_brown_cube(self, *extra):
        """Paint a cube from an all-mid-brown image with PAL; (stdout, extruders in the 3MF)."""
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "cube.3mf")
            c = cube_item()
            mf.write(src, [(c.verts, c.tris)])
            img = os.path.join(d, "brown.png")
            Image.fromarray(np.full((4, 4, 3), (120, 85, 60), np.uint8)).save(img)
            out = os.path.join(d, "out.3mf")
            hexes = ",".join("#%02X%02X%02X" % tuple(int(v) for v in c) for c in self.PAL)
            r = subprocess.run([sys.executable, "-m", "stackforge.tools.paint", src, "-o", out,
                                "--palette", hexes, "--pattern", "image-planar", "--image", img,
                                "--resolution", "1.0", "--layer-height", "0.2", *extra],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            with zipfile.ZipFile(out) as z:
                cfg = z.read("Metadata/model_settings.config").decode()
            return r.stdout, set(re.findall(r'key="extruder" value="(\d+)"', cfg))

    def test_cli_ranks_down_to_slots(self):
        out, used = self.run_brown_cube("--slots", "2", "--base-extruder", "1")
        self.assertIn("ranked 4 sets of 2", out)
        self.assertIn("T2 #78553C", out)                          # the mid brown was chosen
        self.assertEqual(used, {"1", "2"})                        # two extruders, not five

    def test_cli_chooses_the_base(self):
        """Without --base-extruder the base is chosen too: here the brown, which then
        covers the whole surface and needs no modifier at all."""
        out, used = self.run_brown_cube("--slots", "2")
        self.assertIn("ranked 20 set/base pairs of 2", out)
        self.assertIn("T1 #78553C #78553C  (base)", out)          # the base is always extruder 1
        self.assertIn("0 boxes", out)
        self.assertEqual(used, {"1"})

    def test_more_slots_than_filaments_keeps_the_listed_order(self):
        """The review's crash: no --base-extruder and more slots than filaments (a 4-slot
        template, 2 or 3 filaments) ranked no sets at all. And with nothing dropped the
        order is the user's slots, so the base is marked where it is, not moved to T1."""
        out, used = self.run_brown_cube("--slots", "8")
        self.assertIn("ranked 5 set/base pairs of 5", out)
        self.assertIn("T4 #78553C #78553C  (base)", out)                # 4th as listed
        self.assertEqual(used, {"4"})

    def test_tie_goes_to_the_base_covering_most(self):
        rows = [{"mean": 5.00, "usage": 0.1, "id": "a"}, {"mean": 5.05, "usage": 0.6, "id": "b"},
                {"mean": 5.30, "usage": 0.9, "id": "c"}]
        self.assertEqual(sc.pick_best(rows, "mean")["id"], "b")     # within 0.1 dE: a tie
        rows[1]["mean"] = 5.2
        self.assertEqual(sc.pick_best(rows, "mean")["id"], "a")     # 0.2 dE is not

    def test_decorative_pattern_cannot_rank(self):
        with self.assertRaises(SystemExit) as cm:
            sc.main(["x.3mf", "-o", "o.3mf", "--palette", "#000,#fff,#f00", "--slots", "2",
                     "--pattern", "stripes"])
        self.assertIn("colour pattern", str(cm.exception))


class PrintedColour(unittest.TestCase):
    def setUp(self):
        from stackforge.core import spectral
        self.sp = spectral
        self.d = {f.id: f for f in spectral.demo_filaments()}

    def test_translucent_filament_shows_the_base(self):
        red, white, black = self.d["demo-red"], self.d["demo-white"], self.d["demo-black"]
        def gap(depth):        # red channel of the red, over white minus over black
            on_white, _ = sc.printed_palette([white, red], white, depth)
            on_black, _ = sc.printed_palette([black, red], black, depth)
            return on_white[1][0] - on_black[1][0]
        self.assertGreater(gap(0.4), 20)                                  # the base shows through
        self.assertLess(gap(1.2), gap(0.4))                               # less of it, thicker
        bulk, _ = sc.printed_palette([white, red], white, 0)              # depth 0: all the way through
        bulk_b, _ = sc.printed_palette([black, red], black, 0)
        np.testing.assert_allclose(bulk[1], bulk_b[1])

    def test_measured_colour_replaces_the_swatch(self):
        from stackforge.core import colormath
        red, white = self.d["demo-red"], self.d["demo-white"]
        red.color = "#00FF00"                                           # a swatch that is plain wrong
        pal, _ = sc.printed_palette([white, red], white, 1.2)
        self.assertGreater(pal[1][0], pal[1][1])                         # still red, as measured
        sw, _ = sc.printed_palette([white, red], white, 1.2, optics="swatch")
        np.testing.assert_allclose(sw[1], colormath.parse_hex("#00FF00"))

    def test_uncalibrated_keeps_its_swatch_and_says_so(self):
        from stackforge.core.filamentdb import Filament
        plain = Filament(id="plain", name="Plain", color="#336699")
        pal, notes = sc.printed_palette([self.d["demo-white"], plain], self.d["demo-white"], 1.2)
        np.testing.assert_allclose(pal[1], [0x33, 0x66, 0x99])
        self.assertTrue(any("swatch" in n and "Plain" in n for n in notes))


class Stacking(unittest.TestCase):
    def setUp(self):
        from stackforge.core import spectral
        from stackforge.tools import paint_stack
        self.ps = paint_stack
        self.sp = spectral
        d = {f.id: f for f in spectral.demo_filaments()}
        self.fils = [d["demo-white"], d["demo-red"], d["demo-yellow"], d["demo-black"]]

    def test_band_classes(self):
        def ms(*a):
            return [(m, round(u, 3)) for m, u in self.ps.band_classes(*a)]
        self.assertEqual(ms(1.2, 0.08, 0.6), [(15, 0.08), (7, 0.171), (3, 0.4), (2, 0.6)])
        self.assertEqual(ms(1.2, 0.2, 0.6), [(6, 0.2), (3, 0.4), (2, 0.6)])
        # 0.8 mm voxels: a wall has one voxel in 1.2 mm, so it is one band of what it prints
        self.assertEqual(ms(1.2, 0.08, 0.8), [(15, 0.08), (7, 0.171), (3, 0.4), (1, 0.8)])
        # 0.5 mm voxels: two in 1.2 mm, and 0.5 is under a wall band, so one band of 1.0
        self.assertEqual(ms(1.2, 0.08, 0.5)[-1], (1, 1.0))
        # 0.4 mm voxels: three; split in whole voxels of >= 0.6, that is one band of 1.2
        self.assertEqual(ms(1.2, 0.08, 0.4)[-1], (1, 1.2))
        self.assertEqual(ms(1.2, 0.08, 0.3)[-1], (2, 0.6))            # four voxels, two each
        for a in ((1.2, 0.08, 0.6), (0.4, 0.08, 0.6), (0.08, 0.08, 0.6), (2.0, 0.12, 0.45)):
            us = [u for _, u in self.ps.band_classes(*a)]
            self.assertEqual(us, sorted(us), a)                       # thinnest first

    def test_stack_depth_is_whole_layers(self):
        self.assertAlmostEqual(self.ps.stack_depth(1.0, 0.12), 0.96)
        self.assertAlmostEqual(self.ps.stack_depth(1.2, 0.08), 1.2)
        self.assertAlmostEqual(self.ps.stack_depth(0.05, 0.08), 0.08)     # never zero bands

    def test_normals_and_classes_on_a_cube(self):
        res, lh = 0.5, 0.2
        edges = sc.layer_edges(10.0, lh, lh)
        occ = sc.voxelize(cube_item(), (0, 0, 10, 10), res, edges)
        dist = sc.surface_distance(occ, res, lh)
        zs, ys, xs = np.array([len(edges) - 2, 25]), np.array([10, 10]), np.array([10, 0])
        n = self.ps.normals(dist, zs, ys, xs, res, lh)
        np.testing.assert_allclose(n[0], [0, 0, 1], atol=1e-6)            # top face
        np.testing.assert_allclose(n[1], [-1, 0, 0], atol=1e-6)           # the x = 0 wall
        classes = self.ps.band_classes(1.2, lh, res)
        self.assertEqual(list(self.ps.classify(n, lh, res, classes)), [0, len(classes) - 1])
        d = self.ps.true_depth(dist[zs, ys, xs], n, res, lh)
        np.testing.assert_allclose(d, [lh / 2, res / 2], atol=1e-6)      # outermost voxel centres

    def test_slopes_get_bands_in_between(self):
        classes = self.ps.band_classes(1.2, 0.08, 0.6)                   # 15, 7, 3, 2 bands
        t = np.radians([0, 20, 45, 90])                                   # tilt of the normal from up
        n = np.stack([np.sin(t), np.zeros(4), np.cos(t)], 1)
        got = [classes[c][0] for c in self.ps.classify(n, 0.08, 0.6, classes)]
        # 20 deg: a voxel step along the normal is 0.6 sin 20 + 0.08 cos 20 = 0.28 mm -> 4 -> 3
        self.assertEqual(got, [15, 3, 2, 2])

    def test_every_band_has_a_voxel(self):
        """The bug the review found: at 0.8 mm voxels a wall's second 0.6 mm band had no
        voxel, so a stack's second filament never printed. Now a wall gets one band."""
        res, lh, depth = 0.8, 0.2, 1.2
        edges = sc.layer_edges(10.0, lh, lh)
        occ = sc.voxelize(cube_item(), (0, 0, 10, 10), res, edges)
        dist = sc.surface_distance(occ, res, lh)
        shell = occ & (dist <= depth)
        zs, ys, xs = np.nonzero(shell)
        n = self.ps.normals(dist, zs, ys, xs, res, lh)
        classes = self.ps.band_classes(depth, lh, res)
        cls = self.ps.classify(n, lh, res, classes)
        d = self.ps.true_depth(dist[zs, ys, xs], n, res, lh)
        st = self.ps.Stacks(self.fils, 0, classes)
        band = st.band(cls, d)
        for c, (m, u) in enumerate(classes):
            sel = cls == c
            if sel.any():       # every band of the class is some voxel's band
                self.assertEqual(set(band[sel][band[sel] < m]), set(range(m)), f"{m} bands")
        m, u = classes[-1]      # and the wall models what its voxels print: one 0.8 mm voxel
        self.assertAlmostEqual(m * u, res)

    def test_a_column_shares_anchor_class_and_target(self):
        """Voxels down one column must solve alike, or the bands mix; the review saw float
        noise split a column's dither threshold on a face that sat on a voxel boundary."""
        import argparse
        res, lh = 1.0, 0.2
        edges = sc.layer_edges(10.0, lh, lh)
        occ = sc.voxelize(cube_item(), (0, 0, 10, 10), res, edges)
        dist = sc.surface_distance(occ, res, lh)
        shell = occ & (dist <= 1.2)
        coords = sc.shell_coords(shell, (0, 0, 10, 10), res, edges, (10, 10, 10))
        a = argparse.Namespace(resolution=res, layer_height=lh, wall_band=0.6, stack_depth=1.2,
                               size=(10, 10, 10), pattern="image-planar", image=None, axis="z")
        a.image = os.path.join(tempfile.mkdtemp(), "g.png")
        from PIL import Image
        Image.fromarray(np.random.default_rng(0).integers(0, 255, (16, 16, 3), np.uint8)).save(a.image)
        classes = self.ps.band_classes(1.2, lh, res)
        geo = sc.stack_geometry(dist, coords, edges, classes, a)
        (zs, ys, xs), _ = coords
        bottom = (ys == 5) & (xs == 5) & (zs < 6)                        # the bottom-face column
        self.assertGreater(bottom.sum(), 1)
        for key in ("cells", "cls", "target"):
            v = geo[key][bottom]
            self.assertTrue((v == v[0]).all(), key)
        top = (ys == 5) & (xs == 5) & (zs >= len(edges) - 7)             # and the top-face one
        for key in ("cells", "cls", "target"):
            v = geo[key][top]
            self.assertTrue((v == v[0]).all(), key)

    def test_labels_decode_like_plaques_stack(self):
        st = self.ps.Stacks(self.fils, 0, [(3, 0.08), (1, 0.24)])
        g = st.gamuts[0]
        states = np.arange(len(g.colors))
        cls = np.zeros(len(states), int)
        for j in range(4):
            got = st.labels(cls, states, np.full(len(states), j))
            want = [g.stack(int(s))[::-1][j] if j < 3 else 0 for s in states]
            self.assertEqual(got.tolist(), want, f"band {j}")

    def test_solve_finds_a_stack_colour(self):
        from stackforge.core import colormath
        st = self.ps.Stacks(self.fils, 0, [(3, 0.08), (1, 0.24)])
        g = st.gamuts[0]
        pick = np.linspace(1, len(g.colors) - 1, 25).astype(int)
        target = g.colors[pick]
        cls = np.zeros(len(pick), int)
        _, shown, own = st.solve(target, cls, np.zeros((len(pick), 3), int), 0.0)
        de = np.linalg.norm(colormath.linear_to_lab(own) - colormath.linear_to_lab(target), axis=-1)
        self.assertLess(de.max(), 1e-6)
        np.testing.assert_allclose(shown, own)

    def test_cli_stacking_beats_one_filament_per_voxel(self):
        """A pink that is 0.2 mm of red over white: no single filament has it, a top face's
        stack does exactly. The walls (half the cube) only get 0.6 mm bands, which cannot
        make a pink that needs 0.2 mm of red, so about half the error goes."""
        from PIL import Image
        from stackforge.core.filamentdb import DB
        white, red = self.fils[0], self.fils[1]
        pink = self.sp.spectrum_to_srgb(self.sp.layer(self.sp.r_inf(*self.sp.ks(white)),
                                                      *self.sp.ks(red), 0.2))
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, "f.json"))
            for f in self.fils:
                db.filaments[f.id] = f
            db.save()
            src = os.path.join(d, "cube.3mf")
            c = cube_item()
            mf.write(src, [(c.verts, c.tris)])
            img = os.path.join(d, "pink.png")
            Image.fromarray(np.full((4, 4, 3), np.round(pink), np.uint8)).save(img)
            errs, outs = {}, {}
            for stack in ("on", "off"):
                r = subprocess.run([sys.executable, "-m", "stackforge.tools.paint", src,
                                    "-o", os.path.join(d, f"{stack}.3mf"), "--db", db.path,
                                    "--filaments", ",".join(f.id for f in self.fils),
                                    "--pattern", "image-planar", "--image", img, "--dither", "off",
                                    "--resolution", "0.6", "--layer-height", "0.2", "--stack", stack],
                                   cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
                errs[stack] = float(re.search(r"mean colour error ([\d.]+) dE", r.stdout).group(1))
                outs[stack] = r.stdout
        self.assertIn("using (stacked):", outs["on"])
        self.assertIn("using (one filament per voxel):", outs["off"])
        self.assertLess(errs["on"], errs["off"] - 3)      # 8.3 -> 4.2 dE at 0.6 mm voxels


class StackingOff(unittest.TestCase):
    def test_uncalibrated_set_is_not_stacked_and_says_why_up_front(self):
        from PIL import Image
        from stackforge.core import spectral
        from stackforge.core.filamentdb import DB, Filament
        white = {f.id: f for f in spectral.demo_filaments()}["demo-white"]
        plain = Filament(id="plain", name="Plain", color="#336699")
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, "f.json"))
            db.filaments[white.id], db.filaments[plain.id] = white, plain
            db.save()
            src = os.path.join(d, "cube.3mf")
            c = cube_item()
            mf.write(src, [(c.verts, c.tris)])
            img = os.path.join(d, "i.png")
            Image.fromarray(np.full((4, 4, 3), (80, 100, 160), np.uint8)).save(img)
            r = subprocess.run([sys.executable, "-m", "stackforge.tools.paint", src,
                                "-o", os.path.join(d, "o.3mf"), "--db", db.path,
                                "--filaments", "demo-white,plain", "--pattern", "image-planar",
                                "--image", img, "--resolution", "1.0", "--layer-height", "0.2"],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("stacking off: stacking needs 2 spectrally calibrated filaments; 1 are", r.stdout)
        self.assertLess(r.stdout.index("stacking off"), r.stdout.index("reading"))
        self.assertNotIn("stacked in bands", r.stdout)


class Preview(unittest.TestCase):
    def test_front_view_is_not_stretched_by_thin_layers(self):
        from PIL import Image
        edges = sc.layer_edges(8.0, 0.2, 0.08)                 # 8 mm tall, 0.08 mm layers
        lab = np.zeros((len(edges) - 1, 10, 10), np.int16)     # 10 x 10 voxels of 0.8 mm = 8 mm
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.png")
            sc.save_preview(p, lab, np.array([[255, 0, 0]], float), edges, 0.8)
            w, h = Image.open(p).size
        self.assertEqual(h, 10)                                # 8 mm / 0.8 mm, not 99 layers
        self.assertEqual(w, 10 + 4 + 10)


class Boxes(unittest.TestCase):
    def test_boxes_sit_mid_layer_and_skip_base(self):
        edges = sc.layer_edges(0.6, 0.2, 0.2)
        lab = np.zeros((len(edges) - 1, 4, 4), np.int16)
        lab[1, :, :2] = 1
        parts = sc.emit_boxes(lab, (0, 0, 4, 4), 1.0, edges, base_index=0)
        self.assertEqual([p[0] for p in parts], [2])         # only extruder 2; base emits nothing
        z = parts[0][1][:, 2]
        self.assertAlmostEqual(z.min(), 0.2 + 0.05)
        self.assertAlmostEqual(z.max(), 0.2 + 0.15)


class EndToEnd(unittest.TestCase):
    def test_cli_writes_3mf(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "badge.3mf")
            b = badge_item()
            mf.write(src, [(b.verts, b.tris)])
            out = os.path.join(d, "out.3mf")
            r = subprocess.run([sys.executable, "-m", "stackforge.tools.paint", src, "-o", out,
                                "--palette", "#FFFFFF,#101010", "--pattern", "checker3d",
                                "--scale", "8", "--resolution", "1.5", "--layer-height", "0.2"],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            with zipfile.ZipFile(out) as z:
                self.assertTrue(any(n.endswith(".model") for n in z.namelist()))


if __name__ == "__main__":
    unittest.main()
