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
                [sys.executable, "stackforge.py", img, "--filaments", ids,
                 "--base", "polymaker-pla-pro-white", "--width", "6",
                 "--max-layers", "4", "--base-layers", "3", "-o", out],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with zipfile.ZipFile(out) as z:
                self.assertIn("3D/3dmodel.model", z.namelist())


if __name__ == "__main__":
    unittest.main()
