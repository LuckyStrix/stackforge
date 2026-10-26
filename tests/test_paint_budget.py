import contextlib, io, itertools, os, re, tempfile, unittest
from unittest import mock

import numpy as np

from stackforge.tools import make_samples as mf
from stackforge.tools import paint, paint_budget as pb

B, X, Y, Z = 1, 2, 4, 8          # tool bits; B is the base


class Changes(unittest.TestCase):
    def test_every_tool_on_every_layer_is_three_a_layer(self):
        # the head as sliced: a layer starts on the tool the last one ended on
        self.assertEqual(pb.count_changes([B | X | Y | Z] * 10), 30)

    def test_one_tool_is_free(self):
        self.assertEqual(pb.count_changes([B] * 5), 0)

    def test_back_to_the_base_costs_the_boundary(self):
        # B; B then X; X then B
        self.assertEqual(pb.count_changes([B, B | X, B]), 2)

    def test_first_layer_may_end_on_any_tool(self):
        ch, last = pb.step(0, B | X)
        self.assertEqual((ch, last), (1, B | X))

    def test_a_tool_outside_the_layer_means_any_may_end(self):
        # carried in on B or X; X does not print here, so the slicer may start on Y and end on B
        ch, last = pb.step(B | X, B | Y)
        self.assertEqual(ch, 2)
        self.assertEqual(last, B | Y)

    def test_alternating_pairs_with_the_base(self):
        # {B,X} / {B,Y}: worst case 2 a layer after the first, never more
        n = pb.count_changes([B | X, B | Y] * 4)
        self.assertEqual(n, 1 + 2 * 7)

    def test_layer_tools_counts_the_core_as_base(self):
        occ = np.ones((2, 2, 2), bool)
        lab = np.full((2, 2, 2), -1, np.int16)
        lab[0] = 2                                   # layer 0 fully coloured, no base
        lab[1, 0, 0] = 1
        np.testing.assert_array_equal(pb.layer_tools(lab, occ, 0), [Y, B | X])


class Plans(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = np.random.default_rng(3)
        nz, masks = 5, [B | X | Y, B | X, B | Y, B]
        cost = rng.uniform(0, 10, (nz, len(masks)))
        cost[:, 0] = 0                                  # the full set is best for colour
        used = np.tile(masks, (nz, 1))
        lams = np.array([0.01, 1.0, 3.0, 100.0])
        got = pb.plans(cost, used, lams)
        for li, lam in enumerate(lams):
            def total(p):
                return cost[np.arange(nz), p].sum() + lam * pb.count_changes(used[np.arange(nz), p])
            best = min(total(np.array(p)) for p in itertools.product(range(len(masks)), repeat=nz))
            self.assertAlmostEqual(total(got[li]), best, places=9)
        self.assertTrue((got[0] == 0).all())          # tiny weight: every tool
        self.assertTrue((got[-1] == 3).all())         # huge weight: the base alone

    def test_spread_keeps_both_ends(self):
        idx = pb.spread(list(range(0, 1000, 7)), 5)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], len(range(0, 1000, 7)) - 1)
        self.assertLessEqual(len(idx), 5)


def _bands_image(path, colours):
    """Horizontal bands, one per colour: projected along x, the colour changes with height."""
    from PIL import Image
    img = np.zeros((len(colours) * 4, 8, 3), np.uint8)
    for i, c in enumerate(colours):
        img[i * 4:(i + 1) * 4] = c
    Image.fromarray(img).save(path)


class Record(pb.Budget):
    made = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        Record.made.append(self)


def run(argv):
    out = io.StringIO()
    Record.made = []
    with mock.patch.object(pb, "Budget", Record), contextlib.redirect_stdout(out):
        paint.main(argv)
    return out.getvalue()


def changes_in(text):
    return int(re.search(r"tool changes: at most (\d+)", text).group(1))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.src = os.path.join(d, "cube.3mf")
        v, t = mf.box(0, 0, 0, 10, 10, 10)
        mf.write(self.src, [(v, t)])
        self.img = os.path.join(d, "bands.png")
        _bands_image(self.img, [(230, 230, 230), (200, 30, 30), (240, 200, 40), (30, 30, 30)])
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def flat(self, *extra):
        return run([self.src, "-o", os.path.join(self.d, "o.3mf"), "--palette",
                    "#e6e6e6,#c81e1e,#f0c828,#1e1e1e", "--base-extruder", "1",
                    "--pattern", "image-planar", "--axis", "x", "--image", self.img,
                    "--resolution", "1.0", "--layer-height", "0.2", "--depth", "1.0", *extra])

    def test_no_budget_and_a_huge_budget_write_the_same(self):
        plain, huge = self.flat(), self.flat("--max-toolchanges", "100000")
        err = r"mean colour error ([\d.]+) dE"
        self.assertEqual(re.search(err, plain).group(1), re.search(err, huge).group(1))
        self.assertEqual(changes_in(plain), changes_in(huge))
        self.assertEqual(re.search(r"(\d+) boxes", plain).group(1),
                         re.search(r"(\d+) boxes", huge).group(1))

    def test_zero_is_the_base_alone(self):
        out = self.flat("--max-toolchanges", "0")
        self.assertEqual(changes_in(out), 0)
        self.assertIn("0 boxes", out)

    def test_the_budget_holds_and_costs_colour(self):
        full = self.flat()
        limit = changes_in(full) // 2
        half = self.flat("--max-toolchanges", str(limit))
        self.assertLessEqual(changes_in(half), limit)
        err = r"mean colour error ([\d.]+) dE"
        self.assertGreater(float(re.search(err, half).group(1)),
                           float(re.search(err, full).group(1)))

    def test_the_curve_runs_from_few_changes_to_unrestricted(self):
        import json
        path = os.path.join(self.d, "curve.json")
        out = self.flat("--toolchange-curve", path)
        pts = json.load(open(path))["points"]
        self.assertEqual(pts[-1]["changes"], changes_in(out))      # the plan written: no budget
        self.assertEqual([p["changes"] for p in pts], sorted(p["changes"] for p in pts))
        self.assertGreater(pts[0]["mean"], pts[-1]["mean"])

    def test_too_many_filaments_skip_the_curve_but_refuse_a_limit(self):
        pal = ",".join(["#e6e6e6", "#c81e1e", "#f0c828", "#1e1e1e", "#2040c0", "#20a040",
                        "#a020a0"])
        path = os.path.join(self.d, "curve.json")
        args = [self.src, "-o", os.path.join(self.d, "o.3mf"), "--palette", pal,
                "--base-extruder", "1", "--pattern", "image-planar", "--axis", "x",
                "--image", self.img, "--resolution", "1.0", "--layer-height", "0.2"]
        out = run(args + ["--toolchange-curve", path])
        self.assertIn("no tool-change curve", out)
        self.assertFalse(os.path.exists(path))
        with self.assertRaises(SystemExit):
            run(args + ["--max-toolchanges", "5"])

    def test_decorative_patterns_are_refused(self):
        with self.assertRaises(SystemExit):
            run([self.src, "-o", os.path.join(self.d, "o.3mf"), "--palette", "#fff,#000",
                 "--pattern", "checker3d", "--layer-height", "0.2", "--max-toolchanges", "3"])


