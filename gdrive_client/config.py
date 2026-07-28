"""Text config stored at ~/.gdrive-settings.

The setting names here are deliberately semantic rather than named after any one
tool's flags, because the app has to speak to both rsync-style and rclone-style
back-ends.  Every option carries the rsync flag it corresponds to in
RSYNC_EQUIVALENT so the Preferences dialog and the README can show the mapping;
see docs/rsync-findings.md for why each one earned its place.
"""

from __future__ import annotations

import configparser
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("GDRIVE_SETTINGS", Path.home() / ".gdrive-settings"))

# Semantic option -> (rsync flag, rclone flag).  Used by the Preferences dialog
# tooltips and by `gdrive-client explain`.  Empty string == no equivalent.
RSYNC_EQUIVALENT = {
    "interval_minutes": ("(cron/systemd timer)", "(app timer)"),
    "conflict_policy": ("(none - rsync silently loses one side)", "bisync --conflict-resolve"),
    "conflict_loser": ("--backup --suffix", "bisync --conflict-loser/--conflict-suffix"),
    "backup_dir_local": ("--backup-dir", "bisync --backup-dir1"),
    "backup_dir_remote": ("--backup-dir", "bisync --backup-dir2"),
    "dry_run": ("--dry-run, -n", "--dry-run"),
    "max_delete_percent": ("--max-delete", "bisync --max-delete (percent)"),
    "remove_empty_dirs": ("--prune-empty-dirs", "bisync --remove-empty-dirs"),
    "transfers": ("(none - single stream)", "--transfers"),
    "checkers": ("(none)", "--checkers"),
    "bwlimit_up": ("--bwlimit", "--bwlimit"),
    "bwlimit_down": ("--bwlimit", "--bwlimit"),
    "compare": ("--checksum / --size-only / default", "--checksum / --size-only"),
    "modify_window": ("--modify-window", "--modify-window"),
    "retries": ("(none, needs a shell retry loop)", "--retries"),
    "exclude": ("--exclude / --exclude-from", "--filter-from"),
    "max_size": ("--max-size", "--max-size"),
    "preserve_symlinks": ("-l / --links", "--links"),
    "check_access": ("(none)", "bisync --check-access"),
    "resilient": ("(none)", "bisync --resilient"),
    "recover": ("(none)", "bisync --recover"),
}

DEFAULT_EXCLUDES = [
    ".~lock.*",
    "*~",
    "*.tmp",
    "*.swp",
    ".Trash-*",
    ".goutputstream-*",
    "lost+found",
    ".DS_Store",
    "desktop.ini",
    ".gdrive-trash/**",
]

CONFLICT_POLICIES = ("keep-both", "newer-wins", "prefer-local", "prefer-remote")
CONFLICT_LOSERS = ("keep-suffixed", "numbered", "delete")
COMPARE_MODES = ("size,modtime", "checksum", "size-only")
RESYNC_MODES = ("newer", "older", "larger", "smaller", "path1", "path2")

# conflict_policy -> rclone --conflict-resolve value.
_RESOLVE_MAP = {
    "keep-both": "none",
    "newer-wins": "newer",
    "prefer-local": "path1",
    "prefer-remote": "path2",
}
# conflict_loser -> rclone --conflict-loser value.
_LOSER_MAP = {
    "keep-suffixed": "pathname",
    "numbered": "num",
    "delete": "delete",
}

