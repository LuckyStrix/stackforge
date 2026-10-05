"""TerminalView: scrolling output plus an input line wired to a Job's stdin.

measure's own spotread pty session is internal to the measure process; this outer pipe only
carries its input() prompts ("press Enter after placing the chip").
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from stackforge.gui import theme
from stackforge.gui.runner import Job, Screen

POLL_MS = 50


class TerminalView(QWidget):
    def __init__(self, on_finish=None, parent=None):
        super().__init__(parent)
        self.on_finish = self._on_finish = on_finish
        self.job: Job | None = None
        self.screen = Screen()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setMinimumHeight(80)
        theme.mono(self.text)
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("input for the running command (Enter to send)")
        self.entry.returnPressed.connect(self._send)
        QShortcut(QKeySequence("Ctrl+C"), self.entry, activated=self.cancel)
        row.addWidget(self.entry, 1)
        self.send_btn = QPushButton("Enter")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        copy = QPushButton("Copy log")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.screen.text))
        row.addWidget(copy)
        lay.addLayout(row)
        self.status = QLabel("idle")
        self.status.setObjectName("hint")
        lay.addWidget(self.status)

    # ---- job lifecycle -----------------------------------------------------------------
    def attach(self, job: Job, on_finish=None):
        self.job = job
        self._on_finish = on_finish or self.on_finish
        self.screen = Screen()
        self._render()
        self.entry.setFocus()       # "press Enter after placing the chip" is one keystroke
        self._timer.start()

    def cancel(self):
        if self.job and not self.job.done:
            self.job.cancel()

    def clear(self):
        self.screen = Screen()
        self._render()

    def _send(self):
        if not self.job or self.job.done:
            return
        line = self.entry.text()
        self.entry.clear()
        self.screen.write(line + "\n")      # echo, as a terminal would
        self._render()
        self.job.send(line)

    def _poll(self):
        job = self.job
        if job is None:
            self._timer.stop()
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
            self.status.setText(f"{state} · {job.elapsed:.1f}s")
            self._timer.stop()
            if self._on_finish:
                self._on_finish(job)
            return
        self.status.setText(f"running · {job.elapsed:.0f}s")

    def _render(self):
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.text.setPlainText(self.screen.text)
        if at_end:
            bar.setValue(bar.maximum())
