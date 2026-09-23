"""Turning `rclone bisync` log output into a structured change list.

Every regex here was written against captured output from rclone 1.74; see
docs/bisync-output-samples.txt for the raw text.  The authoritative lines are
the "Queue ..." ones, because those describe actions bisync actually applied
rather than differences it merely detected:

    INFO  : - Path1   Queue copy to Path2   - /abs/dest/path/f1.txt
    INFO  : - Path2   Queue delete          - /abs/dest/path/f3.txt
    NOTICE: - WARNING New or changed in both paths - f5.txt

Note the label means different things per verb: for "Queue copy to PathN" it is
the *source* side, while for "Queue delete" it is the side being modified.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 2026/07/28 11:47:40 INFO  : <body>
_LINE_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+"
    r"(?P<level>DEBUG|INFO|NOTICE|ERROR|CRITICAL)\s*:\s*(?P<body>.*)$"
)

# - Path1             Queue copy to Path2                 - /abs/path
_QUEUE_COPY_RE = re.compile(
    r"^-\s+Path(?P<src>[12])\s+Queue copy to Path(?P<dst>[12])\s+-\s+(?P<path>.+?)\s*$"
)
# - Path2             Queue delete                        - /abs/path
_QUEUE_DELETE_RE = re.compile(
    r"^-\s+Path(?P<side>[12])\s+Queue delete\s+-\s+(?P<path>.+?)\s*$"
)
# - WARNING           New or changed in both paths        - relative/path
_CONFLICT_RE = re.compile(
    r"^-\s+WARNING\s+New or changed in both paths\s+-\s+(?P<path>.+?)\s*$"
)
# - Path1             File changed: size (larger)         - relative/path
_DETECT_RE = re.compile(
    r"^-\s+Path(?P<side>[12])\s+"
    r"(?P<what>File is new|File was deleted|File changed:[^-]*?)\s+-\s+(?P<path>.+?)\s*$"
)
# Path1:    4 changes:    1 new,    2 modified,    1 deleted
_SUMMARY_RE = re.compile(
    r"^Path(?P<side>[12]):\s+(?P<total>\d+)\s+changes:\s+(?P<new>\d+)\s+new,"
    r"\s+(?P<modified>\d+)\s+modified,\s+(?P<deleted>\d+)\s+deleted"
)

_SUCCESS = "Bisync successful"

# bisync says "Must run --resync to recover" for two very different situations:
# its saved listings really are gone, or the run aborted partway on something
# else (usually a file it could not read).  Only the first is a state problem;
# reporting the second as "sync data lost" hides the actual cause and sends the
# user round a re-baseline loop that cannot succeed.
_LISTING_LOST_HINTS = (
    "cannot find prior Path1 or Path2 listings",
    "bisync listing not found",
    "Bisync critical error: listing",
)
_RESYNC_DEMAND_HINTS = (
    "must run --resync",
    "Must run --resync",
    "run --resync to recover",
)

# ERROR : some/path.rar: Failed to copy: failed to open source object: ...
_FILE_ERROR_RE = re.compile(
    r"^(?P<path>.+?): (?:Failed to copy|Failed to move|Failed to update|"
    r"Failed to set modification time): (?P<reason>.+)$"
)
# Google's scanner refuses to serve some files; rclone names the exact remedy.
_ABUSE_MARKER = "cannotDownloadAbusiveFile"
# bisync has two distinct safety aborts and they need different explanations:
# one is about deletions exceeding --max-delete, the other fires when *every*
# tracked file changed at once (a strong hint that a folder was moved or
# remounted).  Reporting the second as a deletion problem would be misleading.
_SAFETY_DELETE_HINTS = (
    "too many deletes",
    "--max-delete",
)
_SAFETY_ALLCHANGED_HINTS = (
    "all files were changed",
)
# Another bisync of the same pair holds the workdir lock.  Stale locks from a
# killed run are cleared before each sync, so reaching this means a live one.
_LOCKED_HINT = "prior lock file found"

_SAFETY_HINTS = _SAFETY_DELETE_HINTS + _SAFETY_ALLCHANGED_HINTS + ("Safety abort",)

ACTIONS = ("upload", "download", "delete-local", "delete-remote", "conflict")

_ACTION_LABELS = {
    "upload": "↑ uploaded",
    "download": "↓ downloaded",
    "delete-local": "✕ deleted locally",
    "delete-remote": "✕ deleted on Drive",
    "conflict": "⚠ conflict",
}


def action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


@dataclass
class Change:
    path: str
    action: str
    at: float = field(default_factory=time.time)

    @property
    def label(self) -> str:
        return action_label(self.action)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncResult:
    ok: bool = False
    changes: list[Change] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    needs_resync: bool = False
    listings_lost: bool = False          # the resync demand is a real state loss
    safety_abort: bool = False
    safety_reason: str = ""              # "deletes" | "all-changed" | ""
    file_errors: list[tuple[str, str]] = field(default_factory=list)
    blocked_files: list[str] = field(default_factory=list)   # Google-flagged
    locked: bool = False                 # another live bisync holds the lock
    message: str = ""
    detected: dict = field(default_factory=dict)   # {"local": {...}, "remote": {...}}
    tail: list[str] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.changes)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# rclone labels a remote in its log with an internal instance id, so the same
# remote appears as both 'drive:' and 'drive{P2dbo}:/'.  Observed on 1.74.4 in
# the bisync "Queue copy to Path2" lines.
_REMOTE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.+-]+(?:\{[^}]*\})?:/?")


def _relativise(path: str, local_prefix: str, remote_prefix: str) -> str:
    """Strip whichever root the absolute path sits under.

    bisync prints destination paths as absolute local paths or as
    'remote:sub/dir/file'; the tray wants neither prefix.
    """
    candidates = [p for p in (local_prefix, remote_prefix) if p]
    # Longest prefix first so 'remote:sub' beats 'remote:'.
    for prefix in sorted(candidates, key=len, reverse=True):
        for variant in (prefix.rstrip("/") + "/", prefix):
            if variant and path.startswith(variant):
                return path[len(variant):].lstrip("/") or path

    # Not one of the configured roots verbatim.  If it still looks like a remote
    # spec, drop that prefix rather than showing the user 'drive{P2dbo}:/file'.
    if not path.startswith("/"):
        stripped = _REMOTE_PREFIX_RE.sub("", path, count=1)
        if stripped and stripped != path:
            # Re-strip a configured remote subpath, e.g. remote_path='Work'.
            remote_sub = remote_prefix.split(":", 1)[1].strip("/") if ":" in remote_prefix else ""
            if remote_sub and stripped.startswith(remote_sub + "/"):
                stripped = stripped[len(remote_sub) + 1:]
            return stripped
    return path


def parse_bisync_output(
    text: str | list[str],
    local_prefix: str = "",
    remote_prefix: str = "",
    now: float | None = None,
) -> SyncResult:
    """Parse a full bisync run's combined stdout+stderr."""
    stamp = time.time() if now is None else now
    lines = text.splitlines() if isinstance(text, str) else list(text)

    result = SyncResult()
    seen: set[tuple[str, str]] = set()
    interesting_tail: list[str] = []

    def add(path: str, action: str) -> None:
        path = path.strip()
        if not path:
            return
        key = (path, action)
        if key in seen:
            return
        seen.add(key)
        result.changes.append(Change(path=path, action=action, at=stamp))

    for raw in lines:
        clean = strip_ansi(raw).rstrip()
        if not clean:
            continue

        match = _LINE_RE.match(clean)
        body = match.group("body").strip() if match else clean.strip()
        level = match.group("level") if match else ""

        if level in ("ERROR", "CRITICAL") or "abort" in body.lower():
            interesting_tail.append(body)

        if _SUCCESS in body:
            result.ok = True
            continue

        if any(hint in body for hint in _RESYNC_DEMAND_HINTS):
            result.needs_resync = True
        if any(hint in body for hint in _LISTING_LOST_HINTS):
            result.needs_resync = True
            result.listings_lost = True

        if level in ("ERROR", "CRITICAL"):
            file_error = _FILE_ERROR_RE.match(body)
            if file_error:
                path = _relativise(file_error.group("path"), local_prefix, remote_prefix)
                reason = file_error.group("reason")
                if (path, reason) not in result.file_errors:
                    result.file_errors.append((path, reason))
                if _ABUSE_MARKER in reason and path not in result.blocked_files:
                    result.blocked_files.append(path)

        if _LOCKED_HINT in body:
            result.locked = True

        if any(hint in body for hint in _SAFETY_HINTS):
            result.safety_abort = True
        if any(hint in body for hint in _SAFETY_DELETE_HINTS):
            result.safety_reason = "deletes"
        elif any(hint in body for hint in _SAFETY_ALLCHANGED_HINTS):
            # Do not let this overwrite a more specific "deletes" verdict.
            result.safety_reason = result.safety_reason or "all-changed"

        queue_copy = _QUEUE_COPY_RE.match(body)
        if queue_copy:
            path = _relativise(queue_copy.group("path"), local_prefix, remote_prefix)
            # dst == 2 means the remote is being written, i.e. an upload.
            add(path, "upload" if queue_copy.group("dst") == "2" else "download")
            continue

        queue_delete = _QUEUE_DELETE_RE.match(body)
        if queue_delete:
            path = _relativise(queue_delete.group("path"), local_prefix, remote_prefix)
            side = queue_delete.group("side")
            add(path, "delete-remote" if side == "2" else "delete-local")
            continue

        conflict = _CONFLICT_RE.match(body)
        if conflict:
            path = _relativise(conflict.group("path"), local_prefix, remote_prefix)
            result.conflicts.append(path)
            add(path, "conflict")
            continue

        summary = _SUMMARY_RE.match(body)
        if summary:
            side = "local" if summary.group("side") == "1" else "remote"
            result.detected[side] = {
                "total": int(summary.group("total")),
                "new": int(summary.group("new")),
                "modified": int(summary.group("modified")),
                "deleted": int(summary.group("deleted")),
            }
            continue

    # Conflicts are the headline; show them first in the tray.
    result.changes.sort(key=lambda c: 0 if c.action == "conflict" else 1)
    result.tail = interesting_tail[-12:]

    if not result.message:
        if result.locked:
            # Nothing ran at all, so none of the other verdicts apply.
            result.message = (
                "Skipped: another sync of this folder is still running. "
                "It will be retried at the next interval."
            )
        elif result.safety_abort and result.safety_reason == "all-changed":
            result.message = (
                "Sync stopped for safety: every tracked file appears to have "
                "changed at once. Check that the right folder is in place, then "
                "sync again. Nothing was changed."
            )
        elif result.safety_abort:
            result.message = (
                "Sync aborted by the safety limit: too large a share of files would "
                "have been deleted. Nothing was changed."
            )
        elif result.blocked_files:
            # Diagnose this before the resync demand: the demand is a symptom.
            shown = ", ".join(result.blocked_files[:2])
            extra = (f" and {len(result.blocked_files) - 2} more"
                     if len(result.blocked_files) > 2 else "")
            result.message = (
                f"Google refused to serve {len(result.blocked_files)} file(s) it has "
                f"flagged as unsafe ({shown}{extra}), which aborted the sync. "
                "Enable 'Download Google-flagged files' in Preferences, or exclude them."
            )
        elif result.file_errors and not result.listings_lost:
            first_path, first_reason = result.file_errors[0]
            extra = (f" (and {len(result.file_errors) - 1} other file(s))"
                     if len(result.file_errors) > 1 else "")
            result.message = (
                f"Sync stopped on {first_path}{extra}: {first_reason[:140]}"
            )
        elif result.listings_lost:
            result.message = "Sync needs a one-time full re-baseline (resync)."
        elif result.needs_resync:
            result.message = (
                "The sync run did not finish, so it will re-baseline on the next "
                "attempt. See the sync log for what stopped it."
            )
        elif result.ok:
            count = result.change_count
            result.message = "Up to date." if count == 0 else f"Synced {count} change(s)."
        else:
            result.message = interesting_tail[-1] if interesting_tail else "Sync failed."

    return result
