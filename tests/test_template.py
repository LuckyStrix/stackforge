"""plaque against a synthetic Flash Studio-style --template: layer grid, patched settings."""
import contextlib, io, json, os, re, tempfile, unittest, zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from stackforge.tools import plaque
from stackforge.core import threemf
from stackforge.core.paths import packaged

FILS = ",".join(f"polymaker-pla-pro-{c}" for c in ("white", "black", "blue", "red"))
LH, FLH = 0.12, 0.2


def make_template(path, slots=4):
    settings = {
        "layer_height": str(LH), "initial_layer_print_height": str(FLH),
        "sparse_infill_density": "15%",
        "filament_colour": ["#111111"] * slots, "filament_multi_colour": ["#111111"] * slots,
        "enable_prime_tower": "1", "wipe_tower_x": ["165"], "wipe_tower_y": ["220.62"],
        "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    }
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter">\n'
             ' <metadata name="Application">BambuStudio-02.03.02.00</metadata>\n</model>\n')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/project_settings.config", json.dumps(settings))


def run_main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        plaque.main([os.path.join(ROOT, "docs", "plaque_target.png"), "--db",
                 packaged("filaments.json"), *argv])
    return out.getvalue()


class Template(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.tpl = os.path.join(self.d, "tpl.3mf")
        make_template(self.tpl)

    def tearDown(self):
        self.tmp.cleanup()

    def test_grid_settings_and_overrides(self):
        out = os.path.join(self.d, "o.3mf")
        log = run_main("--filaments", FILS, "--template", self.tpl, "--width", "6",
                       "--base-layers", "3", "--max-layers", "4", "-o", out)
        self.assertIn("(from template)", log)
        with zipfile.ZipFile(out) as z:
            model = z.read("3D/3dmodel.model").decode()
            cfg = z.read("Metadata/model_settings.config").decode()
            prof = json.loads(z.read("Metadata/project_settings.config"))

        # Every modifier box sits in the middle half of a real slicer layer:
        # base_h = first + (base_layers - 1) * layer, not base_layers * layer.
        base_h = FLH + 2 * LH
        z = np.array([float(v) for v in re.findall(r'z="([-\d.eE]+)"', model)])
        top = z.max()
        self.assertAlmostEqual(((top - base_h) / LH) % 1, 0, places=6)
        inner = z[(z > 1e-9) & (z < top - 1e-9)]
        self.assertGreater(len(inner), 0)
        phase = ((inner - base_h) / LH) % 1
        self.assertTrue(np.all(np.isclose(phase, 0.25) | np.isclose(phase, 0.75)), phase)

        self.assertEqual(prof["sparse_infill_density"], "100%")
        self.assertEqual(prof["infill_combination"], "0")
        self.assertEqual(prof["layer_height"], "0.12")
        self.assertEqual(prof["initial_layer_print_height"], "0.2")
        cols = ["#E2DEDB", "#0C0E0C", "#003287", "#E20010"]
        self.assertEqual([c.upper() for c in prof["filament_colour"]], cols)
        self.assertEqual([c.upper() for c in prof["filament_multi_colour"]], cols)
        self.assertLessEqual(float(prof["wipe_tower_y"][0]), 256 - threemf.PRIME_TOWER_DEPTH)

        for key, val in (("layer_height", "0.12"), ("sparse_infill_density", "100%")):
            self.assertIn(f'<metadata key="{key}" value="{val}"/>', cfg)
        # The plate is the base (T1); colour modifiers are on 2..4.
        parts = re.findall(r'subtype="(\w+)">.*?key="extruder" value="(\d+)"', cfg, re.S)
        self.assertEqual([e for st, e in parts if st == "normal_part"], ["1"])
        self.assertTrue({e for st, e in parts if st == "modifier_part"} <= {"2", "3", "4"})

    def test_too_few_template_slots_is_refused(self):
        make_template(self.tpl, slots=2)
        with self.assertRaises(SystemExit) as cm:
            run_main("--filaments", FILS, "--template", self.tpl, "--width", "6",
                     "--max-layers", "2", "-o", os.path.join(self.d, "o.3mf"))
        self.assertIn("filament slot", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "o.3mf")))

    def test_bad_template_fails_before_work(self):
        bad = os.path.join(self.d, "bad.3mf")
        with open(bad, "w") as f:
            f.write("not a zip")
        with self.assertRaises(SystemExit) as cm:
            run_main("--filaments", FILS, "--template", bad, "-o", os.path.join(self.d, "o.3mf"))
        self.assertIn("not a readable 3MF", str(cm.exception))

    def test_missing_output_dir_fails_before_work(self):
        with self.assertRaises(SystemExit) as cm:
            run_main("--filaments", FILS, "-o", os.path.join(self.d, "nope", "o.3mf"))
        self.assertIn("directory does not exist", str(cm.exception))

    def test_orca_output_without_grid_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            run_main("--filaments", FILS, "-o", os.path.join(self.d, "o.3mf"))
        self.assertIn("--template is needed", str(cm.exception))


class BadInput(unittest.TestCase):
    def test_malformed_model_part_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "broken.3mf")
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("3D/3dmodel.model", "<model><unclosed>")
            with self.assertRaises(SystemExit) as cm:
                threemf.read_3mf(p)
            self.assertIn("not valid XML", str(cm.exception))

    def test_bad_transform_is_reported(self):
        with self.assertRaises(SystemExit):
            threemf.parse_transform("1 0 0 junk")


class Escaping(unittest.TestCase):
    def test_names_with_xml_characters(self):
        v = threemf.box_verts(0, 0, 0, 1, 1, 1)
        item = threemf.Item('Nuts & Bolts "v2" <x>', v, threemf.BOX_TRIS.copy())
        with tempfile.TemporaryDirectory() as d:
            for flavor in ("orca", "prusa"):
                p = os.path.join(d, f"{flavor}.3mf")
                threemf.get_writer(flavor)(p, [item], {0: []}, 1)
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if n.endswith((".model", ".config")):
                            from xml.etree import ElementTree as ET
                            ET.fromstring(z.read(n))   # raises if not well-formed


if __name__ == "__main__":
    unittest.main()