TEMPLATE = """\
# Configuration for gdrive-client.  Plain text; edit by hand or via the tray's
# Preferences dialog.  Lines starting with # are comments.
#
# Every value below was chosen after measuring real behaviour against this
# Drive -- see docs/rsync-findings.md.  The "rsync:" notes show the flag each
# setting corresponds to if you are used to rsync.

[account]
# rclone remote name, including the trailing colon.
remote = {remote}
# Optional subfolder inside the remote to treat as the sync root ("" = whole Drive).
remote_path = {remote_path}
# Local folder that mirrors the remote.
local_folder = {local_folder}

[sync]
# Master switch.  false = never sync, tray still works.
enabled = {enabled}
# Temporary pause, toggled from the tray menu.  Survives restarts on purpose.
paused = {paused}
# Minutes between automatic sync runs.
interval_minutes = {interval_minutes}
# Run a sync as soon as the app starts.
sync_on_start = {sync_on_start}
# Which version wins when a file changed on BOTH sides since the last run.
#   keep-both     - no winner; both versions kept side by side (no data loss)
#   newer-wins    - the newer mtime wins
#   prefer-local  - the local copy always wins
#   prefer-remote - the Drive copy always wins
# Plain rsync has no equivalent: it silently overwrites one side.
conflict_policy = {conflict_policy}
# What happens to the version that did NOT win:
#   keep-suffixed - kept next to the winner, renamed with conflict_suffix
#   numbered      - kept as name.1, name.2, ...
#   delete        - discarded (still recoverable from backup_dir_* if set)
# rsync: --backup --suffix
conflict_loser = {conflict_loser}
# Suffix(es) for keep-suffixed.  One value, or two comma-separated values to
# label the local and Drive sides differently.
conflict_suffix = {conflict_suffix}
# Where overwritten and deleted files are kept, per side.  Empty = disabled.
# These must not sit inside local_folder.  Drive keeps its own trash, so the
# remote one is off by default.  rsync: --backup-dir
backup_dir_local = {backup_dir_local}
backup_dir_remote = {backup_dir_remote}
# Global safety: report what would happen but change nothing.  rsync: --dry-run
dry_run = {dry_run}
# Abort the run if more than this PERCENT of files would be deleted.  This is
# the guard the old gdrive script lacked.  rsync: --max-delete (a count there)
max_delete_percent = {max_delete_percent}
# Require a sentinel RCLONE_TEST file on both sides before syncing; protects
# against syncing into an unmounted or accidentally empty folder.
check_access = {check_access}
# Delete directories that become empty.  rsync: --prune-empty-dirs
remove_empty_dirs = {remove_empty_dirs}
# Retry after less-serious errors instead of demanding a manual full resync.
# Both strongly recommended for an unattended tray app.
resilient = {resilient}
recover = {recover}
# How the one-time first run (the "resync") decides between two versions of the
# same path that already exist on both sides.  rclone's own default is path1,
# which would push a stale local copy over a newer file on Drive; "newer" is
# safer when the local folder was seeded by an older tool.
#   newer | older | larger | smaller | path1 | path2
resync_mode = {resync_mode}

[transfer]
# Parallel file transfers.  rsync is single-stream; this is a real win over it.
transfers = {transfers}
# Parallel metadata checks.
checkers = {checkers}
# Upload/download ceilings, e.g. 2M, 500k.  Empty = unlimited.  rsync: --bwlimit
bwlimit_up = {bwlimit_up}
bwlimit_down = {bwlimit_down}
# How to decide a file changed:
#   size,modtime - default, cheap and correct for normal use
#   checksum     - hash both sides; slow but immune to clock skew (rsync: -c)
#   size-only    - fastest, unsafe: misses same-size edits (rsync: --size-only)
compare = {compare}
# Timestamps closer than this count as equal.  Measured: the Drive FUSE mount
# reports whole seconds while ext4 stores nanoseconds, so 0 causes endless
# re-transfers.  rsync: --modify-window
modify_window = {modify_window}
# Retry a failed run this many times before reporting an error.
retries = {retries}

[filters]
# One glob per line, indented.  Applied to both directions.
# rsync: --exclude / --exclude-from
exclude =
{exclude_block}
# Skip files larger than this, e.g. 1G.  Empty = no limit.  rsync: --max-size
max_size = {max_size}
# Copy symlinks as symlinks rather than following them.  rsync: -l
preserve_symlinks = {preserve_symlinks}

[ui]
# How many entries the "Recently changed" tray submenu shows.
recent_files_count = {recent_files_count}
show_notifications = {show_notifications}
notify_on_conflict = {notify_on_conflict}
notify_on_error = {notify_on_error}
# Start the app automatically on login (managed via the autostart .desktop file).
autostart = {autostart}

[advanced]
# Override the rclone binary.  Empty = autodetect, and download a current
# static build if the system one is too old for bisync.
rclone_path = {rclone_path}
# Extra flags appended verbatim to every rclone invocation.
extra_flags = {extra_flags}
# INFO or DEBUG.  Quieter levels are not offered: rclone reports per-file
# actions at INFO, and without them the "Recently changed" menu stays empty.
log_level = {log_level}
"""


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


