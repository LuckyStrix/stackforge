import contextlib, io, os, re, tempfile, unittest

import numpy as np

from stackforge.core import pose
from stackforge.tools import make_samples as mf
from stackforge.tools import paint


def box(x0, y0, z0, x1, y1, z1):
    v, t = mf.box(x0, y0, z0, x1, y1, z1)
    return np.asarray(v, float), np.asarray(t)


class Rotation(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(pose.parse_rotate("90,0,-45"), (90.0, 0.0, -45.0))
        self.assertEqual(pose.parse_rotate("90 0 0"), (90.0, 0.0, 0.0))
        for bad in ("90", "a,b,c", "1,2,3,4", "nan,0,0"):
            with self.assertRaises(ValueError):
                pose.parse_rotate(bad)

    def test_matrix_is_a_rotation_in_xyz_order(self):
        r = pose.matrix((30, 40, 50))
        np.testing.assert_allclose(r @ r.T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(r), 1.0)
        # X first: +90 about X takes +Y to +Z; then +90 about Z takes +X to +Y
        np.testing.assert_allclose(pose.matrix((90, 0, 0)) @ [0, 1, 0], [0, 0, 1], atol=1e-12)
        np.testing.assert_allclose(pose.matrix((90, 0, 90)) @ [1, 0, 0], [0, 1, 0], atol=1e-12)

    def test_normalise_angle(self):
        for a, want in ((270, -90), (-180, 180), (180, 180), (540, 180), (-90, -90), (0, 0)):
            self.assertAlmostEqual(pose.normalise_angle(a), want)

    def test_unposer_inverts_the_pose(self):
        v, _ = box(2, 3, 1, 12, 7, 21)
        angles = (30, -60, 100)
        posed = pose.rotate_points([v], angles)[0]
        np.testing.assert_allclose(pose.unposer([v], angles)(posed), v, atol=1e-9)

    def test_image_stays_on_its_faces_when_posed(self):
        """image-planar on a posed model samples the image where the unposed run does."""
        from types import SimpleNamespace
        from PIL import Image
        rng = np.random.default_rng(1)
        v, _ = box(0, 0, 0, 10, 4, 20)
        pts = rng.uniform([0, 0, 0], [10, 4, 20], (500, 3))
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "i.png")
            Image.fromarray(rng.integers(0, 255, (16, 16, 3), dtype=np.uint8)).save(img)
            a = SimpleNamespace(image=img, axis="y")
            size = (10, 4, 20)
            want = paint.t_image_planar(paint.Coords(*pts.T, size), a)
            angles = (90, 0, 30)
            posed = pose.rotate_points([v, pts], angles)[1]
            plo = pose.rotate_points([v], angles)[0].min(0)
            a.unpose = (pose.unposer([v], angles), v.min(0), size)
            cd = paint.Coords(posed[:, 0] - plo[0], posed[:, 1] - plo[1], posed[:, 2], (1, 1, 1),
                              origin=(plo[0], plo[1]))
            np.testing.assert_allclose(paint.t_image_planar(cd, a), want)

    def test_turned_model_stays_where_it_was_on_the_bed(self):
        v, _ = box(100, 50, 0, 110, 70, 40)           # 10 x 20 x 40, centred at (105, 60)
        (r,) = pose.rotate_points([v], (90, 0, 0))
        lo, hi = r.min(0), r.max(0)
        np.testing.assert_allclose(hi - lo, [10, 40, 20], atol=1e-9)
        np.testing.assert_allclose((lo[:2] + hi[:2]) / 2, [105, 60], atol=1e-9)
        self.assertAlmostEqual(lo[2], 0.0)

    def test_items_turn_together(self):
        from stackforge.core import threemf
        a = threemf.Item("a", *box(0, 0, 0, 10, 10, 10))
        b = threemf.Item("b", *box(0, 0, 10, 10, 10, 30))
        pose.rotate_items([a, b], (0, 90, 0))
        self.assertAlmostEqual(min(a.verts[:, 2].min(), b.verts[:, 2].min()), 0.0)
        # the stack lies down along X: b is beside a now, not on it
        self.assertLess(abs(a.verts[:, 2].max() - b.verts[:, 2].max()), 1e-9)


class Stats(unittest.TestCase):
    def test_layers_on_the_slicer_grid(self):
        v, t = box(0, 0, 0, 10, 10, 2.0)
        st = pose.stats([v], [t], 0.2, 0.25)
        self.assertEqual(st["layers"], 1 + 9)          # 0.25 then 1.75 / 0.2 -> 9 more
        st = pose.stats(pose.rotate_points([v], (90, 0, 0)), [t], 0.2, 0.25)
        self.assertEqual(st["size"][2], 10.0)

    def test_a_cube_needs_no_support_a_t_does(self):
        v, t = box(0, 0, 0, 10, 10, 10)
        self.assertEqual(pose.stats([v], [t], 0.2)["overhang_mm2"], 0.0)
        top_v, top_t = box(-10, 0, 10, 20, 10, 12)       # a slab overhanging 10 mm each side
        st = pose.stats([v, top_v], [t, top_t], 0.2)
        self.assertAlmostEqual(st["overhang_mm2"], 300.0)  # its underside, 30 x 10

    def test_views_draw_the_silhouette(self):
        v, t = box(0, 0, 0, 10, 20, 40)
        pts, nrm = pose.sample_surface([v], [t], n=20_000)
        out = pose.views(pts, nrm, (0, 0, 0), px=100)
        lit = {k: (img.sum(-1) > 3 * 60).sum() for k, img in out.items()}
        h, w = out["front"].shape[:2]
        self.assertGreater(h, w)                         # 10 wide, 40 tall
        self.assertGreater(lit["front"], 0.9 * (h - 9) * (w - 9))  # solid, no specks
        self.assertTrue(all(n > 0 for n in lit.values()))


class Cli(unittest.TestCase):
    def run_paint(self, *extra):
        d = self.d
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            paint.main([os.path.join(d, "slab.3mf"), "-o", os.path.join(d, "o.3mf"),
                        "--palette", "#ffffff,#000000", "--pattern", "stripes", "--axis", "z",
                        "--period", "2", "--layer-height", "0.2", "--resolution", "1.0", *extra])
        return out.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        mf.write(os.path.join(self.d, "slab.3mf"), [mf.box(0, 0, 0, 10, 4, 20)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotate_lays_it_down(self):
        layers = lambda s: int(re.search(r"over (\d+) layers", s).group(1))  # noqa: E731
        up, down = self.run_paint(), self.run_paint("--rotate", "90,0,0")
        self.assertEqual(layers(up), 100)              # 20 mm at 0.2
        self.assertEqual(layers(down), 20)             # 4 mm
        self.assertIn("posed: rotated 90,0,0", down)
        from stackforge.core import threemf
        items = threemf.read_3mf(os.path.join(self.d, "o.3mf"))
        z = np.vstack([i.verts for i in items])[:, 2]
        self.assertAlmostEqual(z.max() - z.min(), 4.0, places=5)  # the written model is posed

    def test_dry_run_writes_nothing(self):
        out = self.run_paint("--dry-run")
        self.assertIn("dry run: nothing written", out)
        self.assertFalse(os.path.exists(os.path.join(self.d, "o.3mf")))

    def test_bad_rotate_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_paint("--rotate", "90,0")
