import json
import os
import tempfile
import unittest

from stackforge.gui.project import Project
from stackforge.gui.settings import PresetStore, Settings
from tests.test_template import make_template


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.path = os.path.join(self.d.name, "sub", "settings.json")

    def test_old_tdforge_config_is_carried_over(self):
        from unittest import mock
        from stackforge.gui.settings import config_dir
        old = os.path.join(self.d.name, "tdforge")
        os.makedirs(os.path.join(old, "presets", "stackforge"))
        with open(os.path.join(old, "settings.json"), "w") as fh:
            json.dump({"template": "/t.3mf"}, fh)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d.name}):
            new = config_dir()
        self.assertEqual(Settings(os.path.join(new, "settings.json")).get("template"), "/t.3mf")
        self.assertTrue(os.path.isdir(os.path.join(new, "presets", "plaque")))
        self.assertTrue(os.path.isdir(old))

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
        self.assertIn("no template", self.p.warning())     # orca with no grid to go on
        self.p.set("flavor", "prusa")
        self.assertIsNone(self.p.warning())
        self.p.set("flavor", "orca")
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




if __name__ == "__main__":
    unittest.main()
