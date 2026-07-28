"""Account identity and quota for the tray's non-selectable info items.

The Drive backend has no `rclone config userinfo` support -- it exits with
"Google drive root '' doesn't support UserInfo" -- and `rclone about` reports
quota but not the signed-in address.  So the primary path reuses the OAuth token
that rclone already holds and asks the Drive API for both in a single call:

    GET /drive/v3/about?fields=user,storageQuota

If that is unavailable (encrypted config, offline, revoked scope) we degrade to
`rclone about --json`, which still gives quota, and report the email as unknown.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime

from . import paths, rclone
from .config import Config

log = logging.getLogger(__name__)

ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user,storageQuota"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CACHE_TTL = 3600  # seconds; quota does not move fast enough to matter


@dataclass
class AccountInfo:
    email: str = ""
    display_name: str = ""
    quota_limit: int = 0          # total bytes across the Google account
    quota_usage: int = 0          # total bytes used across the account
    quota_in_drive: int = 0       # bytes used by Drive itself
    quota_in_trash: int = 0       # bytes used by Drive trash
    fetched_at: float = 0.0
    source: str = ""              # "api", "rclone-about" or "cache"
    error: str = ""

    # ------------------------------------------------------------- rendering

    @property
    def identity_label(self) -> str:
        if self.email and self.display_name:
            return f"{self.display_name} <{self.email}>"
        if self.email:
            return self.email
        if self.error:
            return "Account: unavailable"
        return "Account: unknown"

    @property
    def quota_label(self) -> str:
        if not self.quota_limit:
            if self.quota_usage:
                return f"{human_bytes(self.quota_usage)} used (no quota reported)"
            return "Quota: unavailable"
        pct = 100.0 * self.quota_usage / self.quota_limit
        return (
            f"{human_bytes(self.quota_usage)} of {human_bytes(self.quota_limit)} "
            f"used ({pct:.1f}%)"
        )

    @property
    def drive_detail_label(self) -> str:
        parts = []
        if self.quota_in_drive:
            parts.append(f"Drive {human_bytes(self.quota_in_drive)}")
        if self.quota_in_trash:
            parts.append(f"trash {human_bytes(self.quota_in_trash)}")
        return " · ".join(parts) if parts else ""

    @property
    def quota_fraction(self) -> float:
        if not self.quota_limit:
            return 0.0
        return min(1.0, self.quota_usage / self.quota_limit)


def human_bytes(count: int | float) -> str:
    """Binary units, matching what `rclone about` and Drive's own UI show."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


# ------------------------------------------------------------------- token


def _config_dump(info: rclone.RcloneInfo) -> dict:
    """`rclone config dump` handles encrypted configs; plain file read does not."""
    proc = subprocess.run(
        [str(info.path), "config", "dump"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone config dump failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout or "{}")


def _remote_section(cfg: Config, info: rclone.RcloneInfo) -> dict:
    name = cfg.remote.strip().rstrip(":")
    dump = _config_dump(info)
    section = dump.get(name)
    if not section:
        raise RuntimeError(f"remote '{name}' not found in rclone config")
    return section


_EXPIRY_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def parse_expiry(expiry: str) -> "datetime | None":
    """Parse an rclone token expiry, or None if it isn't a timestamp we understand.

    rclone writes Go's RFC3339 with nanosecond precision, e.g.
    '2026-07-28T12:00:00.123456789+02:00'.  fromisoformat rejects more than six
    fractional digits before Python 3.11, so trim them.
    """
    match = _EXPIRY_RE.match(expiry.strip())
    if not match:
        return None
    stamp = match.group("stamp")
    frac = (match.group("frac") or "")[:6]
    tz = match.group("tz") or ""
    if tz == "Z":
        tz = "+00:00"
    text = stamp + (f".{frac}" if frac else "") + tz
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _access_token(section: dict) -> str:
    """Return a usable access token, refreshing via OAuth if the cached one is stale.

    The refreshed token is intentionally not written back to rclone's config:
    rclone owns that file, and a stale access_token there is harmless because
    rclone refreshes it itself on next use.
    """
    raw = section.get("token")
    if not raw:
        raise RuntimeError("remote has no OAuth token (service account or encrypted config?)")
    token = json.loads(raw) if isinstance(raw, str) else raw

    access = token.get("access_token", "")
    deadline = parse_expiry(token.get("expiry", "") or "")
    if access:
        if deadline is None:
            # Unparseable or absent expiry: try the token and let the API judge.
            return access
        now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
        if (deadline - now).total_seconds() > 120:
            return access

    refresh = token.get("refresh_token")
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not (refresh and client_id and client_secret):
        if access:
            return access  # last resort: maybe it still works
        raise RuntimeError("token expired and no refresh credentials are available")

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen(TOKEN_URL, body, timeout=30) as resp:
        return json.load(resp)["access_token"]


def _api_about(access_token: str) -> dict:
    req = urllib.request.Request(ABOUT_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


# -------------------------------------------------------------------- cache


def _read_cache() -> AccountInfo | None:
    try:
        data = json.loads(paths.ACCOUNT_CACHE.read_text(encoding="utf-8"))
        return AccountInfo(**data)
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(acc: AccountInfo) -> None:
    try:
        paths.ACCOUNT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        paths.ACCOUNT_CACHE.write_text(json.dumps(asdict(acc), indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not cache account info: %s", exc)


# -------------------------------------------------------------------- fetch


def fetch(
    cfg: Config,
    info: rclone.RcloneInfo,
    force: bool = False,
) -> AccountInfo:
    """Return account identity and quota, preferring cache within CACHE_TTL."""
    if not force:
        cached = _read_cache()
        if cached and time.time() - cached.fetched_at < CACHE_TTL:
            cached.source = "cache"
            return cached

    errors: list[str] = []

    # Primary: Drive API, gives email + quota together.
    try:
        section = _remote_section(cfg, info)
        data = _api_about(_access_token(section))
        user = data.get("user") or {}
        quota = data.get("storageQuota") or {}
        acc = AccountInfo(
            email=user.get("emailAddress", ""),
            display_name=user.get("displayName", ""),
            quota_limit=int(quota.get("limit", 0) or 0),
            quota_usage=int(quota.get("usage", 0) or 0),
            quota_in_drive=int(quota.get("usageInDrive", 0) or 0),
            quota_in_trash=int(quota.get("usageInDriveTrash", 0) or 0),
            fetched_at=time.time(),
            source="api",
        )
        _write_cache(acc)
        return acc
    except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError) as exc:
        errors.append(f"Drive API: {exc}")
        log.info("Drive API about failed (%s); falling back to rclone about", exc)

    # Fallback: quota only.
    try:
        data = rclone.about(cfg, info)
        acc = AccountInfo(
            quota_limit=int(data.get("total", 0) or 0),
            quota_usage=int(data.get("used", 0) or 0),
            quota_in_trash=int(data.get("trashed", 0) or 0),
            fetched_at=time.time(),
            source="rclone-about",
            error="; ".join(errors),
        )
        _write_cache(acc)
        return acc
    except Exception as exc:
        errors.append(f"rclone about: {exc}")

    stale = _read_cache()
    if stale:
        stale.source = "cache"
        stale.error = "; ".join(errors)
        return stale
    return AccountInfo(error="; ".join(errors), fetched_at=time.time())
