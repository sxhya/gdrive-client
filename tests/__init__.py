"""Test package.

Importing this redirects every path in gdrive_client.paths into a throwaway
temp directory, before any test module gets a chance to run.  An earlier
ad-hoc test run wrote its own baseline marker and listings into the real
~/.local/state/gdrive-client, which then made the app think the live Drive pair
had already been baselined against two /tmp directories.  This makes that class
of mistake impossible rather than relying on each test to remember.
"""

import atexit
import shutil
import tempfile
from pathlib import Path

from gdrive_client import paths as _paths

_SANDBOX = Path(tempfile.mkdtemp(prefix="gdrive-client-tests-"))

_paths.DATA_DIR = _SANDBOX / "data"
_paths.CACHE_DIR = _SANDBOX / "cache"
_paths.STATE_DIR = _SANDBOX / "state"
_paths.CONFIG_DIR = _SANDBOX / "config"
_paths.BIN_DIR = _paths.DATA_DIR / "bin"
_paths.BISYNC_WORKDIR = _paths.DATA_DIR / "bisync"
_paths.FILTER_FILE = _paths.CACHE_DIR / "filters.txt"
_paths.HISTORY_FILE = _paths.STATE_DIR / "history.json"
_paths.STATUS_FILE = _paths.STATE_DIR / "status.json"
_paths.BASELINE_FILE = _paths.STATE_DIR / "baseline.json"
_paths.LOG_FILE = _paths.STATE_DIR / "sync.log"
_paths.LOCK_FILE = _paths.STATE_DIR / "sync.lock"
_paths.ACCOUNT_CACHE = _paths.CACHE_DIR / "account.json"
_paths.AUTOSTART_FILE = _paths.CONFIG_DIR / "autostart" / "gdrive-client.desktop"

_paths.ensure_dirs()

atexit.register(lambda: shutil.rmtree(_SANDBOX, ignore_errors=True))
