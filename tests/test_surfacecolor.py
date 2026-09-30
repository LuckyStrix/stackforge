import os, subprocess, sys, tempfile, unittest, zipfile
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import make_fixture as mf
import surfacecolor as sc
import td3mf


def cube_item(size=10.0):
    v, t = mf.box(0, 0, 0, size, size, size)
    return td3mf.Item("cube", v, t)


def badge_item():
    """A dome (open at the bottom) sitting in a plate: overlapping, never unioned."""
    v, t = mf.merge([mf.box(0, 0, 0, 70, 70, 3), mf.dome(35, 35, 30, 12)])
    return td3mf.Item("badge", v, t)


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

    def test_index_wraps_to_palette_size(self):
        got = sc.evaluate("expr", self.cd, self.ns(expr="x * 10"), np.zeros((3, 3)))
        self.assertTrue(((got >= 0) & (got < 3)).all())


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
            r = subprocess.run([sys.executable, "surfacecolor.py", src, "-o", out,
                                "--palette", "#FFFFFF,#101010", "--pattern", "checker3d",
                                "--scale", "8", "--resolution", "1.5"],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            with zipfile.ZipFile(out) as z:
                self.assertTrue(any(n.endswith(".model") for n in z.namelist()))


if __name__ == "__main__":
    unittest.main()
