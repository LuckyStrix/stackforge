"""argform: spec / argv logic (no display needed) and a drift guard over every tool's parser."""
import argparse
import os
import unittest

from tdforge.core import filamentdb
from tdforge.gui.argform import argv as av
from tdforge.gui.argform import overrides
from tdforge.gui.argform.spec import introspect
from tdforge.tools import (calibrate, halftone_compare, make_fixture, munki, polymaker,
                           stackforge, surfacecolor, topdeco)

TOOLS = {"stackforge": stackforge, "topdeco": topdeco, "surfacecolor": surfacecolor,
         "calibrate": calibrate, "munki": munki, "polymaker": polymaker,
         "halftone_compare": halftone_compare, "make_fixture": make_fixture,
         "filamentdb": filamentdb}


def specs():
    for name, mod in TOOLS.items():
        parser = mod.build_parser()
        yield name, parser, introspect(parser, name)


class SpecTests(unittest.TestCase):
    def test_every_tool_introspects(self):
        for name, parser, spec in specs():
            self.assertTrue(spec.fields or spec.subs, name)

    def test_subcommands_found(self):
        _, _, cal = [s for s in specs() if s[0] == "calibrate"][0]
        self.assertEqual(set(cal.subs), {"wedge", "chips", "fit"})

    def test_help_unescaped(self):
        _, _, sf = [s for s in specs() if s[0] == "stackforge"][0]
        f = next(f for f in sf.fields if f.dest == "rank_by")
        self.assertIn("5%", f.help)
        self.assertNotIn("%%", f.help)

    def test_exclusive_groups(self):
        _, _, tp = [s for s in specs() if s[0] == "topdeco"][0]
        self.assertIn(["palette", "filaments"], tp.exclusive)
        _, _, sc = [s for s in specs() if s[0] == "surfacecolor"][0]
        self.assertEqual(sc.exclusive_required, [True])
        self.assertEqual(av.missing_required(sc, {"model": "m", "output": "o", "pattern": "stripes"}),
                         ["palette/filaments"])


class DriftTests(unittest.TestCase):
    def test_every_field_resolves_to_a_known_kind(self):
        for name, _, spec in specs():
            for path, sp in spec.walk():
                for f in sp.fields:
                    kind = overrides.resolve_kind(name, " ".join(path), f)
                    self.assertTrue(overrides.kind_known(kind), (name, path, f.dest, kind))

    def test_overrides_name_existing_dests(self):
        have = {}
        for name, _, spec in specs():
            for path, sp in spec.walk():
                for f in sp.fields:
                    have.setdefault(name, set()).add((" ".join(path), f.dest))
        for (tool, cmd, dest), kind in overrides.OVERRIDES.items():
            self.assertTrue(overrides.kind_known(kind), (tool, cmd, dest))
            pairs = have.get(tool, set())
            if cmd == "*":
                ok = any(d == dest for _, d in pairs)
            else:
                ok = (cmd, dest) in pairs
            self.assertTrue(ok, f"override {tool} {cmd} {dest} names a flag that no longer exists")

    def test_no_dest_collision_along_a_command_path(self):
        for name, _, spec in specs():
            for path, sp in spec.walk():
                if not path:
                    continue
                seen = {f.dest for f in spec.fields}
                for f in sp.fields:
                    self.assertNotIn(f.dest, seen, (name, path, f.dest))

    def test_defaults_roundtrip(self):
        """Defaults -> argv -> parse_args reproduces the defaults Namespace."""
        for name, parser, spec in specs():
            for path, sp in spec.walk():
                if sp.subs:
                    continue
                # fill required fields with throwaway values of the right type
                vals = {}
                for s in self._chain(spec, path):
                    for f in s.fields:
                        if f.required:
                            vals[f.dest] = self._dummy(f)
                    for dests, req in zip(s.exclusive, s.exclusive_required):
                        if req:
                            f = next(x for x in s.fields if x.dest == dests[0])
                            vals[f.dest] = self._dummy(f)
                argv = av.build_argv(spec, vals, path)
                ns = parser.parse_args(argv)
                for s in self._chain(spec, path):
                    for f in s.fields:
                        got = getattr(ns, f.dest)
                        want = vals.get(f.dest, f.default)
                        self.assertEqual(got, want if f.nargs is None or f.dest not in vals
                                         else (want if isinstance(want, list) else [want]),
                                         (name, path, f.dest, argv))

    @staticmethod
    def _chain(spec, path):
        out, cur = [spec], spec
        for p in path:
            cur = cur.subs[p]
            out.append(cur)
        return out

    @staticmethod
    def _dummy(f):
        if f.kind == "choice":
            return f.choices[0]
        if f.kind == "int":
            return 3
        if f.kind == "float":
            return 2.5
        if f.nargs is not None or f.kind == "append":
            return ["x", "y"] if f.nargs in ("*", "+", None) else ["x"] * f.nargs
        return "x"


