"""Run a CLI as a subprocess exactly as a user would.

`python -u -m stackforge.tools.<tool> <argv>`: the GUI cannot diverge from the CLI, and
SystemExit / ap.error come back as text plus an exit code. A reader thread feeds a queue
that the UI polls from its own thread.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time

TOOL_MODULES = {
    "plaque": "stackforge.tools.plaque", "top_paint": "stackforge.tools.top_paint",
    "paint": "stackforge.tools.paint", "calibrate": "stackforge.tools.calibrate",
    "measure": "stackforge.tools.measure", "polymaker": "stackforge.tools.polymaker",
    "dither_compare": "stackforge.tools.dither_compare",
    "make_samples": "stackforge.tools.make_samples", "filamentdb": "stackforge.core.filamentdb",
    "spectral": "stackforge.tools.spectral",
}

KILL_AFTER = 3.0
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def console_script(tool: str) -> str:
    """The installed command for a tool, as pyproject.toml names it."""
    return "stackforge-" + ("filaments" if tool == "filamentdb" else tool.replace("_", "-"))


def tool_command(tool: str, argv: list) -> list:
    if getattr(sys, "frozen", False):
        # A bundled build (PyInstaller) has no python to `-m` with: the app runs the tool
        # itself (packaging/launcher.py -> run_tool).
        return [sys.executable, RUN_TOOL, tool, *argv]
    return [sys.executable, "-u", "-m", TOOL_MODULES[tool], *argv]


RUN_TOOL = "--run-tool"


def run_tool(tool: str, argv: list) -> int:
    """`python -u -m <tool> argv` for a bundled build: unbuffered UTF-8 output (what Job
    decodes), then the tool's main()."""
    import importlib
    import io
    import traceback
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None:
            raw = open(stream.fileno(), "wb", buffering=0, closefd=False)
            setattr(sys, name, io.TextIOWrapper(raw, encoding="utf-8", errors="replace",
                                                write_through=True))
    sys.argv = [console_script(tool), *argv]
    try:
        rc = importlib.import_module(TOOL_MODULES[tool]).main()
    except SystemExit as exc:
        rc = exc.code
    except BaseException:
        # A windowed bundle has no console: left to the bootloader, the traceback would go
        # to a dialog instead of the pipe the GUI's terminal reads.
        traceback.print_exc()
        return 1
    if isinstance(rc, str):
        print(rc, file=sys.stderr)
        return 1
    return rc or 0


class Job:
    """One subprocess. Output arrives via `drain()`; `done` once it has exited and every
    byte has been read."""

    def __init__(self, cmd: list, cwd: str | None = None, env: dict | None = None):
        self.cmd = list(cmd)
        self.q: queue.Queue = queue.Queue()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.cancelled = False
        self._eof = threading.Event()
        self._proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, cwd=cwd, env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})})
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        fd = self._proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.q.put(chunk.decode("utf-8", "replace"))
        self._eof.set()

    def drain(self) -> str:
        """Everything received since the last call ('' if nothing)."""
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return "".join(out)

    def poll(self):
        """Exit code once finished (and output fully read), else None."""
        if self.returncode is None:
            rc = self._proc.poll()
            if rc is not None and self._eof.wait(0.2):
                self.returncode = rc
                self.ended = time.monotonic()
                for pipe in (self._proc.stdin, self._proc.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass
        return self.returncode

    @property
    def done(self) -> bool:
        return self.poll() is not None

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def send(self, line: str):
        """Write a line to the process's stdin (its input() prompts)."""
        try:
            self._proc.stdin.write((line + "\n").encode())
            self._proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def cancel(self):
        """terminate, then kill after KILL_AFTER seconds."""
        if self._proc.poll() is not None:
            return
        self.cancelled = True
        self._proc.terminate()

        def reaper():
            try:
                self._proc.wait(KILL_AFTER)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        threading.Thread(target=reaper, daemon=True).start()

    def wait(self, timeout: float = 30.0) -> int:
        end = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() > end:
                raise TimeoutError(" ".join(self.cmd))
            time.sleep(0.02)
        return self.returncode


class Screen:
    """Terminal-ish text model: strips ANSI, honours \\r (overwrite the line) and \\b."""

    def __init__(self):
        self.lines = [""]

    def write(self, chunk: str):
        chunk = _ANSI.sub("", chunk).replace("\r\n", "\n")
        for ch in chunk:
            if ch == "\n":
                self.lines.append("")
            elif ch == "\r":
                self.lines[-1] = ""
            elif ch == "\b":
                self.lines[-1] = self.lines[-1][:-1]
            else:
                self.lines[-1] += ch

    @property
    def text(self) -> str:
        return "\n".join(self.lines)