class Stacked(unittest.TestCase):
    """Stacked columns cross layers (a top face's bands are its layers): whatever the plan,
    no voxel may print a tool its layer has not got, and the full plan is the plain solve."""

    @classmethod
    def setUpClass(cls):
        from stackforge.core import spectral
        from stackforge.core.filamentdb import DB
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        db = DB(os.path.join(d, "f.json"))
        for f in spectral.demo_filaments():
            db.filaments[f.id] = f
        db.save()
        src = os.path.join(d, "cube.3mf")
        v, t = mf.box(0, 0, 0, 8, 8, 4)
        mf.write(src, [(v, t)])
        img = os.path.join(d, "bands.png")
        _bands_image(img, [(240, 170, 170), (200, 30, 30), (240, 220, 120), (90, 90, 90)])
        cls.argv = [src, "-o", os.path.join(d, "o.3mf"), "--db", db.path, "--filaments",
                    "demo-white,demo-red,demo-yellow,demo-black", "--base-extruder", "1",
                    "--pattern", "image-planar", "--axis", "x", "--image", img,
                    "--resolution", "0.8", "--layer-height", "0.2", "--depth", "0.8"]
        cls.out = run(cls.argv + ["--max-toolchanges", "100000"])
        cls.b = Record.made[0]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_it_stacked(self):
        self.assertIn("stacked in bands", self.out)
        self.assertIsNotNone(self.b.stacks)

    def check(self, plan):
        ev = self.b.evaluate(plan, keep=True)
        stats = self.b.evaluate(plan)
        self.assertEqual((stats["changes"], stats["mean"]), (ev["changes"], ev["mean"]))
        allowed = np.array([self.b.masks[i] for i in plan])
        for sh, labels, de in zip(self.b.shells, ev["labels"], ev["de"]):
            bad = (allowed[sh.zs] >> labels.astype(np.int64)) & 1 == 0
            self.assertFalse(bad.any(), f"{bad.sum()} voxels print a tool their layer lacks")
            vol = np.full(sh.s.occ.shape, -1, np.int16)
            vol[sh.zs, sh.ys, sh.xs] = labels           # the per-voxel count is the volume's
            np.testing.assert_array_equal(self.b._used(sh, labels),
                                          pb.layer_tools(vol, sh.s.occ, self.b.base))
            # every voxel down a column still comes from one stack: one dE per column
            col, _ = self.b.columns(sh)
            for c in np.unique(col):
                self.assertEqual(len(np.unique(de[col == c])), 1)
        return ev

    def test_random_plans_do_cross_layers(self):
        """The cube's top face stacks over several layers, so a random plan re-solves."""
        sh = self.b.shells[0]
        col, _ = self.b.columns(sh)
        span = np.zeros(col.max() + 1, int)
        np.maximum.at(span, col, sh.zs)
        low = np.full(col.max() + 1, 10 ** 6)
        np.minimum.at(low, col, sh.zs)
        self.assertTrue((span > low).any())

    def test_random_plans_keep_every_voxel_within_its_layer(self):
        rng = np.random.default_rng(0)
        for _ in range(3):
            self.check(rng.integers(0, len(self.b.masks), self.b.nz))

    def test_every_curve_point_comes_back_under_its_own_count(self):
        """The GUI's slider reruns with a point's evaluated count: that must find a plan at
        least as good, within the count."""
        for pt in self.b.curve():
            ev = self.b.choose(pt["changes"])
            self.assertLessEqual(ev["changes"], pt["changes"])
            self.assertLessEqual(ev["mean"], pt["mean"] + 1e-9)

    def test_the_full_plan_is_the_plain_solve(self):
        ev = self.check(np.zeros(self.b.nz, np.int64))
        sh = self.b.shells[0]
        np.testing.assert_array_equal(ev["labels"][0], sh.labels[0])
        plain = run(self.argv)
        err = r"mean colour error ([\d.]+) dE"
        self.assertEqual(re.search(err, plain).group(1), re.search(err, self.out).group(1))
        self.assertEqual(changes_in(plain), changes_in(self.out))
