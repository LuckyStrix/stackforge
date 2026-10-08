"""Keep the test run out of the user's real config: settings, presets and the user copy of
the filament database (core/paths.user_data_path) go to a throwaway folder."""
import os
import tempfile

_cfg = tempfile.TemporaryDirectory(prefix="stackforge-test-config-")
os.environ["XDG_CONFIG_HOME"] = _cfg.name
