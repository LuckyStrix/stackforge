"""TerminalView: scrolling output plus an input line wired to a Job's stdin.

munki's own spotread pty session is internal to the munki process; this outer pipe only
carries its input() prompts ("press Enter after placing the chip").
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tdforge.gui import theme
from tdforge.gui.run.runner import Job, Screen

POLL_MS = 50


class TerminalView(ttk.Frame):
    def __init__(self, master, on_finish=None):
        super().__init__(master)
        self.on_finish = on_finish
        self.job: Job | None = None
        self.screen = Screen()
        self._status = tk.StringVar(value="idle")
        self.text = theme.text_view(self, height=14)
        self.text.config(state="disabled")
        bar = ttk.Frame(self)
        self.entry = ttk.Entry(bar)
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.bind("<Control-c>", lambda e: self.cancel())
        self.send_btn = ttk.Button(bar, text="Enter", width=8, command=self._send)
        ttk.Button(bar, text="Copy log", command=self._copy).pack(side="right", padx=(4, 0))
        self.send_btn.pack(side="right", padx=(4, 0))
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Label(self, textvariable=self._status, style="Hint.TLabel").pack(side="bottom", anchor="w")
        bar.pack(side="bottom", fill="x", pady=(4, 2))
        self.text.pack(side="top", fill="both", expand=True)

    # ---- job lifecycle -----------------------------------------------------------------
    def attach(self, job: Job):
        self.job = job
        self.screen = Screen()
        self._render()
        self.entry.focus_set()      # "press Enter after placing the chip" is one keystroke
        self._poll()

    def cancel(self):
        if self.job and not self.job.done:
            self.job.cancel()
        return "break"

    def clear(self):
        self.screen = Screen()
        self._render()

    def _send(self):
        if not self.job or self.job.done:
            return
        line = self.entry.get()
        self.entry.delete(0, "end")
        self.screen.write(line + "\n")      # echo, as a terminal would
        self._render()
        self.job.send(line)

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.screen.text)

    def _poll(self):
        job = self.job
        if job is None:
            return
        chunk = job.drain()
        if chunk:
            self.screen.write(chunk)
            self._render()
        if job.done:
            tail = job.drain()
            if tail:
                self.screen.write(tail)
                self._render()
            state = "cancelled" if job.cancelled else f"exit {job.returncode}"
            self._status.set(f"{state} · {job.elapsed:.1f}s")
            if self.on_finish:
                self.on_finish(job)
            return
        self._status.set(f"running · {job.elapsed:.0f}s")
        self.after(POLL_MS, self._poll)

    def _render(self):
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", self.screen.text)
        t.config(state="disabled")
        t.see("end")
