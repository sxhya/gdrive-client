"""The sync engine: schedules and runs `rclone bisync`, tracks what changed.

Threading model: one worker thread owns every rclone subprocess, and a
scheduler thread only ever asks it to wake up.  Callbacks fire on the worker
thread, so GUI code must marshal them (the tray uses GLib.idle_add).

Deliberate design choices, both learned from the failure this app replaces:
  * A run that trips the --max-delete guard is reported, never auto-retried
    with --force.  Bulldozing a safety limit unattended is how the old script
    lost files.
  * The one-time --resync baseline is only taken automatically when there is no
    prior listing at all.  If a later run demands a resync, the tray asks first.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from . import paths, rclone
from .config import Config
from .parser import Change, SyncResult, parse_bisync_output

log = logging.getLogger(__name__)

HISTORY_LIMIT = 500


class State(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    SYNCING = "syncing"
    PAUSED = "paused"
    DISABLED = "disabled"
    ERROR = "error"
    NEEDS_RESYNC = "needs-resync"
    SAFETY_ABORT = "safety-abort"
    MISCONFIGURED = "misconfigured"
    BLOCKED = "blocked"

    @property
    def is_problem(self) -> bool:
        return self in (
            State.ERROR, State.NEEDS_RESYNC, State.SAFETY_ABORT,
            State.MISCONFIGURED, State.BLOCKED,
        )


@dataclass
class Status:
    state: State = State.STARTING
    message: str = ""
    last_sync_at: float = 0.0
    last_success_at: float = 0.0
    last_error: str = ""
    next_sync_at: float = 0.0
    pending_conflicts: list[str] = field(default_factory=list)
    blocked_files: list[str] = field(default_factory=list)
    rclone_version: str = ""
    bisync_experimental: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data


class AlreadyRunning(RuntimeError):
    """Another gdrive-client process holds the sync lock."""

    def __init__(self, message: str, pid: int | None = None):
        super().__init__(message)
        self.pid = pid


def lock_holder_pid() -> int | None:
    """Read the pid recorded in the lock file, if it still looks alive."""
    try:
        raw = paths.LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    try:
        os.kill(pid, 0)          # signal 0 only tests for existence
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid               # exists, owned by someone else
    return pid


def _acquire_lock() -> "object":
    """Take an exclusive flock so two instances can't bisync the same pair.

    Opened O_RDWR without O_TRUNC on purpose.  Using open(path, "w") would
    truncate the file *before* the flock attempt, so a second launch would erase
    the pid of the instance that legitimately holds the lock and the error
    message could no longer say which process to look for.  The file is only
    rewritten once the lock is actually ours.
    """
    paths.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        holder = lock_holder_pid()
        detail = f", held by pid {holder}" if holder else ""
        raise AlreadyRunning(
            f"another gdrive-client is already running{detail}.\n"
            f"  lock file: {paths.LOCK_FILE}\n"
            f"  stop it with: gdrive-client stop"
            + (f"   (or: kill {holder})" if holder else ""),
            pid=holder,
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _is_live_rclone(pid: int) -> bool:
    """True if pid is a running rclone process.

    Checking the command line, not just existence, matters because pids are
    reused: after a reboot the recorded pid can easily belong to something
    unrelated, and that must not keep the lock alive.
    """
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return False
    except OSError:
        # No /proc to consult: fall back to a bare existence test.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return "rclone" in os.path.basename(os.fsdecode(argv[0]))


def _bisync_lock_pid(lock: Path) -> int | None:
    """The pid recorded in a bisync .lck file (JSON since 1.66, bare pid before)."""
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if isinstance(value, dict):
        value = value.get("PID")
    value = str(value).strip()
    return int(value) if value.isdigit() else None


def clear_stale_bisync_locks(workdir: Path | None = None) -> list[Path]:
    """Delete bisync lock files whose owning rclone process is gone.

    bisync writes a .lck file for the duration of a run and removes it on the
    way out -- unless it never gets there.  If the app is killed mid-sync (a
    crash, logout, kill -9) rclone dies with it and leaves the lock behind.
    Without --max-lock that lock never expires, so every later run failed with
    "prior lock file found" until someone deleted it by hand.

    A lock is only removed when its pid is provably not a running rclone.  A
    live one is left alone: that is a genuinely concurrent run (e.g.
    `gdrive-client sync` from a terminal) and the lock is doing its job.  A lock
    whose pid can't be read is left alone too, rather than guessed at.
    """
    workdir = workdir or paths.BISYNC_WORKDIR
    removed: list[Path] = []
    for lock in workdir.glob("*.lck"):
        pid = _bisync_lock_pid(lock)
        if pid is None or _is_live_rclone(pid):
            continue
        try:
            lock.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("could not remove stale bisync lock %s: %s", lock, exc)
            continue
        log.warning("removed stale bisync lock %s (rclone pid %d is gone)", lock, pid)
        removed.append(lock)
    return removed


class SyncEngine:
    """Owns the sync loop.  Construct, then call start()."""

    def __init__(
        self,
        cfg: Config,
        info: rclone.RcloneInfo,
        on_status=None,
        on_changes=None,
        on_notify=None,
    ):
        self.cfg = cfg
        self.info = info
        self._on_status = on_status
        self._on_changes = on_changes
        self._on_notify = on_notify

        self.status = Status(
            rclone_version=".".join(map(str, info.version)),
            bisync_experimental=not info.bisync_is_stable,
        )
        # Carry the last successful sync time across restarts, so the tray can
        # still say "Up to date - 3 min ago" instead of claiming it never ran.
        self._restore_timestamps()
        self._history: list[Change] = self._load_history()

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._force_resync = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        self._terminate_child()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _terminate_child(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---------------------------------------------------------------- public

    def sync_now(self, resync: bool = False) -> None:
        """Ask the worker to run a sync as soon as it can."""
        if resync:
            self._force_resync = True
        self._wake.set()

    def set_paused(self, paused: bool, persist: bool = True) -> None:
        self.cfg.paused = paused
        if persist:
            try:
                self.cfg.save()
            except OSError as exc:
                log.warning("could not persist pause state: %s", exc)
        if paused:
            self._terminate_child()
            self._set_status(State.PAUSED, "Syncing is paused.")
        else:
            self._set_status(State.IDLE, "Resuming…")
            self._wake.set()

    def recent_changes(self, limit: int | None = None) -> list[Change]:
        limit = self.cfg.recent_files_count if limit is None else limit
        return list(reversed(self._history))[:limit] if limit else []

    def is_syncing(self) -> bool:
        return self.status.state is State.SYNCING

    def apply_config(self, cfg: Config) -> None:
        """Swap in an edited config and reschedule."""
        was_paused = self.cfg.paused
        self.cfg = cfg
        rclone.write_filter_file(cfg)
        if was_paused and not cfg.paused:
            self._set_status(State.IDLE, "Resuming…")
        elif cfg.paused:
            self._set_status(State.PAUSED, "Syncing is paused.")
        self._wake.set()

    # ------------------------------------------------------------- internals

    def _set_status(self, state: State, message: str = "", **extra) -> None:
        self.status.state = state
        if message:
            self.status.message = message
        for key, value in extra.items():
            setattr(self.status, key, value)
        self._persist_status()
        if self._on_status:
            try:
                self._on_status(self.status)
            except Exception:
                log.exception("status callback failed")

    def _notify(self, title: str, body: str, urgent: bool = False) -> None:
        if self._on_notify and self.cfg.show_notifications:
            try:
                self._on_notify(title, body, urgent)
            except Exception:
                log.exception("notify callback failed")

    def _persist_status(self) -> None:
        try:
            paths.STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            paths.STATUS_FILE.write_text(
                json.dumps(self.status.as_dict(), indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.debug("could not write status file: %s", exc)

    # -------------------------------------------------------------- history

    def _restore_timestamps(self) -> None:
        """Reload just the timestamps from the previous run's status file.

        Only these fields are restored: a stale *state* would be misleading (the
        app might have been killed mid-sync and would come back claiming to be
        syncing), whereas the timestamps remain true across a restart.
        """
        try:
            saved = json.loads(paths.STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for field_name in ("last_sync_at", "last_success_at"):
            value = saved.get(field_name)
            if isinstance(value, (int, float)) and value > 0:
                setattr(self.status, field_name, float(value))

    def _load_history(self) -> list[Change]:
        try:
            data = json.loads(paths.HISTORY_FILE.read_text(encoding="utf-8"))
            return [Change(**entry) for entry in data][-HISTORY_LIMIT:]
        except (OSError, ValueError, TypeError):
            return []

    def _save_history(self) -> None:
        try:
            paths.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            paths.HISTORY_FILE.write_text(
                json.dumps([c.as_dict() for c in self._history], indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.debug("could not write history: %s", exc)

    def _record(self, changes: list[Change]) -> None:
        if not changes:
            return
        self._history.extend(changes)
        del self._history[:-HISTORY_LIMIT]
        self._save_history()
        if self._on_changes:
            try:
                self._on_changes(self.recent_changes())
            except Exception:
                log.exception("changes callback failed")

    # ----------------------------------------------------------- resync gate

    def _baseline_key(self) -> dict:
        return {"path1": str(self.cfg.local_path), "path2": self.cfg.remote_spec}

    def needs_initial_resync(self) -> bool:
        """True when this local/remote pair has never been baselined.

        bisync keeps its own listings in the workdir, but names them after a
        mangled form of the two paths, so probing for them means depending on an
        rclone implementation detail.  A marker file we write ourselves after the
        first successful run is both simpler and survives a workdir wipe being
        the *intended* way to force a rebuild.
        """
        try:
            marker = json.loads(paths.BASELINE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        key = self._baseline_key()
        # A changed folder or remote is a different sync pair and needs its own
        # baseline, otherwise bisync would read the old pair's state as ours.
        return not (
            marker.get("path1") == key["path1"] and marker.get("path2") == key["path2"]
        )

    def _write_baseline_marker(self) -> None:
        try:
            paths.BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {**self._baseline_key(), "established_at": time.time()}
            paths.BASELINE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write baseline marker: %s", exc)

    # ------------------------------------------------------------- the loop

    def _loop(self) -> None:
        if self.cfg.sync_on_start and not self.cfg.paused:
            self._wake.set()
        else:
            self._idle_status()

        while not self._stop.is_set():
            interval = max(60, self.cfg.interval_minutes * 60)
            self.status.next_sync_at = (
                0.0 if self.cfg.paused or not self.cfg.enabled else time.time() + interval
            )
            self._persist_status()

            self._wake.wait(timeout=interval)
            if self._stop.is_set():
                break
            self._wake.clear()

            if not self.cfg.enabled:
                self._set_status(State.DISABLED, "Syncing is switched off.")
                continue
            if self.cfg.paused:
                self._set_status(State.PAUSED, "Syncing is paused.")
                continue

            problems = self.cfg.validate()
            if problems:
                self._set_status(
                    State.MISCONFIGURED, problems[0], last_error="; ".join(problems)
                )
                continue

            try:
                self._run_once()
            except Exception as exc:
                log.exception("sync run crashed")
                self._set_status(State.ERROR, f"Sync failed: {exc}", last_error=str(exc))

    def _idle_status(self) -> None:
        if not self.cfg.enabled:
            self._set_status(State.DISABLED, "Syncing is switched off.")
        elif self.cfg.paused:
            self._set_status(State.PAUSED, "Syncing is paused.")
        else:
            self._set_status(State.IDLE, "Waiting for the next sync.")

    # --------------------------------------------------------------- one run

    def _run_once(self) -> None:
        resync = self._force_resync or self.needs_initial_resync()
        self._force_resync = False

        if resync:
            self._set_status(
                State.SYNCING,
                "First run: building the initial baseline (this can take a while)…",
            )
        else:
            self._set_status(State.SYNCING, "Syncing…")

        rclone.write_filter_file(self.cfg)
        if backup := self.cfg.backup_local_path:
            backup.mkdir(parents=True, exist_ok=True)

        result = self._invoke(resync=resync)
        self.status.last_sync_at = time.time()

        if result.ok:
            self.status.last_success_at = time.time()
            self.status.last_error = ""
            self.status.pending_conflicts = list(result.conflicts)
            self.status.blocked_files = []
            # A dry run proves nothing about the baseline, so don't claim one.
            if not self.cfg.dry_run:
                self._write_baseline_marker()
            self._record(result.changes)
            suffix = " (dry run: nothing was changed)" if self.cfg.dry_run else ""
            self._set_status(State.IDLE, result.message + suffix)

            if result.conflicts and self.cfg.notify_on_conflict:
                shown = ", ".join(result.conflicts[:3])
                more = f" and {len(result.conflicts) - 3} more" if len(result.conflicts) > 3 else ""
                self._notify(
                    "Google Drive: conflicts kept",
                    f"Both copies were kept for {shown}{more}.",
                )
            return

        # ---- failure paths, each handled rather than blindly retried

        if result.safety_abort:
            self.status.last_error = result.message
            self._set_status(State.SAFETY_ABORT, result.message)
            if self.cfg.notify_on_error:
                self._notify(
                    "Google Drive: sync stopped for safety",
                    f"{result.message} Open Preferences to review, then sync manually.",
                    urgent=True,
                )
            return

        if result.blocked_files:
            # Checked before needs_resync on purpose: bisync demands a resync
            # after any aborted run, but re-baselining cannot help here -- the
            # next attempt hits the same unreadable files. Telling the user to
            # re-baseline would just loop them.
            self.status.last_error = result.message
            self.status.blocked_files = list(result.blocked_files)
            self._set_status(State.BLOCKED, result.message)
            if self.cfg.notify_on_error:
                self._notify(
                    f"Google Drive: {len(result.blocked_files)} file(s) could not be synced",
                    result.message,
                    urgent=True,
                )
            return

        if result.needs_resync:
            self.status.last_error = result.message
            if result.listings_lost:
                message = ("Sync needs a one-time re-baseline. "
                           "Use \"Sync now (rebuild baseline)\".")
                body = ("The saved sync state was lost. Choose \"Sync now "
                        "(rebuild baseline)\" in the tray menu to recover.")
            else:
                message = result.message
                body = result.message
            self._set_status(State.NEEDS_RESYNC, message)
            if self.cfg.notify_on_error:
                self._notify("Google Drive: sync did not finish", body, urgent=True)
            return

        self.status.last_error = result.message
        self._set_status(State.ERROR, result.message)
        if self.cfg.notify_on_error:
            self._notify("Google Drive: sync failed", result.message, urgent=True)

    def _invoke(self, resync: bool, dry_run: bool = False) -> SyncResult:
        cmd = rclone.bisync_command(self.cfg, self.info, resync=resync, dry_run=dry_run)
        clear_stale_bisync_locks()
        log.info("running: %s", " ".join(cmd))

        collected: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "RCLONE_CONFIG_PASS": os.environ.get("RCLONE_CONFIG_PASS", "")},
            )
        except OSError as exc:
            return SyncResult(ok=False, message=f"could not start rclone: {exc}")

        with self._proc_lock:
            self._proc = proc

        try:
            assert proc.stdout is not None
            paths.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(paths.LOG_FILE, "a", encoding="utf-8") as logfile:
                logfile.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                              f"{'resync' if resync else 'sync'} =====\n")
                for line in proc.stdout:
                    collected.append(line.rstrip("\n"))
                    logfile.write(line)
                    # Flush as we go so the log is useful while a long run is
                    # still in progress.
                    logfile.flush()
            proc.wait()
        finally:
            # Closing the pipe explicitly matters here: this process is a
            # long-lived daemon, so a leaked fd per sync would accumulate.
            if proc.stdout is not None:
                proc.stdout.close()
            with self._proc_lock:
                self._proc = None

        result = parse_bisync_output(
            collected,
            local_prefix=str(self.cfg.local_path),
            remote_prefix=self.cfg.remote_spec,
        )

        if proc.returncode == 0 and not result.ok:
            # Nothing to do at all still counts as success.
            result.ok = True
            if not result.message:
                result.message = "Up to date."
        elif proc.returncode != 0 and result.ok:
            result.ok = False
            result.message = result.message or f"rclone exited with code {proc.returncode}"

        if self._stop.is_set() or (self.cfg.paused and proc.returncode != 0):
            result.ok = False
            result.message = "Sync was interrupted."

        return result

    # ------------------------------------------------------------ dry-run API

    def preview(self, resync: bool = False) -> SyncResult:
        """Run a --dry-run bisync and return what it would do. Blocking."""
        rclone.write_filter_file(self.cfg)
        return self._invoke(resync=resync, dry_run=True)
