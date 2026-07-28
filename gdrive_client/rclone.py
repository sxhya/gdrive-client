"""Locating, upgrading and driving the rclone binary.

Why a version gate exists: `rclone bisync` is the only piece of this stack that
does real bidirectional sync.  It landed in 1.58, still self-describes as
EXPERIMENTAL in 1.60 (what Ubuntu 24.04 ships), and only grew the
--conflict-resolve family in 1.66.  Ubuntu 20.04 ships 1.50, which has no
bisync at all.  So if the system rclone is too old we fetch the official static
build into our own data dir rather than asking the user to fight apt.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import Config

log = logging.getLogger(__name__)

# 1.66 is the first release with --conflict-resolve / --conflict-loser /
# --conflict-suffix, which is what makes the configurable conflict policies
# implementable.  Below this we can still sync, but only with keep-both.
MIN_VERSION = (1, 66, 0)
# bisync stopped calling itself experimental here.
STABLE_BISYNC_VERSION = (1, 64, 0)

DOWNLOAD_BASE = "https://downloads.rclone.org"

_ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "armv7l": "arm-v7",
    "armv6l": "arm-v6",
}


class RcloneError(RuntimeError):
    """rclone is missing, too old, or exited non-zero in a way we can't handle."""


@dataclass(frozen=True)
class RcloneInfo:
    path: Path
    version: tuple[int, int, int]
    raw_version: str

    @property
    def supports_bisync(self) -> bool:
        return self.version >= (1, 58, 0)

    @property
    def bisync_is_stable(self) -> bool:
        return self.version >= STABLE_BISYNC_VERSION

    @property
    def supports_conflict_flags(self) -> bool:
        return self.version >= MIN_VERSION

    def __str__(self) -> str:
        return f"{self.path} (v{'.'.join(map(str, self.version))})"


# --------------------------------------------------------------------- version


