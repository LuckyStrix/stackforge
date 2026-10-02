"""The host app builds, switches tabs and closes. Skipped without a display."""
import os
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class HostSmoke(unittest.TestCase):
    def test_host_builds_and_switches_tabs(self):
        import tkinter as tk
        from tdforge.gui.app import HostApp
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = d
            self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if old is None
                            else os.environ.__setitem__("XDG_CONFIG_HOME", old))
            try:
                app = HostApp()
            except tk.TclError:
                self.skipTest("no usable display")
            try:
                app.update()
                self.assertEqual(set(app.tabs), {"Plaque", "Filaments"})
                app.show_tab("Filaments")
                app.update()
                self.assertIs(app.current(), app.tabs["Filaments"])
                app.show_tab("Plaque")
                app.update()
                app._close()
                self.assertTrue(os.path.exists(os.path.join(d, "tdforge", "settings.json")))
            finally:
                try:
                    app.destroy()
                except tk.TclError:
                    pass


if __name__ == "__main__":
    unittest.main()
