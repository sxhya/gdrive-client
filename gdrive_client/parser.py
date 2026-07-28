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
_RESYNC_HINTS = (
    "cannot find prior Path1 or Path2 listings",
    "must run --resync",
    "Must run --resync",
    "run --resync to recover",
    "bisync listing not found",
)
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
    safety_abort: bool = False
    safety_reason: str = ""      # "deletes" | "all-changed" | ""
    message: str = ""
    detected: dict = field(default_factory=dict)   # {"local": {...}, "remote": {...}}
    tail: list[str] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.changes)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


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

        if any(hint in body for hint in _RESYNC_HINTS):
            result.needs_resync = True
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
        if result.safety_abort and result.safety_reason == "all-changed":
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
        elif result.needs_resync:
            result.message = "Sync needs a one-time full re-baseline (resync)."
        elif result.ok:
            count = result.change_count
            result.message = "Up to date." if count == 0 else f"Synced {count} change(s)."
        else:
            result.message = interesting_tail[-1] if interesting_tail else "Sync failed."

    return result
