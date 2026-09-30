"""topdeco: transparent parts of a logo must stay unpainted."""
import os, subprocess, sys, tempfile, unittest

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import make_fixture as mf


class Transparency(unittest.TestCase):
    def test_transparent_logo_background_is_not_painted(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "plate.3mf")
            mf.write(src, [mf.box(0, 0, 0, 40, 40, 3)])
            logo = os.path.join(d, "logo.png")
            im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
            im.paste((255, 0, 0, 255), (15, 15, 25, 25))      # red square, 1/16 of the area
            im.save(logo)
            r = subprocess.run(
                [sys.executable, "topdeco.py", src, logo, "--palette", "000000,ff0000,ffff00",
                 "-o", os.path.join(d, "out.3mf")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            pct = float(r.stdout.split("% of grid")[0].rsplit("(", 1)[1])
            self.assertLess(pct, 15)       # was ~99% when transparency became white


if __name__ == "__main__":
    unittest.main()
