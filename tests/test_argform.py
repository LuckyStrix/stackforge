"""argform: spec / argv logic (no display needed) and a drift guard over every tool's parser."""
import unittest

from stackforge.core import filamentdb
from stackforge.gui.argform import argv as av
from stackforge.gui.argform import overrides
from stackforge.gui.argform.spec import introspect
from stackforge.tools import (calibrate, dither_compare, make_samples, measure, polymaker,
                           plaque, paint, spectral, top_paint)

TOOLS = {"plaque": plaque, "top_paint": top_paint, "paint": paint,
         "calibrate": calibrate, "measure": measure, "polymaker": polymaker,
         "dither_compare": dither_compare, "make_samples": make_samples,
         "filamentdb": filamentdb, "spectral": spectral}


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
        _, _, sf = [s for s in specs() if s[0] == "plaque"][0]
        f = next(f for f in sf.fields if f.dest == "rank_by")
        self.assertIn("5%", f.help)
        self.assertNotIn("%%", f.help)

    def test_exclusive_groups(self):
        _, _, tp = [s for s in specs() if s[0] == "top_paint"][0]
        self.assertIn(["palette", "filaments"], tp.exclusive)
        _, _, sc = [s for s in specs() if s[0] == "paint"][0]
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
        _, _, sf = [s for s in specs() if s[0] == "plaque"][0]
        w = next(f for f in sf.fields if f.dest == "width")
        self.assertEqual(av.coerce(w, "60"), 60.0)
        self.assertIsNone(av.coerce(w, ""))
        with self.assertRaises(ValueError):
            av.coerce(w, "abc")
        d = next(f for f in sf.fields if f.dest == "dither")
        with self.assertRaises(ValueError):
            av.coerce(d, "nope")

    def test_defaults_omitted_changes_emitted(self):
        _, parser, sf = [s for s in specs() if s[0] == "plaque"][0]
        base = {"image": "a.png", "filaments": "x,y"}
        self.assertEqual(av.build_argv(sf, base), ["--filaments", "x,y", "a.png"])
        argv = av.build_argv(sf, {**base, "width": 60.0, "rank": True})
        ns = parser.parse_args(argv)
        self.assertEqual((ns.width, ns.rank), (60.0, True))

    def test_append_and_subcommand(self):
        _, parser, mk = [s for s in specs() if s[0] == "measure"][0]
        argv = av.build_argv(mk, {"nospos": True, "spotread_arg": ["-x", "-y"]}, ("measure-wedge",))
        self.assertIn("--nospos", argv)
        self.assertIn("measure-wedge", argv)
        ns = parser.parse_args(argv)
        self.assertEqual(ns.spotread_arg, ["-x", "-y"])
        self.assertEqual(av.values_from_namespace(mk, ns)["nospos"], True)

    def test_command_line_quotes(self):
        self.assertEqual(av.command_line("p", ["a b", "c"]), "p 'a b' c")


class VisibleWhen(unittest.TestCase):
    def test_every_table_entry_names_a_real_dest_and_controlling_choice(self):
        by_tool = {name: spec for name, _parser, spec in specs()}
        for (tool, dest), (ctrl, vals) in overrides.VISIBLE_WHEN.items():
            have = {f.dest: f for f in by_tool[tool].fields}
            self.assertIn(dest, have)
            self.assertIn(ctrl, have)
            self.assertTrue(vals <= set(have[ctrl].choices), (dest, vals))


if __name__ == "__main__":
    unittest.main()
