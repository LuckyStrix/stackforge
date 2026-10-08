"""Shared test setup.

Keep the test run out of the user's real config: settings, presets and the user copy of the
filament database (core/paths.user_data_path) go to a throwaway folder.

Delete every leftover widget while the QApplication still exists. The GUI tests build hundreds
of parentless widgets and leave them to the garbage collector; from PySide6 6.12 on, destroying
them during interpreter shutdown, in whatever order Python picks, segfaults after all tests
pass ("shared QObject was deleted directly"). The app itself closes its window first and is
not affected.
"""
import os
import tempfile

_cfg = tempfile.TemporaryDirectory(prefix="stackforge-test-config-")
os.environ["XDG_CONFIG_HOME"] = _cfg.name


def pytest_sessionfinish(session, exitstatus):
    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
