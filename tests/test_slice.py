"""Slice a plaque 3MF headlessly in Flash Studio and check the slicer agrees with the design.

Runs automatically when Flash Studio and a template are installed; skipped otherwise.
Disable with STACKFORGE_SKIP_SLICER=1. Locations, if not in the defaults:
  FLASHSTUDIO_RUN       path to run.sh   (default ~/Downloads/FlashStudio-1.7.8/run.sh)
  FLASHSTUDIO_TEMPLATE  template 3MF     (default ~/Downloads/ffSample2.3mf; 4 slots, 0.12/0.25)
"""
import os
import re
import subprocess
import tempfile
import unittest

from stackforge.tools import plaque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.expanduser(os.environ.get("FLASHSTUDIO_RUN", "~/Downloads/FlashStudio-1.7.8/run.sh"))
TEMPLATE = os.path.expanduser(os.environ.get("FLASHSTUDIO_TEMPLATE", "~/Downloads/ffSample2.3mf"))
FILS = ",".join(f"polymaker-pla-pro-{c}" for c in ("white", "black", "blue", "red"))
BASE_LAYERS, COLOUR_LAYERS = 3, 4


@unittest.skipIf(os.environ.get("STACKFORGE_SKIP_SLICER"), "STACKFORGE_SKIP_SLICER is set")
@unittest.skipUnless(os.access(RUN, os.X_OK) and os.path.exists(TEMPLATE),
                     "Flash Studio or its template is not installed")
class HeadlessSlice(unittest.TestCase):
    def test_layer_grid_and_tools(self):
        with tempfile.TemporaryDirectory() as d:
            f3 = os.path.join(d, "plaque.3mf")
            plaque.main([os.path.join(ROOT, "docs", "plaque_target.png"), "--filaments", FILS,
                     "--template", TEMPLATE, "--width", "20", "--base-layers", str(BASE_LAYERS),
                     "--max-layers", str(COLOUR_LAYERS), "-o", f3])
            out = os.path.join(d, "out")
            os.mkdir(out)
            p = subprocess.run(
                [RUN, "--datadir", os.path.join(d, "data"), "--slice", "1", "--outputdir", out, f3],
                capture_output=True, text=True, timeout=600)
            gcode_path = os.path.join(out, "plate_1.gcode")
            self.assertTrue(os.path.exists(gcode_path), p.stdout[-800:] + p.stderr[-800:])
            with open(gcode_path) as fh:
                gcode = fh.read()

        # one slicer layer per designed layer: a mismatched grid would add or drop layers
        m = re.search(r"; total layer number: (\d+)", gcode)
        self.assertEqual(int(m.group(1)), BASE_LAYERS + COLOUR_LAYERS)
        # modifier extruder overrides were honoured: every colour's tool is used
        tools = set(re.findall(r"^T(\d+)\s*$", gcode, re.M))
        self.assertTrue({"0", "1", "2", "3"} <= tools, tools)
        self.assertRegex(gcode, r"; layer_height = 0\.12\b")


if __name__ == "__main__":
    unittest.main()