def parse_version(text: str) -> tuple[int, int, int]:
    """Pull a version triple out of `rclone version` output.

    Handles the shapes seen in the wild: 'rclone v1.60.1-DEV', 'rclone v1.71.0'
    and Debian's 'rclone v1.60.1+dfsg'.
    """
    match = re.search(r"rclone\s+v?(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        raise RcloneError(f"could not parse an rclone version out of: {text.splitlines()[:1]}")
    major, minor, patch = match.group(1), match.group(2), match.group(3) or "0"
    return (int(major), int(minor), int(patch))


def probe(binary: str | Path) -> RcloneInfo | None:
    """Return version info for a candidate binary, or None if it isn't usable."""
    try:
        proc = subprocess.run(
            [str(binary), "version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or proc.stderr).strip()
    try:
        return RcloneInfo(Path(binary), parse_version(first), first.splitlines()[0])
    except RcloneError:
        return None


def _arch() -> str:
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine)
    if arch is None:
        raise RcloneError(
            f"no official rclone build for architecture '{machine}'. "
            "Install rclone >= 1.66 manually and set advanced.rclone_path."
        )
    return arch


def _fetch(url: str, timeout: int = 120) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        raise RcloneError(f"fetch failed from {url}: {exc}") from exc


def _current_version_tag() -> str:
    """Read the published version tag, e.g. 'v1.74.4', from version.txt."""
    text = _fetch(f"{DOWNLOAD_BASE}/version.txt", timeout=30).decode().strip()
    match = re.search(r"(v\d+\.\d+\.\d+)", text)
    if not match:
        raise RcloneError(f"unexpected version.txt contents: {text[:80]!r}")
    return match.group(1)


def _expected_sha256(tag: str, archive: str) -> str:
    """Look the archive up in the release's PGP-signed SHA256SUMS listing.

    Failure is fatal on purpose: we are about to install an executable, so no
    checksum means no install.
    """
    listing = _fetch(f"{DOWNLOAD_BASE}/{tag}/SHA256SUMS", timeout=60).decode(
        "utf-8", errors="replace"
    )
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise RcloneError(f"{archive} has no SHA256 entry in {tag}/SHA256SUMS")


def download(dest_dir: Path | None = None, progress=None) -> RcloneInfo:
    """Fetch the current official static rclone into dest_dir and return its info.

    The versioned archive is downloaded rather than 'rclone-current-*' so that
    the bytes and the checksum are guaranteed to describe the same release even
    if upstream publishes a new version mid-download.

    progress, if given, is called with a human-readable status string.
    """
    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    dest_dir = paths.BIN_DIR if dest_dir is None else dest_dir
    arch = _arch()
    tag = _current_version_tag()
    archive = f"rclone-{tag}-linux-{arch}.zip"
    url = f"{DOWNLOAD_BASE}/{tag}/{archive}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    say(f"Fetching checksum for rclone {tag} ({arch})…")
    expected = _expected_sha256(tag, archive)

    say(f"Downloading {archive}…")
    blob = _fetch(url, timeout=300)

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise RcloneError(
            f"checksum mismatch for {archive}: expected {expected}, got {actual}. "
            "Refusing to install."
        )
    say("Checksum verified.")

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            member = next(
                (n for n in zf.namelist() if n.endswith("/rclone") or n == "rclone"), None
            )
            if member is None:
                raise RcloneError(f"{archive} contains no rclone binary")
            payload = zf.read(member)
    except zipfile.BadZipFile as exc:
        raise RcloneError(f"{archive} is not a valid zip: {exc}") from exc

    final = dest_dir / "rclone"
    fd, tmp = tempfile.mkstemp(dir=str(dest_dir), prefix=".rclone.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o755)
        info = probe(tmp)
        if info is None:
            raise RcloneError("the downloaded rclone binary would not run")
        os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    resolved = probe(final)
    if resolved is None:
        raise RcloneError("the installed rclone binary would not run")
    say(f"Installed rclone v{'.'.join(map(str, resolved.version))} to {final}")
    return resolved


def resolve(cfg: Config, allow_download: bool = True, progress=None) -> RcloneInfo:
    """Pick the rclone to use, downloading a current build if needed.

    Order: explicit config override, our own downloaded copy, then $PATH.
    """
    if cfg.rclone_path.strip():
        override = Path(os.path.expanduser(cfg.rclone_path.strip()))
        info = probe(override)
        if info is None:
            raise RcloneError(f"advanced.rclone_path '{override}' is not a working rclone binary")
        if not info.supports_bisync:
            raise RcloneError(
                f"{info} has no 'bisync' command (needs >= 1.58). "
                "Clear advanced.rclone_path to let the app fetch a current build."
            )
        return info

    candidates: list[Path] = [paths.BIN_DIR / "rclone"]
    system = shutil.which("rclone")
    if system:
        candidates.append(Path(system))

    best: RcloneInfo | None = None
    for candidate in candidates:
        info = probe(candidate)
        if info and (best is None or info.version > best.version):
            best = info

    if best and best.supports_conflict_flags:
        return best

    if not allow_download:
        if best and best.supports_bisync:
            return best
        raise RcloneError(
            "no rclone with bisync support found and downloading is disabled."
        )

    why = (
        f"system rclone v{'.'.join(map(str, best.version))} is older than "
        f"{'.'.join(map(str, MIN_VERSION))}"
        if best else "rclone was not found on PATH"
    )
    log.info("%s; fetching a current static build", why)
    try:
        return download(progress=progress)
    except RcloneError as exc:
        # A too-old-but-working rclone beats no sync at all, as long as it can
        # bisync -- the app degrades to keep-both conflicts and warns in the tray.
        if best and best.supports_bisync:
            log.warning("download failed (%s); falling back to %s", exc, best)
            return best
        raise RcloneError(
            f"{why}, and downloading a current build failed: {exc}\n"
            "Install rclone >= 1.66 manually (https://rclone.org/install/) and set "
            "advanced.rclone_path in ~/.gdrive-settings."
        ) from exc


# --------------------------------------------------------------- command lines


def write_filter_file(cfg: Config, path: Path | None = None) -> Path:
    """Render filters.exclude into an rclone filter file.

    rclone filter syntax is '- pattern' to exclude; unlike rsync a bare pattern
    with no slash matches at any depth, which is the behaviour we want here.

    The default is resolved here rather than in the signature so that it always
    agrees with what bisync_command() reads, even if paths.FILTER_FILE is
    redirected at runtime.
    """
    path = paths.FILTER_FILE if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Generated by gdrive-client from ~/.gdrive-settings [filters].", "# Do not edit."]
    for pattern in cfg.exclude:
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        # Allow explicit '+ '/'- ' prefixes to pass through for power users.
        lines.append(pattern if pattern[:2] in ("- ", "+ ") else f"- {pattern}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def log_level_flag(cfg: Config) -> str:
    """Clamp the configured level to something we can still parse changes from.

    rclone emits per-file 'Copied'/'Deleted' lines at INFO, so anything quieter
    would leave the "Recently changed" menu permanently empty.  Note that -v and
    --log-level are mutually exclusive in rclone, so only this flag is passed.
    """
    return "DEBUG" if cfg.log_level.strip().upper() == "DEBUG" else "INFO"


def common_flags(cfg: Config, info: RcloneInfo) -> list[str]:
    """Flags shared by every rclone invocation this app makes."""
    flags = [
        "--transfers", str(cfg.transfers),
        "--checkers", str(cfg.checkers),
        "--modify-window", cfg.modify_window or "1s",
        "--retries", str(max(1, cfg.retries)),
        "--log-level", log_level_flag(cfg),
        # rclone colours its log even when not on a tty in some paths; the
        # escape codes land in the middle of the file paths we parse.
        "--color", "NEVER",
    ]

    if cfg.compare == "checksum":
        flags.append("--checksum")
    elif cfg.compare == "size-only":
        flags.append("--size-only")

    up, down = cfg.bwlimit_up.strip(), cfg.bwlimit_down.strip()
    if up or down:
        flags += ["--bwlimit", f"{up or 'off'}:{down or 'off'}"]

    if cfg.max_size.strip():
        flags += ["--max-size", cfg.max_size.strip()]

    if cfg.preserve_symlinks:
        flags.append("--links")

    if cfg.dry_run:
        flags.append("--dry-run")

    if cfg.extra_flags.strip():
        flags += cfg.extra_flags.split()

    return flags


def bisync_command(
    cfg: Config,
    info: RcloneInfo,
    resync: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    """Build the bisync command line.

    Path1 is local and Path2 is remote, which matters for conflict naming and
    for --resync (Path1 wins ties on the very first run).
    """
    cmd = [
        str(info.path), "bisync",
        str(cfg.local_path), cfg.remote_spec,
        "--workdir", str(paths.BISYNC_WORKDIR),
        "--filters-file", str(paths.FILTER_FILE),
    ]

    cmd += common_flags(cfg, info)
    if dry_run and "--dry-run" not in cmd:
        cmd.append("--dry-run")

    if resync:
        # --resync alone means --resync-mode path1, i.e. the local copy wins every
        # tie. That would push a stale locally-seeded file over a newer one on
        # Drive, so the mode is configurable and defaults to "newer".
        cmd += ["--resync", "--resync-mode", cfg.resync_mode]
    if force:
        cmd.append("--force")

    # For bisync --max-delete is a PERCENTAGE of changed files, not a count as
    # it is for plain sync. 100 means "no limit", so only pass it below that.
    if cfg.max_delete_percent < 100:
        cmd += ["--max-delete", str(cfg.max_delete_percent)]

    if cfg.check_access:
        cmd.append("--check-access")
    if cfg.remove_empty_dirs:
        cmd.append("--remove-empty-dirs")
    if cfg.resilient:
        cmd.append("--resilient")
    if cfg.recover:
        cmd.append("--recover")

    if info.supports_conflict_flags:
        cmd += ["--conflict-resolve", cfg.resolve_flag,
                "--conflict-loser", cfg.loser_flag]
        if cfg.loser_flag == "pathname" and cfg.conflict_suffix.strip():
            cmd += ["--conflict-suffix", cfg.conflict_suffix.strip()]
    # Below 1.66 bisync has no conflict flags at all and falls back to its
    # built-in keep-both behaviour, which is the safe default anyway.

    # --backup-dir1/2 are bisync-specific: a single --backup-dir would be
    # resolved against whichever side is the destination, which is wrong when
    # both sides take turns being the destination.
    local_backup = cfg.backup_local_path
    if local_backup:
        cmd += ["--backup-dir1", str(local_backup)]
    if cfg.backup_dir_remote.strip():
        cmd += ["--backup-dir2", cfg.backup_dir_remote.strip()]

    # No progress spinner, so the output stays line-oriented and parseable.
    cmd += ["--stats", "0"]
    return cmd


def about(cfg: Config, info: RcloneInfo, timeout: int = 60) -> dict:
    """`rclone about --json` -- used as the quota fallback when the API call fails."""
    proc = subprocess.run(
        [str(info.path), "about", cfg.remote_spec, "--json"],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RcloneError(f"rclone about failed: {proc.stderr.strip()[:300]}")
    import json
    return json.loads(proc.stdout)


def remote_exists(cfg: Config, info: RcloneInfo) -> bool:
    """True if the configured remote name is present in rclone's config."""
    proc = subprocess.run(
        [str(info.path), "listremotes"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        return False
    wanted = cfg.remote.strip().rstrip(":") + ":"
    return wanted in [ln.strip() for ln in proc.stdout.splitlines()]
