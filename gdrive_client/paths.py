"""Well-known locations, following the XDG basedir spec."""

from __future__ import annotations

import os
from pathlib import Path

APP = "gdrive-client"


def _base(env: str, default: Path) -> Path:
    raw = os.environ.get(env)
    return Path(raw) if raw else default


DATA_DIR = _base("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP
CACHE_DIR = _base("XDG_CACHE_HOME", Path.home() / ".cache") / APP
STATE_DIR = _base("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP
CONFIG_DIR = _base("XDG_CONFIG_HOME", Path.home() / ".config") / APP

BIN_DIR = DATA_DIR / "bin"
BISYNC_WORKDIR = DATA_DIR / "bisync"
FILTER_FILE = CACHE_DIR / "filters.txt"
HISTORY_FILE = STATE_DIR / "history.json"
STATUS_FILE = STATE_DIR / "status.json"
# Records which local/remote pair has a bisync baseline; see engine.needs_initial_resync.
BASELINE_FILE = STATE_DIR / "baseline.json"
LOG_FILE = STATE_DIR / "sync.log"
LOCK_FILE = STATE_DIR / "sync.lock"
ACCOUNT_CACHE = CACHE_DIR / "account.json"

AUTOSTART_FILE = _base("XDG_CONFIG_HOME", Path.home() / ".config") / "autostart" / f"{APP}.desktop"

DRIVE_WEB_URL = "https://drive.google.com/drive/my-drive"


def ensure_dirs() -> None:
    for directory in (DATA_DIR, CACHE_DIR, STATE_DIR, BIN_DIR, BISYNC_WORKDIR):
        directory.mkdir(parents=True, exist_ok=True)
