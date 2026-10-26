# PyInstaller spec for the stackforge app: `pip install -e ".[gui,release]"`, then
# `pyinstaller packaging/stackforge.spec` from the repo root. Output lands in dist/:
# a single stackforge.exe (Windows) / stackforge (Linux), or stackforge.app (macOS, where a
# windowed one-file build is deprecated).
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# The tools are imported by name (gui/runner.py TOOL_MODULES), which the analysis cannot see.
hidden = collect_submodules("stackforge")
datas = collect_data_files("stackforge")     # data/*.json, data/cie/*

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(SPECPATH, "..", "src")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="stackforge", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="stackforge")
    app = BUNDLE(coll, name="stackforge.app", bundle_identifier="io.github.stackforge")
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="stackforge", console=False,
              upx=False)
