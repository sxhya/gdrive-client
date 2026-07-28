"""Entry point: the tray application plus a small CLI for headless use."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import account, autostart, engine as engine_mod, paths, rclone
from .config import CONFIG_PATH, Config
from .parser import action_label

log = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    paths.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(paths.STATE_DIR / "app.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def bootstrap_config() -> Config:
    """Load the config, creating a sensible one on first run.

    If rclone knows exactly one remote we adopt it, because that is nearly
    always the intent and it saves the user an edit before anything works.
    """
    cfg = Config.load()
    created = not CONFIG_PATH.exists()

    if not cfg.remote.strip():
        remotes = _list_remotes()
        if len(remotes) == 1:
            cfg.remote = remotes[0]
            log.info("adopting the only configured rclone remote: %s", cfg.remote)
        elif remotes:
            log.warning(
                "several rclone remotes found (%s); set account.remote in %s",
                ", ".join(remotes), CONFIG_PATH,
            )

    if created or not CONFIG_PATH.exists():
        try:
            cfg.save()
            log.info("wrote a starter config to %s", CONFIG_PATH)
        except OSError as exc:
            log.warning("could not write %s: %s", CONFIG_PATH, exc)

    return cfg


def _list_remotes() -> list[str]:
    import shutil
    import subprocess

    binary = shutil.which("rclone") or str(paths.BIN_DIR / "rclone")
    try:
        proc = subprocess.run(
            [binary, "listremotes"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


# --------------------------------------------------------------------- the GUI


def run_tray(cfg: Config, info: rclone.RcloneInfo) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    from .indicator import backend_name, have_appindicator
    from .tray import Tray

    log.info("tray backend: %s", backend_name())
    if not have_appindicator():
        log.warning(
            "AppIndicator typelib missing; using Gtk.StatusIcon. On Ubuntu/Xubuntu: "
            "sudo apt install gir1.2-ayatanaappindicator3-0.1"
        )

    sync = engine_mod.SyncEngine(cfg, info)
    tray = Tray(cfg, info, sync)
    sync._on_status = tray.on_status
    sync._on_changes = tray.on_changes
    sync._on_notify = tray.notify

    # Reflect the starting state before the first run finishes.
    tray._apply_status(sync.status)

    if cfg.autostart and not autostart.is_enabled():
        autostart.set_enabled(True)

    sync.start()

    def shutdown(*_args):
        log.info("shutting down")
        sync.stop()
        Gtk.main_quit()
        return False

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, shutdown)

    try:
        Gtk.main()
    finally:
        sync.stop()
    return 0


# --------------------------------------------------------------------- the CLI


def cmd_status(cfg: Config, info: rclone.RcloneInfo) -> int:
    import json

    print(f"rclone            : {info}")
    print(f"bisync stable     : {'yes' if info.bisync_is_stable else 'NO (experimental)'}")
    print(f"config            : {CONFIG_PATH}")
    print(f"local folder      : {cfg.local_path}")
    print(f"remote            : {cfg.remote_spec}")
    print(f"paused            : {cfg.paused}")
    print(f"conflict policy   : {cfg.conflict_policy} / loser {cfg.conflict_loser}")

    problems = cfg.validate()
    if problems:
        print("\nconfiguration problems:")
        for problem in problems:
            print(f"  - {problem}")

    try:
        state = json.loads(paths.STATUS_FILE.read_text(encoding="utf-8"))
        print(f"\nlast known state  : {state.get('state')} — {state.get('message', '')}")
    except (OSError, ValueError):
        print("\nlast known state  : (no run recorded yet)")

    try:
        acc = account.fetch(cfg, info)
        print(f"account           : {acc.identity_label}")
        print(f"quota             : {acc.quota_label}")
    except Exception as exc:
        print(f"account           : unavailable ({exc})")
    return 0


def cmd_sync(cfg: Config, info: rclone.RcloneInfo, resync: bool, dry_run: bool) -> int:
    if problems := cfg.validate():
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    sync = engine_mod.SyncEngine(cfg, info)
    if resync or sync.needs_initial_resync():
        if not resync:
            print("no baseline for this folder/remote pair yet; doing a first-run resync")
        resync = True

    print(f"{'previewing' if dry_run else 'running'} bisync: "
          f"{cfg.local_path}  <->  {cfg.remote_spec}")
    result = sync._invoke(resync=resync, dry_run=dry_run)

    for change in result.changes:
        print(f"  {action_label(change.action):22} {change.path}")
    if result.detected:
        for side, counts in result.detected.items():
            print(f"  detected on {side}: {counts['new']} new, "
                  f"{counts['modified']} modified, {counts['deleted']} deleted")

    print(f"\n{result.message}")
    if not result.ok:
        for line in result.tail:
            print(f"  ! {line}", file=sys.stderr)
        return 1

    if not dry_run:
        sync.status.last_success_at = sync.status.last_sync_at = __import__("time").time()
        sync._write_baseline_marker()
        sync._record(result.changes)
    return 0


def cmd_explain(cfg: Config, _info: rclone.RcloneInfo) -> int:
    """Print the setting -> rsync/rclone flag mapping."""
    from .config import RSYNC_EQUIVALENT

    width = max(len(k) for k in RSYNC_EQUIVALENT)
    print(f"{'setting'.ljust(width)}  {'rsync'.ljust(38)}  rclone")
    print(f"{'-' * width}  {'-' * 38}  {'-' * 34}")
    for option, (rsync_flag, rclone_flag) in RSYNC_EQUIVALENT.items():
        print(f"{option.ljust(width)}  {rsync_flag.ljust(38)}  {rclone_flag}")
    return 0


def cmd_doctor(cfg: Config, info: rclone.RcloneInfo) -> int:
    """Check the things that actually break in practice."""
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "ok  " if good else "FAIL"
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))

    check("rclone found", True, str(info))
    check("bisync available", info.supports_bisync)
    check("bisync non-experimental", info.bisync_is_stable,
          "" if info.bisync_is_stable else "upgrade to >= 1.64")
    check("conflict flags available", info.supports_conflict_flags,
          "" if info.supports_conflict_flags else "needs >= 1.66")

    problems = cfg.validate()
    check("config valid", not problems, "; ".join(problems))
    check("remote configured in rclone", rclone.remote_exists(cfg, info), cfg.remote)
    check("local folder exists", cfg.local_path.is_dir(), str(cfg.local_path))

    backup = cfg.backup_local_path
    if backup:
        check("local backup folder outside synced folder",
              cfg.local_path not in Path(os.path.abspath(backup)).parents, str(backup))

    from .indicator import backend_name, have_appindicator
    check("tray backend", True, backend_name())
    if not have_appindicator():
        print("       hint: sudo apt install gir1.2-ayatanaappindicator3-0.1")

    try:
        acc = account.fetch(cfg, info, force=True)
        check("account reachable", bool(acc.email or acc.quota_limit),
              acc.identity_label)
    except Exception as exc:
        check("account reachable", False, str(exc))

    return 0 if ok else 1


# -------------------------------------------------------------------- dispatch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gdrive-client",
        description="Two-way Google Drive sync with a tray icon.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tray", help="run the tray application (default)")
    sub.add_parser("status", help="print current status and account info")
    sub.add_parser("explain", help="show how settings map to rsync/rclone flags")
    sub.add_parser("doctor", help="check the setup and report problems")

    p_sync = sub.add_parser("sync", help="run one sync now and exit")
    p_sync.add_argument("--resync", action="store_true",
                        help="rebuild the baseline (needed after errors ask for it)")
    p_sync.add_argument("-n", "--dry-run", action="store_true",
                        help="show what would happen, change nothing")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    cfg = bootstrap_config()

    allow_download = args.command != "doctor"
    try:
        info = rclone.resolve(cfg, allow_download=allow_download)
    except rclone.RcloneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    command = args.command or "tray"

    if command == "tray":
        try:
            lock = engine_mod._acquire_lock()
        except engine_mod.AlreadyRunning as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4
        try:
            return run_tray(cfg, info)
        finally:
            lock.close()

    if command == "status":
        return cmd_status(cfg, info)
    if command == "explain":
        return cmd_explain(cfg, info)
    if command == "doctor":
        return cmd_doctor(cfg, info)
    if command == "sync":
        try:
            lock = engine_mod._acquire_lock()
        except engine_mod.AlreadyRunning as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4
        try:
            return cmd_sync(cfg, info, resync=args.resync, dry_run=args.dry_run)
        finally:
            lock.close()

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