class ArgvTests(unittest.TestCase):
    def test_coerce(self):
        _, _, sf = [s for s in specs() if s[0] == "stackforge"][0]
        w = next(f for f in sf.fields if f.dest == "width")
        self.assertEqual(av.coerce(w, "60"), 60.0)
        self.assertIsNone(av.coerce(w, ""))
        with self.assertRaises(ValueError):
            av.coerce(w, "abc")
        d = next(f for f in sf.fields if f.dest == "dither")
        with self.assertRaises(ValueError):
            av.coerce(d, "nope")

    def test_defaults_omitted_changes_emitted(self):
        _, parser, sf = [s for s in specs() if s[0] == "stackforge"][0]
        base = {"image": "a.png", "filaments": "x,y"}
        self.assertEqual(av.build_argv(sf, base), ["--filaments", "x,y", "a.png"])
        argv = av.build_argv(sf, {**base, "width": 60.0, "rank": True})
        ns = parser.parse_args(argv)
        self.assertEqual((ns.width, ns.rank), (60.0, True))

    def test_append_and_subcommand(self):
        _, parser, mk = [s for s in specs() if s[0] == "munki"][0]
        argv = av.build_argv(mk, {"nospos": True, "spotread_arg": ["-x", "-y"]}, ("measure-wedge",))
        self.assertIn("--nospos", argv)
        self.assertIn("measure-wedge", argv)
        ns = parser.parse_args(argv)
        self.assertEqual(ns.spotread_arg, ["-x", "-y"])
        self.assertEqual(av.values_from_namespace(mk, ns)["nospos"], True)

    def test_command_line_quotes(self):
        self.assertEqual(av.command_line("p", ["a b", "c"]), "p 'a b' c")


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class FormSmoke(unittest.TestCase):
    def test_forms_build_and_roundtrip(self):
        import tkinter as tk
        from tdforge.gui import theme
        from tdforge.gui.argform.form import CommandForm
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no usable display")
        self.addCleanup(root.destroy)
        theme.apply_theme(root)
        n = 0
        for name, parser, spec in specs():
            for path, sp in spec.walk():
                if sp.subs:
                    continue
                form = CommandForm(root, spec, name, path)
                form.update_idletasks()
                self.assertEqual(set(form.entries), {f.dest for s in form.chain for f in s.fields
                                                     if overrides.resolve_kind(name, " ".join(path), f) != "hide"})
                n += 1
        self.assertGreaterEqual(n, 24)

    def test_exclusion_and_required(self):
        import tkinter as tk
        from tdforge.gui.argform.form import CommandForm
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no usable display")
        self.addCleanup(root.destroy)
        _, parser, spec = [s for s in specs() if s[0] == "topdeco"][0]
        form = CommandForm(root, spec, "topdeco")
        self.assertTrue(form._cmdline.get().startswith("topdeco"))
        self.assertTrue(any("required" in e for e in form.validate()))
        mk = CommandForm(root, [x for x in specs() if x[0] == "munki"][0][2], "munki", ("measure-wedge",))
        self.assertTrue(mk._cmdline.get().startswith("munki measure-wedge"), mk._cmdline.get())
        form.set_values({"model": "m.3mf", "image": "i.png", "output": "o.3mf",
                         "palette": "ff0000,00ff00"})
        self.assertEqual(form.validate(), [])
        ns = parser.parse_args(form.argv())
        self.assertEqual(ns.palette, "ff0000,00ff00")
        self.assertEqual(form.set_values({"nonexistent": 1}), ["nonexistent"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
class PatternVisibility(unittest.TestCase):
    def test_pattern_shows_only_its_parameters(self):
        import tkinter as tk
        from tdforge.gui.argform.form import CommandForm
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no usable display")
        self.addCleanup(root.destroy)
        _, parser, spec = [s for s in specs() if s[0] == "surfacecolor"][0]
        form = CommandForm(root, spec, "surfacecolor")
        root.update()
        vis = lambda: {d for d, e in form.entries.items() if e.visible}
        self.assertFalse(vis() & {"scale", "lat", "period", "expr"})
        form.set_values({"pattern": "stripes", "period": 3.0, "scale": 9.0})
        self.assertTrue({"axis", "period"} <= vis())
        self.assertNotIn("scale", vis())
        self.assertNotIn("scale", form.values())       # hidden fields are not sent
        form.set_values({"pattern": "checker3d"})
        self.assertIn("scale", vis())
        self.assertNotIn("period", form.values())
        # every table entry names a real dest, and a real controlling dest
        have = {f.dest for f in spec.fields}
        for (tool, dest), (ctrl, vals) in overrides.VISIBLE_WHEN.items():
            self.assertIn(dest, have)
            self.assertIn(ctrl, have)
            ctrl_f = next(f for f in spec.fields if f.dest == ctrl)
            self.assertTrue(vals <= set(ctrl_f.choices), (dest, vals))
