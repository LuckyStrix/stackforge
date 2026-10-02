"""{dest: value} <-> argv. No tkinter.

Values are typed Python values: str, int, float, bool, or a list for append / nargs fields.
Flags equal to their default are left out so the shown command line stays short; required
flags and positionals are always emitted.
"""
from __future__ import annotations

import shlex

from tdforge.gui.argform.spec import FieldSpec, FormSpec


def coerce(f: FieldSpec, raw):
    """Text (or an already-typed value) -> the field's Python value. '' / None -> None.
    Raises ValueError with a readable message on bad input."""
    if raw is None or raw == "" or raw == []:
        return None
    if f.kind == "bool":
        return bool(raw)
    if f.kind == "append" or f.nargs in ("*", "+") or isinstance(f.nargs, int):
        items = raw.split() if isinstance(raw, str) else list(raw)
        if isinstance(f.nargs, int) and len(items) != f.nargs:
            raise ValueError(f"{f.flag}: needs exactly {f.nargs} values")
        if f.kind in ("int", "float"):
            conv = int if f.kind == "int" else float
            try:
                items = [conv(x) for x in items]
            except ValueError:
                raise ValueError(f"{f.flag}: expected {f.kind} values") from None
        return items
    if f.kind in ("int", "float"):
        try:
            return int(raw) if f.kind == "int" else float(raw)
        except ValueError:
            raise ValueError(f"{f.flag}: expected {f.kind}, got {raw!r}") from None
    if f.kind == "choice" and raw not in f.choices:
        raise ValueError(f"{f.flag}: {raw!r} is not one of {', '.join(map(str, f.choices))}")
    return str(raw)


def _tokens(f: FieldSpec, v) -> list:
    if f.positional:
        return [str(x) for x in (v if isinstance(v, list) else [v])]
    if f.kind == "bool":
        return [f.flag] if v else []
    if f.kind == "append":
        return [t for x in v for t in _opt(f.flag, x)]
    if isinstance(v, list):
        return [f.flag] + [str(x) for x in v]
    return _opt(f.flag, v)


def _opt(flag, v) -> list:
    """`--flag value`, or `--flag=value` when the value starts with '-' (argparse would
    otherwise read it as another option)."""
    s = str(v)
    return [f"{flag}={s}"] if s.startswith("-") and not _is_number(s) else [flag, s]


def _is_number(s) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _same(f: FieldSpec, v) -> bool:
    d = f.default
    if f.kind == "bool":
        return bool(v) == bool(d)
    if d is None or d == []:
        return v is None or v == []
    return v == d


def fields_argv(spec: FormSpec, values: dict) -> list:
    """argv tokens for one spec's own fields: options first, then positionals."""
    opts, pos = [], []
    for f in spec.fields:
        v = values.get(f.dest)
        if v is None or v == [] or (f.kind == "bool" and not v):
            continue
        if _same(f, v) and not f.required:
            continue
        (pos if f.positional else opts).extend(_tokens(f, v))
    return opts + pos


def build_argv(spec: FormSpec, values: dict, command: tuple = ()) -> list:
    """Full argv for `command` (a path of subcommand names). `values` is flat: {dest: value}
    (one tab shows one command, so dests do not collide in practice; when a subcommand
    reuses a parent's dest, pass values={"<cmd>.<dest>": ...} - looked up first)."""
    out, cur = [], spec
    path = ""
    while True:
        scope = {f.dest: values.get(path + f.dest, values.get(f.dest)) for f in cur.fields}
        out += fields_argv(cur, scope)
        if not command:
            break
        name, command = command[0], command[1:]
        out.append(name)
        cur = cur.subs[name]
        path += name + "."
    return out


def missing_required(spec: FormSpec, values: dict) -> list:
    """Dests of required fields with no value (a mutually exclusive set counts once)."""
    miss = []
    for f in spec.fields:
        v = values.get(f.dest)
        if f.required and (v is None or v == [] or (f.kind == "bool" and not v)):
            miss.append(f.dest)
    for dests, req in zip(spec.exclusive, spec.exclusive_required):
        if req and all(values.get(d) in (None, [], False, "") for d in dests):
            miss.append("/".join(dests))
    return miss


def values_from_namespace(spec: FormSpec, ns) -> dict:
    """Namespace -> {dest: value} for the fields of `spec` (and its chosen subcommand)."""
    out = {}
    for f in spec.fields:
        if hasattr(ns, f.dest):
            out[f.dest] = getattr(ns, f.dest)
    sub = getattr(ns, spec.sub_dest, None) if spec.sub_dest else None
    if sub in spec.subs:
        out.update(values_from_namespace(spec.subs[sub], ns))
    return out


def command_line(prog: str, argv: list) -> str:
    return shlex.join([prog] + argv)
