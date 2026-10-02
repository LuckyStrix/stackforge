"""End-to-end smoke test: a tiny image through stackforge to a valid 3MF."""
import os, subprocess, sys, tempfile, unittest, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Smoke(unittest.TestCase):
    def test_plaque_written(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "in.png")
            Image.linear_gradient("L").resize((24, 16)).convert("RGB").save(img)
            out = os.path.join(d, "out.3mf")
            ids = ",".join(f"polymaker-pla-pro-{c}" for c in ("white", "black", "blue", "red"))
            r = subprocess.run(
                [sys.executable, "-m", "tdforge.tools.stackforge", img, "--filaments", ids,
                 "--base", "polymaker-pla-pro-white", "--width", "6",
                 "--max-layers", "4", "--base-layers", "3", "-o", out],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with zipfile.ZipFile(out) as z:
                self.assertIn("3D/3dmodel.model", z.namelist())

    def test_ranking_picks_a_subset_that_fits_the_slots(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "in.png")
            Image.linear_gradient("L").resize((24, 16)).convert("RGB").save(img)
            ids = ",".join(f"polymaker-pla-pro-{c}" for c in ("white", "black", "blue", "red", "yellow"))
            r = subprocess.run(
                [sys.executable, "-m", "tdforge.tools.stackforge", img, "--filaments", ids,
                 "--base", "polymaker-pla-pro-white", "--slots", "3", "--width", "6",
                 "--max-layers", "3", "--rank-samples", "50", "--top", "2",
                 "--rank-sheet", os.path.join(d, "sheet.png")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("6 combinations", r.stdout)   # C(4,2), base always included
            self.assertTrue(os.path.exists(os.path.join(d, "sheet.png")))


if __name__ == "__main__":
    unittest.main()
