import json
import os
import tempfile
import unittest

from tdforge.gui.project import Project
from tdforge.gui.settings import PresetStore, Settings
from tests.test_template import make_template


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.path = os.path.join(self.d.name, "sub", "settings.json")

    def test_roundtrip_and_defaults(self):
        s = Settings(self.path)
        self.assertEqual(s.get("flavor"), "orca")
        s.set("template", "/x/t.3mf")
        s.add_recent("/a")
        s.add_recent("/b")
        s.add_recent("/a")
        s2 = Settings(self.path)
        self.assertEqual(s2.get("template"), "/x/t.3mf")
        self.assertEqual(s2.get("recent"), ["/a", "/b"])

    def test_corrupt_and_wrong_types_are_not_fatal(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(Settings(self.path).get("flavor"), "orca")
        with open(self.path, "w") as fh:
            json.dump({"flavor": 7, "template": "t", "bogus": 1}, fh)
        s = Settings(self.path)
        self.assertEqual((s.get("flavor"), s.get("template")), ("orca", "t"))

    def test_unknown_key_refused(self):
        with self.assertRaises(KeyError):
            Settings(self.path).set("nope", 1)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.s = Settings(os.path.join(self.d.name, "settings.json"))
        self.p = Project(self.s)

    def test_layers_follow_the_template(self):
        t1 = os.path.join(self.d.name, "a.3mf")
        make_template(t1)
        calls = []
        self.p.subscribe(lambda: calls.append(1))
        self.assertIsNone(self.p.get("layer_height"))
        self.p.set("template", t1)
        lh, fl = self.p.layers()
        self.assertEqual(self.p.get("layer_height"), f"{lh:g}")
        self.assertEqual(self.p.get("first_layer"), f"{fl:g}")
        self.assertTrue(calls)

    def test_override_wins_and_is_explicit(self):
        t1 = os.path.join(self.d.name, "a.3mf")
        make_template(t1)
        self.p.set("template", t1)
        self.p.set("layer_height", "0.2")
        self.assertNotEqual(self.p.get("layer_height"), "0.2")     # ignored until unlocked
        self.p.set("layer_override", True)
        self.assertEqual(self.p.get("layer_height"), "0.2")

    def test_db_defaults_to_absolute_path(self):
        self.assertTrue(os.path.isabs(self.p.get("db")))

    def test_warnings(self):
        self.assertIsNone(self.p.warning())
        self.p.set("template", "/no/such.3mf")
        self.assertIn("not found", self.p.warning())
        t = os.path.join(self.d.name, "a.3mf")
        make_template(t)
        self.p.set("template", t)
        self.assertIsNone(self.p.warning())        # 02.03.02.00 >= 2.3.2


class PresetTests(unittest.TestCase):
    def test_roundtrip_per_command(self):
        with tempfile.TemporaryDirectory() as d:
            st = PresetStore(d)
            st.save("calibrate", ("fit",), "my fit", {"per_channel": True, "x": 1.5})
            st.save("calibrate", ("wedge",), "other", {})
            self.assertEqual(st.names("calibrate", ("fit",)), ["my fit"])
            self.assertEqual(st.load("calibrate", ("fit",), "my fit"), {"per_channel": True, "x": 1.5})
            st.delete("calibrate", ("fit",), "my fit")
            self.assertEqual(st.names("calibrate", ("fit",)), [])
            self.assertEqual(st.clean("a/b"), "a_b")
            with self.assertRaises(ValueError):
                st.clean("  ")


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class FormBinding(unittest.TestCase):
    def test_project_fields_follow_bar_and_stay_out_of_presets(self):
        import tkinter as tk
        from tdforge.gui.argform.form import CommandForm
        from tdforge.gui.argform.spec import introspect
        from tdforge.tools import stackforge
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no usable display")
        self.addCleanup(root.destroy)
        with tempfile.TemporaryDirectory() as d:
            proj = Project(Settings(os.path.join(d, "s.json")))
            form = CommandForm(root, introspect(stackforge.build_parser(), "stackforge"),
                               "stackforge", project=proj)
            proj.subscribe(form.refresh_project)
            self.assertIsNone(form.values().get("layer_height"))
            t = os.path.join(d, "t.3mf")
            make_template(t)
            proj.set("template", t)
            self.assertEqual(form.values()["layer_height"], proj.layers()[0])
            self.assertEqual(form.values()["template"], t)
            form.set_values({"width": 60.0})
            pv = form.preset_values()
            self.assertEqual(pv["width"], 60.0)
            for k in ("template", "layer_height", "first_layer_height", "db"):
                self.assertNotIn(k, pv)
            # a preset naming a dest that no longer exists loads the rest
            self.assertEqual(form.set_values({"width": 40.0, "gone": 1}), ["gone"])
            self.assertEqual(form.values()["width"], 40.0)


if __name__ == "__main__":
    unittest.main()
