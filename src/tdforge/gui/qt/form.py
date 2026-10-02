"""CommandForm: one command's generated form (scrolling sections, exclusion, live command line)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton,
                               QVBoxLayout, QWidget)

from tdforge.gui.argform import argv as av
from tdforge.gui.argform import overrides
from tdforge.gui.argform.spec import FormSpec
from tdforge.gui.qt import theme, widgets


class CollapsibleSection(QWidget):
    """A titled block that folds away; fill `.form` (a QFormLayout)."""

    def __init__(self, title, description="", collapsed=True):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.btn = QToolButton()
        self.btn.setObjectName("section")
        self.btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.RightArrow)
        self.btn.setText(title)
        self.btn.setCheckable(True)
        lay.addWidget(self.btn)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(14, 0, 0, 4)
        if description:
            bl.addWidget(theme.hint(description))
        self.form = _form_layout()
        bl.addLayout(self.form)
        lay.addWidget(self.body)
        self.btn.toggled.connect(self._sync)
        self.btn.setChecked(not collapsed)
        self._sync(not collapsed)

    def _sync(self, open_):
        self.btn.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow)
        self.body.setVisible(open_)


def _form_layout() -> QFormLayout:
    f = QFormLayout()
    f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    f.setRowWrapPolicy(QFormLayout.DontWrapRows)
    f.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    f.setVerticalSpacing(8)
    return f


class _Entry:
    """Everything the form tracks about one field."""

    def __init__(self, field, kind, widget, label, spec):
        self.field, self.kind, self.widget, self.label, self.spec = field, kind, widget, label, spec
        self.project_key = overrides.project_key(field) if overrides.is_project_kind(kind) else None
        self.unlocked = False
        self.layout = None          # QFormLayout and row, for show/hide
        self.row = -1
        self.visible = True


class CommandForm(QWidget):
    """Form for `command` (a path of subcommand names, () for a tool without any).

    `project` is anything with .get(key) -> str | None; bound fields show its value read-only
    ("from project") until overridden. `on_change(form)` fires after every edit.
    """

    def __init__(self, spec: FormSpec, tool: str, command: tuple = (), project=None,
                 on_change=None, parent=None):
        super().__init__(parent)
        self.root_spec, self.tool, self.command = spec, tool, tuple(command)
        self.project, self.on_change = project, on_change
        self.entries: dict[str, _Entry] = {}
        self._busy = True
        chain, cur = [spec], spec
        for name in self.command:
            cur = cur.subs[name]
            chain.append(cur)
        self.chain, self.leaf = chain, chain[-1]
        self.prog = tool.replace("_", "-")      # the console script; subcommands are in argv
        self._build()
        self._busy = False
        self.refresh_project()

    # ---- construction ------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(2, 2, 8, 2)
        col.setSpacing(6)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        cmd = " ".join(self.command)
        main = QGroupBox(cmd or self.tool)
        mlay = QVBoxLayout(main)
        if self.leaf.description:
            mlay.addWidget(theme.hint(self.leaf.description.split("\n\n")[0]))
        mform = _form_layout()
        mlay.addLayout(mform)
        col.addWidget(main)
        forms = {"": mform}
        for sp in self.chain:
            for f in sp.fields:
                kind = overrides.resolve_kind(self.tool, cmd, f)
                if kind == "hide":
                    continue
                group = overrides.group_for(self.tool, cmd, f)
                if group not in forms:
                    required = any(x.required for x in sp.fields
                                   if overrides.group_for(self.tool, cmd, x) == group)
                    cs = CollapsibleSection(group, sp.groups.get(group, ""), collapsed=not required)
                    col.addWidget(cs)
                    forms[group] = cs.form
                self._add_field(forms[group], sp, f, kind)
        col.addStretch(1)

        # footer: status + the equivalent shell command (pinned below the scroll area)
        self._status = QLabel()
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)
        row = QHBoxLayout()
        self._cmdline = QLineEdit()
        self._cmdline.setReadOnly(True)
        theme.mono(self._cmdline)
        row.addWidget(self._cmdline, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._cmdline.text()))
        row.addWidget(copy)
        outer.addLayout(row)

    def _add_field(self, form, sp, f, kind):
        w = widgets.build(f, kind, self._changed)
        if f.default not in (None, [], False) and f.kind != "bool":
            w.set(f.default)
        label = QLabel(f.flag + (" *" if f.required else ""))
        label.setToolTip(f.help)
        cell = QWidget()
        cl = QVBoxLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(w.widget, 1)
        e = _Entry(f, kind, w, label, sp)
        if e.project_key:
            ck = QCheckBox("override")
            ck.toggled.connect(lambda on, e=e: self._unlock(e, on))
            top.addWidget(ck)
        cl.addLayout(top)
        hint = f.help + ("  (from project)" if overrides.is_project_kind(kind) else "")
        if hint:
            cl.addWidget(theme.hint(hint))
        form.addRow(label, cell)
        e.layout, e.row = form, form.rowCount() - 1
        self.entries[f.dest] = e

    # ---- project binding ---------------------------------------------------------------
    def _unlock(self, e: _Entry, on: bool):
        e.unlocked = on
        self.refresh_project()

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
        self._status.setText("; ".join(errs) if errs else
                             ("needs: " + ", ".join(missing) if missing else ""))
        self._cmdline.setText(av.command_line(self.prog, av.build_argv(self.root_spec, vals, self.command)))
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
                e.layout.setRowVisible(e.row, on)

    def _apply_exclusion(self):
        for sp in self.chain:
            for dests in sp.exclusive:
                present = [d for d in dests if d in self.entries]
                chosen = [d for d in present if self._has_value(self.entries[d])]
                for d in present:
                    on = not chosen or d == chosen[0]     # the first chosen member wins
                    self.entries[d].widget.enable(on)
                    if not on:
                        e = self.entries[d]
                        e.widget.set(False if e.field.kind == "bool" else "")

    @staticmethod
    def _has_value(e: _Entry) -> bool:
        v = e.widget.get()
        return bool(v) if e.field.kind == "bool" else v not in ("", [], None)

    def leaf_view(self) -> FormSpec:
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
