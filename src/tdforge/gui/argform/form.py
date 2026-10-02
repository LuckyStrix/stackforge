"""CommandForm: one command's generated form (sections, exclusion, live command line)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tdforge.gui import theme
from tdforge.gui.argform import argv as av
from tdforge.gui.argform import overrides, widgets
from tdforge.gui.argform.spec import FormSpec


class _Entry:
    """Everything the form tracks about one field."""

    def __init__(self, field, kind, widget, label, section_spec):
        self.field, self.kind, self.widget, self.label = field, kind, widget, label
        self.project_key = overrides.project_key(field) if overrides.is_project_kind(kind) else None
        self.unlocked = False
        self.spec = section_spec
        self.rows: list = []         # grid widgets of this field's row(s), for show/hide
        self.visible = True


class CommandForm(ttk.Frame):
    """Form for `command` (a path of subcommand names, () for a tool without any).

    `project` is anything with .get(key) -> str | None; fields bound to it show its value
    read-only ("from project") until unlocked. Pass None for no binding.
    """

    def __init__(self, master, spec: FormSpec, tool: str, command: tuple = (),
                 project=None, on_change=None):
        super().__init__(master)
        self.root_spec, self.tool, self.command = spec, tool, tuple(command)
        self.project = project
        self.on_change = on_change
        self.entries: dict[str, _Entry] = {}
        self._busy = True
        self._cmdline = tk.StringVar()
        self._status = tk.StringVar()

        chain, cur = [spec], spec
        for name in self.command:
            cur = cur.subs[name]
            chain.append(cur)
        self.chain = chain
        self.leaf = chain[-1]
        self.prog = self.leaf.prog
        self._build()
        self._busy = False
        self.refresh_project()
        self._changed()

    # ---- construction ------------------------------------------------------------------
    def _build(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        cmd = " ".join(self.command)
        main = theme.Section(body, cmd or self.tool)
        main.pack(fill="x", pady=(0, 6))
        if self.leaf.description:
            main.note(self.leaf.description.split("\n\n")[0], wraplength=420)
        sections = {"": main}
        for sp in self.chain:
            for f in sp.fields:
                kind = overrides.resolve_kind(self.tool, cmd, f)
                if kind == "hide":
                    continue
                if f.group not in sections:
                    required = any(x.required for x in sp.fields if x.group == f.group)
                    cs = theme.CollapsibleSection(body, f.group, sp.groups.get(f.group, ""),
                                                  collapsed=not required)
                    cs.pack(fill="x")
                    sections[f.group] = cs.body
                sec = sections[f.group]
                label = f.flag + (" *" if f.required else "")
                w = widgets.build(sec, f, kind, self._changed)
                if f.default not in (None, [], False) and f.kind != "bool":
                    w.set(f.default)
                hint = f.help
                if overrides.is_project_kind(kind):
                    hint = (hint + "  " if hint else "") + "(from project)"
                first = sec._row
                sec.field(label, w.frame, hint or None)
                e = _Entry(f, kind, w, label, sp)
                e.rows = [x for r in range(first, sec._row) for x in sec.grid_slaves(row=r)]
                self.entries[f.dest] = e
                if e.project_key:
                    ck = ttk.Checkbutton(w.frame, text="override", command=lambda e=e: self._unlock(e))
                    ck.pack(side="left", padx=(6, 0))
        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(4, 0))
        ttk.Label(foot, textvariable=self._status, style="Hint.TLabel").pack(anchor="w")
        row = ttk.Frame(foot)
        row.pack(fill="x")
        ent = ttk.Entry(row, textvariable=self._cmdline, state="readonly")
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Copy", width=6, command=self._copy).pack(side="left", padx=(4, 0))

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._cmdline.get())

    # ---- project binding ---------------------------------------------------------------
    def _unlock(self, e: _Entry):
        e.unlocked = not e.unlocked
        self.refresh_project()
        self._changed()

    def refresh_project(self):
        """Re-read every project-bound field from the project (call when the bar changes)."""
        self._busy = True
        for e in self.entries.values():
            if not e.project_key:
                continue
            pv = self.project.get(e.project_key) if self.project else None
            if pv is not None and not e.unlocked:
                e.widget.set(pv)
                e.widget.readonly(True)
            else:
                e.widget.readonly(False)
        self._busy = False
        self._changed()

    # ---- state -------------------------------------------------------------------------
    def _changed(self, *_):
        if self._busy:
            return
        self._busy = True
        try:
            self._apply_exclusion()
            self._apply_visibility()
        finally:
            self._busy = False
        vals, errs = self.collect()
        missing = av.missing_required(self.leaf_view(), vals) if not errs else []
        self._status.set("; ".join(errs) if errs else
                         ("needs: " + ", ".join(missing) if missing else ""))
        self._cmdline.set(av.command_line(self.prog, av.build_argv(self.root_spec, vals, self.command)))
        if self.on_change:
            self.on_change(self)

    def _apply_visibility(self):
        """Show a field only when the field that selects it has a matching value."""
        for dest, e in self.entries.items():
            rule = overrides.VISIBLE_WHEN.get((self.tool, dest))
            if rule is None:
                continue
            ctrl = self.entries.get(rule[0])
            on = ctrl is None or ctrl.widget.get() in rule[1]
            if on != e.visible:
                e.visible = on
                for w in e.rows:
                    w.grid() if on else w.grid_remove()

    def _apply_exclusion(self):
        for sp in self.chain:
            for dests in sp.exclusive:
                present = [d for d in dests if d in self.entries]
                chosen = [d for d in present if self._has_value(self.entries[d])]
                for d in present:
                    # the first chosen member wins; the others are switched off
                    on = not chosen or d == chosen[0]
                    self.entries[d].widget.enable(on)
                    if not on:
                        self._clear(self.entries[d])

    @staticmethod
    def _has_value(e: _Entry) -> bool:
        v = e.widget.get()
        return bool(v) if e.field.kind == "bool" else v not in ("", [], None)

    @staticmethod
    def _clear(e: _Entry):
        e.widget.set(False if e.field.kind == "bool" else "")

    def leaf_view(self) -> FormSpec:
        """A FormSpec of every field on this form, for required checks."""
        merged = FormSpec(prog=self.prog)
        for sp in self.chain:
            merged.fields += sp.fields
            merged.exclusive += sp.exclusive
            merged.exclusive_required += sp.exclusive_required
        return merged

    def collect(self):
        """-> ({dest: typed value}, [error strings])."""
        vals, errs = {}, []
        for dest, e in self.entries.items():
            if not e.visible:           # a field the chosen pattern does not use is not sent
                continue
            try:
                vals[dest] = av.coerce(e.field, e.widget.get())
            except ValueError as ex:
                errs.append(str(ex))
        return vals, errs

    # ---- public API --------------------------------------------------------------------
    def values(self) -> dict:
        return self.collect()[0]

    def argv(self) -> list:
        return av.build_argv(self.root_spec, self.values(), self.command)

    def validate(self) -> list:
        vals, errs = self.collect()
        if errs:
            return errs
        return [f"{m} is required" for m in av.missing_required(self.leaf_view(), vals)]

    def preset_values(self) -> dict:
        """values() minus project-bound fields that follow the project bar."""
        vals = self.values()
        for dest, e in self.entries.items():
            if e.project_key and not e.unlocked:
                vals.pop(dest, None)
        return vals

    def set_values(self, values: dict) -> list:
        """Load {dest: value}; returns the dests that no longer exist (skipped)."""
        skipped = []
        self._busy = True
        for dest, v in values.items():
            e = self.entries.get(dest)
            if e is None:
                skipped.append(dest)
                continue
            e.widget.set(bool(v) if e.field.kind == "bool" else v)
        self._busy = False
        self._changed()
        return skipped
