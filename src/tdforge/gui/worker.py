"""Worker plumbing: one background job at a time, reporting through a queue."""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass



@dataclass
class Job:
    """A unit of background work and whatever it produced."""

    kind: str
    result: object = None
    error: str = ""


class Worker:
    """Runs one job at a time on a thread, reporting through a queue.

    Cancellation is cooperative: `cancel()` sets a flag that progress
    callbacks check, so a job stops at its next checkpoint rather than being
    killed mid-write.
    """

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def submit(self, kind, fn):
        if self.busy:
            return False
        self._cancel.clear()

        def run():
            try:
                self.q.put(Job(kind, fn()))
            except Cancelled:
                self.q.put(Job("cancelled"))
            except BaseException:
                # SystemExit too: filamentdb/td3mf report bad data that way,
                # and an uncaught one kills this thread with the UI still busy.
                self.q.put(Job(kind, error=traceback.format_exc()))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return True

    def progress(self, frac, msg):
        if self._cancel.is_set():
            raise Cancelled()
        self.q.put(Job("progress", (frac, msg)))


class Cancelled(Exception):
    pass
