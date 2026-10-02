"""argparse parser -> FormSpec. No tkinter.

Walks the parser's private `_actions` / `_action_groups` / `_mutually_exclusive_groups`
(stable since 3.2). Everything that touches those lives in `introspect`, so an argparse
change is one fix.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field


@dataclass
class FieldSpec:
    dest: str
    flags: tuple            # option strings; empty for a positional
    kind: str               # str | int | float | bool | choice | append
    default: object = None
    choices: tuple = ()
    required: bool = False
    help: str = ""
    metavar: str = ""
    nargs: object = None    # None | int | "*" | "+"
    group: str = ""         # section title ("" = the main section)
    exclusive: int = -1     # index of its mutually exclusive set, -1 = none

    @property
    def positional(self) -> bool:
        return not self.flags

    @property
    def flag(self) -> str:
        """Long option string (or the dest for a positional)."""
        if not self.flags:
            return self.dest
        longs = [f for f in self.flags if f.startswith("--")]
        return (longs or list(self.flags))[0]

    @property
    def multi(self) -> bool:
        return self.nargs is not None or self.kind == "append"


@dataclass
class FormSpec:
    prog: str
    description: str = ""
    fields: list = field(default_factory=list)
    groups: dict = field(default_factory=dict)      # title -> description
    exclusive: list = field(default_factory=list)   # list of [dest, ...]
    exclusive_required: list = field(default_factory=list)  # parallel to `exclusive`
    sub_dest: str = ""
    sub_required: bool = False
    subs: dict = field(default_factory=dict)        # command -> FormSpec

    def all_fields(self):
        """Every field in this spec and, recursively, its subcommands."""
        yield from self.fields
        for s in self.subs.values():
            yield from s.all_fields()

    def walk(self, path=()):
        """Yield (command path, FormSpec) for this spec and every subcommand."""
        yield path, self
        for name, s in self.subs.items():
            yield from s.walk(path + (name,))


def _kind(a: argparse.Action) -> str:
    if isinstance(a, argparse._StoreTrueAction):
        return "bool"
    if isinstance(a, argparse._AppendAction):
        return "append"
    if a.choices:
        return "choice"
    if a.type in (int, float):
        return a.type.__name__
    return "str"


def _help(parser, a) -> str:
    if not a.help or a.help == argparse.SUPPRESS:
        return ""
    try:
        return parser._get_formatter()._expand_help(a)     # %% and %(default)s
    except Exception:
        return a.help.replace("%%", "%")


def introspect(parser: argparse.ArgumentParser, prog: str = "") -> FormSpec:
    prog = prog or parser.prog
    spec = FormSpec(prog=prog, description=parser.description or "")
    group_of, group_desc = {}, {}
    for g in parser._action_groups:
        # the two default groups hold everything not explicitly grouped
        title = "" if g.title in ("positional arguments", "options", "optional arguments") else g.title
        if title:
            group_desc[title] = g.description or ""
        for a in g._group_actions:
            group_of[id(a)] = title
    excl = {}
    for i, mg in enumerate(parser._mutually_exclusive_groups):
        spec.exclusive.append([a.dest for a in mg._group_actions])
        spec.exclusive_required.append(bool(mg.required))
        for a in mg._group_actions:
            excl[id(a)] = i
    for a in parser._actions:
        if isinstance(a, argparse._HelpAction):
            continue
        if isinstance(a, argparse._SubParsersAction):
            spec.sub_dest = a.dest
            spec.sub_required = bool(a.required)
            for name, sp in a.choices.items():
                spec.subs[name] = introspect(sp, f"{prog} {name}")
            continue
        title = group_of.get(id(a), "")
        if title:
            spec.groups.setdefault(title, group_desc.get(title, ""))
        default = a.default
        if default == argparse.SUPPRESS:
            default = None
        spec.fields.append(FieldSpec(
            dest=a.dest, flags=tuple(a.option_strings), kind=_kind(a), default=default,
            choices=tuple(a.choices or ()), required=bool(a.required) or not a.option_strings,
            help=_help(parser, a), metavar=str(a.metavar or ""), nargs=a.nargs,
            group=title, exclusive=excl.get(id(a), -1)))
    return spec
