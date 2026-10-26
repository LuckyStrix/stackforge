"""Entry point of the bundled app (packaging/stackforge.spec).

The GUI runs every tool as a subprocess; in a bundle that subprocess is this executable
again with `--run-tool <tool> argv` (gui/runner.py), dispatched here before Qt is imported.
"""
import sys

from stackforge.gui import runner

if __name__ == "__main__":
    if sys.argv[1:2] == [runner.RUN_TOOL]:
        sys.exit(runner.run_tool(sys.argv[2], sys.argv[3:]))
    from stackforge.gui.app import main
    sys.exit(main())
