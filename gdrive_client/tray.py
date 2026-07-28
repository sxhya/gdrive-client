"""The tray icon and its menu."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
except (ValueError, ImportError):  # pragma: no cover - depends on the host
    Notify = None

from . import account, paths, rclone
from .config import Config
from .engine import State, SyncEngine, Status
from .indicator import TrayIcon, backend_name
from .parser import Change
from .prefs import PreferencesDialog

log = logging.getLogger(__name__)

APP_ID = "gdrive-client"

_STATE_ICON = {
    State.STARTING: "gdrive-client-idle",
    State.IDLE: "gdrive-client-idle",
    State.SYNCING: "gdrive-client-syncing",
    State.PAUSED: "gdrive-client-paused",
    State.DISABLED: "gdrive-client-paused",
    State.ERROR: "gdrive-client-error",
    State.NEEDS_RESYNC: "gdrive-client-attention",
    State.SAFETY_ABORT: "gdrive-client-attention",
    State.MISCONFIGURED: "gdrive-client-error",
    State.BLOCKED: "gdrive-client-attention",
}

_STATE_TEXT = {
    State.STARTING: "Starting…",
    State.IDLE: "Up to date",
    State.SYNCING: "Syncing…",
    State.PAUSED: "Paused",
    State.DISABLED: "Syncing off",
    State.ERROR: "Sync error",
    State.NEEDS_RESYNC: "Needs re-baseline",
    State.SAFETY_ABORT: "Stopped for safety",
    State.MISCONFIGURED: "Not configured",
    State.BLOCKED: "Some files could not sync",
}


def _ago(when: float) -> str:
    if not when:
        return "never"
    delta = max(0, int(time.time() - when))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        hours = delta // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = delta // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def _open_uri(uri: str) -> None:
    """Open a URI with the desktop's handler.

    xdg-open is used rather than Gio.AppInfo because it is the one thing that
    behaves the same on GNOME and on a bare XFCE session.
    """
    try:
        subprocess.Popen(
            ["xdg-open", uri],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        log.warning("could not open %s: %s", uri, exc)


class Tray:
    def __init__(self, cfg: Config, info: rclone.RcloneInfo, engine: SyncEngine):
        self.cfg = cfg
        self.info = info
        self.engine = engine
        self.account = account.AccountInfo()
        self._changes: list[Change] = engine.recent_changes()
        self._prefs_dialog: PreferencesDialog | None = None
        self._suppress_pause_signal = False

        data_root = Path(__file__).resolve().parent.parent / "data" / "icons"
        self.icon = TrayIcon(
            APP_ID,
            icon_theme_path=data_root / "hicolor" / "scalable" / "apps",
            png_dir=data_root / "hicolor" / "22x22" / "apps",
            title="Google Drive",
        )
        self.icon.set_icon("gdrive-client-idle", "Google Drive")

        if Notify is not None:
            Notify.init("Google Drive")

        self._build_menu()
        self.icon.set_activate_callback(self.on_open_folder)
        self._refresh_account_async()
        # Keep the "x min ago" line honest without any sync traffic.
        GLib.timeout_add_seconds(60, self._tick)

    # ---------------------------------------------------------------- menu

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

        self.item_status = self._info_item("Starting…")
        menu.append(self.item_status)
        self.item_detail = self._info_item("")
        menu.append(self.item_detail)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem(label="Open Google Drive folder")
        item.connect("activate", lambda _i: self.on_open_folder())
        menu.append(item)
        self._folder_item = item

        item = Gtk.MenuItem(label="Launch Google Drive website")
        item.connect("activate", lambda _i: _open_uri(paths.DRIVE_WEB_URL))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Account info: present but deliberately not selectable.
        self.item_email = self._info_item("Loading account…")
        menu.append(self.item_email)
        self.item_quota = self._info_item("")
        menu.append(self.item_quota)
        self.item_quota_detail = self._info_item("")
        menu.append(self.item_quota_detail)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_recent = Gtk.MenuItem(label="Recently changed")
        self.recent_menu = Gtk.Menu()
        self.item_recent.set_submenu(self.recent_menu)
        menu.append(self.item_recent)

        menu.append(Gtk.SeparatorMenuItem())

        self.item_sync = Gtk.MenuItem(label="Sync now")
        self.item_sync.connect("activate", lambda _i: self.on_sync_now())
        menu.append(self.item_sync)

        self.item_resync = Gtk.MenuItem(label="Sync now (rebuild baseline)…")
        self.item_resync.connect("activate", lambda _i: self.on_resync())
        menu.append(self.item_resync)

        self.item_pause = Gtk.CheckMenuItem(label="Pause syncing")
        self.item_pause.set_active(self.cfg.paused)
        self.item_pause.connect("toggled", self.on_pause_toggled)
        menu.append(self.item_pause)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem(label="Preferences…")
        item.connect("activate", lambda _i: self.on_preferences())
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem(label="Quit")
        item.connect("activate", lambda _i: self.on_quit())
        menu.append(item)

        self.menu = menu
        self.icon.set_menu(menu)
        self.icon.set_secondary_activate(self.item_sync)
        self._rebuild_recent()

    @staticmethod
    def _info_item(text: str) -> Gtk.MenuItem:
        """A label-only row: visible, never clickable."""
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        return item

    def _rebuild_recent(self) -> None:
        for child in self.recent_menu.get_children():
            self.recent_menu.remove(child)

        if not self._changes:
            self.recent_menu.append(self._info_item("Nothing changed yet"))
        else:
            for change in self._changes:
                label = f"{change.label}   {change.path}"
                if len(label) > 72:
                    label = label[:69] + "…"
                item = Gtk.MenuItem(label=label)
                item.set_tooltip_text(
                    f"{change.path}\n{change.label} · {_ago(change.at)}\n"
                    "Click to open the containing folder."
                )
                item.connect("activate", self._on_recent_activate, change.path)
                self.recent_menu.append(item)

            self.recent_menu.append(Gtk.SeparatorMenuItem())
            log_item = Gtk.MenuItem(label="Open sync log")
            log_item.connect("activate", lambda _i: _open_uri(paths.LOG_FILE.as_uri()))
            self.recent_menu.append(log_item)

        self.recent_menu.show_all()

    def _on_recent_activate(self, _item, relative_path: str) -> None:
        """Open the folder containing the changed file.

        Opening the file itself is unhelpful when the change was a deletion, and
        a deleted path has no parent to show either, so fall back upwards until
        something exists.
        """
        target = (self.cfg.local_path / relative_path).parent
        while not target.exists() and target != target.parent:
            target = target.parent
        _open_uri(target.as_uri())

    # ------------------------------------------------------------- callbacks

    def on_open_folder(self) -> None:
        local = self.cfg.local_path
        try:
            local.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.notify("Google Drive", f"Cannot open {local}: {exc}", urgent=True)
            return
        _open_uri(local.as_uri())

    def on_sync_now(self) -> None:
        if self.engine.is_syncing():
            self.notify("Google Drive", "A sync is already running.")
            return
        self.engine.sync_now()

    def on_resync(self) -> None:
        """Rebuild the bisync baseline, with a confirmation because it is not free."""
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Rebuild the sync baseline?",
        )
        dialog.format_secondary_text(
            "This re-reads both sides in full and re-establishes what is in sync. "
            "It does not delete files: where the same path exists on both sides and "
            f"the contents differ, the '{self.cfg.resync_mode}' copy is kept.\n\n"
            "Use this after a sync error asks for it, or after changing the local "
            "folder or remote."
        )
        dialog.set_keep_above(True)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.engine.sync_now(resync=True)

    def on_pause_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if self._suppress_pause_signal:
            return
        self.engine.set_paused(item.get_active())

    def on_preferences(self) -> None:
        if self._prefs_dialog is not None:
            self._prefs_dialog.present()
            return

        def on_saved(new_cfg: Config) -> None:
            self.cfg = new_cfg
            self.engine.apply_config(new_cfg)
            self._suppress_pause_signal = True
            self.item_pause.set_active(new_cfg.paused)
            self._suppress_pause_signal = False
            self._changes = self.engine.recent_changes()
            self._rebuild_recent()
            self._refresh_account_async(force=True)

        def on_closed() -> None:
            self._prefs_dialog = None

        self._prefs_dialog = PreferencesDialog(
            self.cfg, on_saved=on_saved, on_closed=on_closed
        )
        self._prefs_dialog.present()

    def on_quit(self) -> None:
        self.engine.stop()
        if Notify is not None:
            Notify.uninit()
        Gtk.main_quit()

    # ------------------------------------------------------- engine plumbing

    def on_status(self, status: Status) -> None:
        """Called from the sync thread; hop to the GTK thread."""
        GLib.idle_add(self._apply_status, status)

    def on_changes(self, changes: list[Change]) -> None:
        GLib.idle_add(self._apply_changes, changes)

    def notify(self, title: str, body: str, urgent: bool = False) -> None:
        GLib.idle_add(self._show_notification, title, body, urgent)

    def _apply_status(self, status: Status) -> bool:
        icon = _STATE_ICON.get(status.state, "gdrive-client-idle")
        self.icon.set_icon(icon, _STATE_TEXT.get(status.state, ""))

        headline = _STATE_TEXT.get(status.state, status.state.value)
        if status.state is State.IDLE and status.last_success_at:
            headline = f"Up to date · {_ago(status.last_success_at)}"
        self.item_status.set_label(headline)

        detail = status.message or ""
        if status.state.is_problem and status.last_error:
            detail = status.last_error
        if len(detail) > 90:
            detail = detail[:87] + "…"
        self.item_detail.set_label(detail)
        self.item_detail.set_visible(bool(detail))

        self.icon.set_tooltip(f"Google Drive — {headline}")

        syncing = status.state is State.SYNCING
        self.item_sync.set_sensitive(not syncing)
        self.item_resync.set_sensitive(not syncing)
        # Offer the rebuild prominently only when it is actually the fix.
        self.item_resync.set_label(
            "Sync now (rebuild baseline)…"
            if status.state is not State.NEEDS_RESYNC
            else "Rebuild baseline now (required)…"
        )
        return False

    def _apply_changes(self, changes: list[Change]) -> bool:
        self._changes = changes
        self._rebuild_recent()
        return False

    def _show_notification(self, title: str, body: str, urgent: bool) -> bool:
        if Notify is None or not self.cfg.show_notifications:
            log.info("notification: %s - %s", title, body)
            return False
        try:
            note = Notify.Notification.new(title, body, "gdrive-client-idle")
            if urgent:
                note.set_urgency(Notify.Urgency.CRITICAL)
            note.show()
        except Exception as exc:
            log.warning("could not show notification: %s", exc)
        return False

    # ---------------------------------------------------------------- account

    def _refresh_account_async(self, force: bool = False) -> None:
        import threading

        def work() -> None:
            try:
                info = account.fetch(self.cfg, self.info, force=force)
            except Exception as exc:
                log.warning("account lookup failed: %s", exc)
                return
            GLib.idle_add(self._apply_account, info)

        threading.Thread(target=work, name="account", daemon=True).start()

    def _apply_account(self, info: account.AccountInfo) -> bool:
        self.account = info
        self.item_email.set_label(info.identity_label)
        self.item_quota.set_label(info.quota_label)
        detail = info.drive_detail_label
        self.item_quota_detail.set_label(detail)
        self.item_quota_detail.set_visible(bool(detail))
        return False

    def _tick(self) -> bool:
        self._apply_status(self.engine.status)
        # Refresh quota roughly hourly; account.fetch caches, so this is cheap.
        if time.time() - self.account.fetched_at > account.CACHE_TTL:
            self._refresh_account_async()
        return True