@dataclass
class Config:
    """In-memory view of ~/.gdrive-settings."""

    remote: str = ""
    remote_path: str = ""
    local_folder: str = str(Path.home() / "GoogleDrive")

    enabled: bool = True
    paused: bool = False
    interval_minutes: int = 15
    sync_on_start: bool = True
    conflict_policy: str = "keep-both"
    conflict_loser: str = "keep-suffixed"
    conflict_suffix: str = "path1,path2"
    backup_dir_local: str = str(Path.home() / ".gdrive-trash")
    backup_dir_remote: str = ""
    dry_run: bool = False
    max_delete_percent: int = 50
    check_access: bool = False
    remove_empty_dirs: bool = False
    resilient: bool = True
    recover: bool = True
    resync_mode: str = "newer"

    transfers: int = 4
    checkers: int = 8
    bwlimit_up: str = ""
    bwlimit_down: str = ""
    compare: str = "size,modtime"
    modify_window: str = "1s"
    retries: int = 3

    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_size: str = ""
    preserve_symlinks: bool = True

    recent_files_count: int = 10
    show_notifications: bool = True
    notify_on_conflict: bool = True
    notify_on_error: bool = True
    autostart: bool = True

    rclone_path: str = ""
    extra_flags: str = ""
    log_level: str = "INFO"

    # ---------------------------------------------------------------- paths

    @property
    def local_path(self) -> Path:
        return Path(os.path.expanduser(self.local_folder)).resolve()

    @property
    def backup_local_path(self) -> Path | None:
        raw = self.backup_dir_local.strip()
        return Path(os.path.expanduser(raw)) if raw else None

    @property
    def resolve_flag(self) -> str:
        return _RESOLVE_MAP.get(self.conflict_policy, "none")

    @property
    def loser_flag(self) -> str:
        return _LOSER_MAP.get(self.conflict_loser, "pathname")

    @property
    def remote_spec(self) -> str:
        """Full rclone remote spec, e.g. 'name:' or 'name:sub/dir'."""
        remote = self.remote if self.remote.endswith(":") or "/" in self.remote else self.remote + ":"
        sub = self.remote_path.strip("/")
        return f"{remote}{sub}" if sub else remote

    # ------------------------------------------------------------ validate

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means usable."""
        problems: list[str] = []
        if not self.remote.strip():
            problems.append("account.remote is empty - set an rclone remote name.")
        if not self.local_folder.strip():
            problems.append("account.local_folder is empty.")
        if self.conflict_policy not in CONFLICT_POLICIES:
            problems.append(
                f"sync.conflict_policy '{self.conflict_policy}' is not one of "
                f"{', '.join(CONFLICT_POLICIES)}."
            )
        if self.conflict_loser not in CONFLICT_LOSERS:
            problems.append(
                f"sync.conflict_loser '{self.conflict_loser}' is not one of "
                f"{', '.join(CONFLICT_LOSERS)}."
            )
        if self.conflict_policy == "keep-both" and self.conflict_loser == "delete":
            problems.append(
                "sync.conflict_loser=delete with conflict_policy=keep-both would "
                "delete BOTH versions of a conflicted file. Pick another combination."
            )
        if self.compare not in COMPARE_MODES:
            problems.append(
                f"transfer.compare '{self.compare}' is not one of {', '.join(COMPARE_MODES)}."
            )
        if self.resync_mode not in RESYNC_MODES:
            problems.append(
                f"sync.resync_mode '{self.resync_mode}' is not one of {', '.join(RESYNC_MODES)}."
            )
        if self.interval_minutes < 1:
            problems.append("sync.interval_minutes must be at least 1.")
        if not 0 <= self.max_delete_percent <= 100:
            problems.append("sync.max_delete_percent must be between 0 and 100.")
        if self.transfers < 1:
            problems.append("transfer.transfers must be at least 1.")
        if self.checkers < 1:
            problems.append("transfer.checkers must be at least 1.")
        if self.recent_files_count < 0:
            problems.append("ui.recent_files_count cannot be negative.")
        local = self.local_path
        if local == Path.home():
            problems.append("account.local_folder must not be your home directory itself.")

        # rclone refuses overlapping --backup-dir1, and silently syncing your
        # backups back into Drive would be worse than refusing.
        backup = self.backup_local_path
        if backup is not None:
            backup = Path(os.path.abspath(backup))
            if backup == local or local in backup.parents or backup in local.parents:
                problems.append(
                    f"sync.backup_dir_local '{backup}' overlaps account.local_folder "
                    f"'{local}'. Move it outside the synced folder."
                )
        return problems

    # ---------------------------------------------------------------- I/O

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        if not path.exists():
            return cfg

        parser = configparser.ConfigParser(interpolation=None)
        # Keep going on a malformed file rather than leaving the user with no tray.
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error:
            return cfg

        def get(section: str, option: str, current):
            if not parser.has_option(section, option):
                return current
            raw = parser.get(section, option)
            if isinstance(current, bool):
                return raw.strip().lower() in ("true", "yes", "1", "on")
            if isinstance(current, int):
                try:
                    return int(raw.strip())
                except ValueError:
                    return current
            if isinstance(current, list):
                return [ln.strip() for ln in raw.splitlines() if ln.strip()]
            return raw.strip()

        cfg.remote = get("account", "remote", cfg.remote)
        cfg.remote_path = get("account", "remote_path", cfg.remote_path)
        cfg.local_folder = get("account", "local_folder", cfg.local_folder)

        for opt in (
            "enabled", "paused", "interval_minutes", "sync_on_start", "conflict_policy",
            "conflict_loser", "conflict_suffix", "backup_dir_local", "backup_dir_remote",
            "dry_run", "max_delete_percent", "check_access", "remove_empty_dirs",
            "resilient", "recover", "resync_mode",
        ):
            setattr(cfg, opt, get("sync", opt, getattr(cfg, opt)))

        for opt in (
            "transfers", "checkers", "bwlimit_up", "bwlimit_down", "compare",
            "modify_window", "retries",
        ):
            setattr(cfg, opt, get("transfer", opt, getattr(cfg, opt)))

        for opt in ("exclude", "max_size", "preserve_symlinks"):
            setattr(cfg, opt, get("filters", opt, getattr(cfg, opt)))

        for opt in (
            "recent_files_count", "show_notifications", "notify_on_conflict",
            "notify_on_error", "autostart",
        ):
            setattr(cfg, opt, get("ui", opt, getattr(cfg, opt)))

        for opt in ("rclone_path", "extra_flags", "log_level"):
            setattr(cfg, opt, get("advanced", opt, getattr(cfg, opt)))

        return cfg

    def render(self) -> str:
        exclude_block = "\n".join(f"    {pat}" for pat in self.exclude) or "    "
        values = {
            k: (_fmt_bool(v) if isinstance(v, bool) else v)
            for k, v in vars(self).items()
            if k != "exclude"
        }
        return TEMPLATE.format(exclude_block=exclude_block, **values)

    def save(self, path: Path | None = None) -> Path:
        """Write atomically, preserving a .bak of the previous version."""
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gdrive-settings.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.render())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path
