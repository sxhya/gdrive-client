"""The Preferences window: a GTK front end for ~/.gdrive-settings.

Every control carries a tooltip naming the rsync flag it corresponds to, so the
settings stay recognisable to anyone used to driving rsync by hand.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from . import autostart, rclone
from .config import (
    COMPARE_MODES,
    CONFLICT_LOSERS,
    CONFLICT_POLICIES,
    CONFIG_PATH,
    RESYNC_MODES,
    RSYNC_EQUIVALENT,
    Config,
)

log = logging.getLogger(__name__)

_POLICY_BLURB = {
    "keep-both": "Keep both versions side by side (safest)",
    "newer-wins": "The newer modification time wins",
    "prefer-local": "The local copy always wins",
    "prefer-remote": "The Drive copy always wins",
}
_LOSER_BLURB = {
    "keep-suffixed": "Keep it, renamed with the suffix below",
    "numbered": "Keep it as name.1, name.2, …",
    "delete": "Discard it",
}
_COMPARE_BLURB = {
    "size,modtime": "Size and modification time (default)",
    "checksum": "Checksum — slow, immune to clock skew",
    "size-only": "Size only — fastest, misses same-size edits",
}


def _tooltip_for(option: str, extra: str = "") -> str:
    mapping = RSYNC_EQUIVALENT.get(option)
    parts = [extra] if extra else []
    if mapping:
        rsync_flag, rclone_flag = mapping
        parts.append(f"rsync: {rsync_flag}\nrclone: {rclone_flag}")
    return "\n\n".join(parts)


class PreferencesDialog(Gtk.Window):
    def __init__(self, cfg: Config, on_saved=None, on_closed=None):
        super().__init__(title="Google Drive — Preferences")
        self.cfg = cfg
        self._on_saved = on_saved
        self._on_closed = on_closed
        self._widgets: dict[str, Gtk.Widget] = {}

        # Fits on a 768-pixel-high screen; the tabs are split so no single one
        # needs to scroll at this height.
        self.set_default_size(700, 600)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("gdrive-client")
        self.connect("destroy", self._on_destroy)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        self.notebook = Gtk.Notebook()
        self.notebook.set_border_width(8)
        outer.pack_start(self.notebook, True, True, 0)

        self._build_account_tab()
        self._build_sync_tab()
        self._build_safety_tab()
        self._build_transfer_tab()
        self._build_filters_tab()
        self._build_interface_tab()
        self._build_advanced_tab()

        self.error_bar = Gtk.InfoBar()
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        self.error_label = Gtk.Label(label="")
        self.error_label.set_line_wrap(True)
        self.error_label.set_xalign(0.0)
        self.error_bar.get_content_area().add(self.error_label)
        self.error_bar.set_no_show_all(True)
        outer.pack_start(self.error_bar, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_border_width(10)

        edit_btn = Gtk.Button(label="Edit file directly…")
        edit_btn.set_tooltip_text(f"Open {CONFIG_PATH} in your default text editor")
        edit_btn.connect("clicked", self._on_edit_file)
        buttons.pack_start(edit_btn, False, False, 0)

        reset_btn = Gtk.Button(label="Restore defaults")
        reset_btn.connect("clicked", self._on_reset)
        buttons.pack_start(reset_btn, False, False, 0)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.destroy())
        buttons.pack_end(cancel, False, False, 0)

        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.pack_end(save, False, False, 0)

        outer.pack_start(buttons, False, False, 0)

        self._load_into_widgets(cfg)
        self.show_all()
        self.error_bar.hide()

    # ------------------------------------------------------------- tab builder

    def _new_page(self, title: str, blurb: str = "") -> Gtk.Grid:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)

        if blurb:
            label = Gtk.Label()
            label.set_markup(f"<i>{GLib_escape(blurb)}</i>")
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            box.pack_start(label, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_column_homogeneous(False)
        box.pack_start(grid, False, False, 0)
        scroller.add(box)
        self.notebook.append_page(scroller, Gtk.Label(label=title))
        grid._row = 0  # type: ignore[attr-defined]
        return grid

    def _add_row(self, grid: Gtk.Grid, label_text: str, widget: Gtk.Widget,
                 option: str = "", hint: str = "") -> Gtk.Widget:
        row = grid._row  # type: ignore[attr-defined]
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        grid.attach(label, 0, row, 1, 1)

        widget.set_hexpand(True)
        if isinstance(widget, (Gtk.Switch, Gtk.CheckButton)):
            holder = Gtk.Box()
            widget.set_halign(Gtk.Align.START)
            widget.set_hexpand(False)
            holder.pack_start(widget, False, False, 0)
            grid.attach(holder, 1, row, 1, 1)
        else:
            grid.attach(widget, 1, row, 1, 1)

        tip = _tooltip_for(option, hint) if (option or hint) else ""
        if tip:
            label.set_tooltip_text(tip)
            widget.set_tooltip_text(tip)

        grid._row = row + 1  # type: ignore[attr-defined]
        if option:
            self._widgets[option] = widget
        return widget

    def _add_section(self, grid: Gtk.Grid, title: str) -> None:
        row = grid._row  # type: ignore[attr-defined]
        label = Gtk.Label()
        label.set_markup(f"<b>{GLib_escape(title)}</b>")
        label.set_xalign(0.0)
        label.set_margin_top(10 if row else 0)
        grid.attach(label, 0, row, 2, 1)
        grid._row = row + 1  # type: ignore[attr-defined]

    # ------------------------------------------------------------------- tabs

    def _build_account_tab(self) -> None:
        grid = self._new_page(
            "Account",
            "Which Drive to sync, and where it lives on this machine.",
        )
        combo = Gtk.ComboBoxText.new_with_entry()
        for name in self._list_remotes():
            combo.append_text(name)
        self._add_row(grid, "rclone remote", combo, "remote",
                      "Configure remotes with: rclone config")

        self._add_row(grid, "Subfolder on Drive", Gtk.Entry(), "remote_path",
                      "Leave empty to sync the whole Drive.")

        chooser = Gtk.FileChooserButton(
            title="Local folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self._add_row(grid, "Local folder", chooser, "local_folder")

    def _build_sync_tab(self) -> None:
        grid = self._new_page(
            "Sync",
            "When to sync, and what to do when both sides changed the same file.",
        )
        self._add_row(grid, "Syncing enabled", Gtk.Switch(), "enabled")
        self._add_row(grid, "Paused", Gtk.Switch(), "paused",
                      "Same as the tray's Pause syncing item.")
        self._add_row(grid, "Sync on startup", Gtk.Switch(), "sync_on_start")
        self._add_row(grid, "Interval (minutes)",
                      Gtk.SpinButton.new_with_range(1, 1440, 1), "interval_minutes")

        self._add_section(grid, "Conflicts")
        self._add_row(grid, "Winner", self._combo(CONFLICT_POLICIES, _POLICY_BLURB),
                      "conflict_policy")
        self._add_row(grid, "Loser", self._combo(CONFLICT_LOSERS, _LOSER_BLURB),
                      "conflict_loser")
        self._add_row(grid, "Suffix(es)", Gtk.Entry(), "conflict_suffix",
                      "One value, or two comma-separated values for the local/Drive sides.")

    def _build_safety_tab(self) -> None:
        grid = self._new_page(
            "Safety",
            "Guards against a bad sync, and how to recover from one.",
        )
        self._add_row(grid, "Dry run (change nothing)", Gtk.Switch(), "dry_run")
        self._add_row(grid, "Abort above deleted %",
                      Gtk.SpinButton.new_with_range(0, 100, 5), "max_delete_percent",
                      "The guard the old gdrive script lacked. 100 disables it.")
        self._add_row(grid, "Local backup folder", Gtk.Entry(), "backup_dir_local",
                      "Overwritten and deleted local files are kept here. "
                      "Must be outside the synced folder. Empty disables it.")
        self._add_row(grid, "Drive backup folder", Gtk.Entry(), "backup_dir_remote",
                      "Same, on the Drive side. Empty disables it; Drive has its own trash.")
        self._add_row(grid, "Require sentinel file", Gtk.Switch(), "check_access",
                      "Refuse to sync unless RCLONE_TEST exists on both sides. "
                      "Guards against syncing into an empty or unmounted folder.")
        self._add_row(grid, "Remove empty folders", Gtk.Switch(), "remove_empty_dirs")

        self._add_section(grid, "Recovery")
        self._add_row(grid, "Retry after minor errors", Gtk.Switch(), "resilient")
        self._add_row(grid, "Auto-recover interruptions", Gtk.Switch(), "recover")
        self._add_row(grid, "First-run winner", self._combo(RESYNC_MODES), "resync_mode",
                      "On the very first sync, which copy wins when a path already "
                      "exists on both sides with different contents.")

    def _build_transfer_tab(self) -> None:
        grid = self._new_page(
            "Transfer",
            "Throughput and how a file is judged to have changed.",
        )
        self._add_row(grid, "Parallel transfers",
                      Gtk.SpinButton.new_with_range(1, 32, 1), "transfers",
                      "rsync is single-stream; this is a genuine improvement over it.")
        self._add_row(grid, "Parallel checkers",
                      Gtk.SpinButton.new_with_range(1, 64, 1), "checkers")
        self._add_row(grid, "Upload limit", Gtk.Entry(), "bwlimit_up",
                      "e.g. 2M or 500k. Empty means unlimited.")
        self._add_row(grid, "Download limit", Gtk.Entry(), "bwlimit_down",
                      "e.g. 2M or 500k. Empty means unlimited.")
        self._add_row(grid, "Compare files by", self._combo(COMPARE_MODES, _COMPARE_BLURB),
                      "compare")
        self._add_row(grid, "Timestamp tolerance", Gtk.Entry(), "modify_window",
                      "Measured on this Drive: the FUSE mount reports whole seconds "
                      "while ext4 stores nanoseconds, so 0 causes endless re-transfers.")
        self._add_row(grid, "Retries",
                      Gtk.SpinButton.new_with_range(1, 20, 1), "retries")

    def _build_filters_tab(self) -> None:
        grid = self._new_page(
            "Filters",
            "Paths to leave alone entirely, in both directions.",
        )
        self._add_row(grid, "Max file size", Gtk.Entry(), "max_size",
                      "e.g. 1G. Empty means no limit.")
        self._add_row(grid, "Keep symlinks as links", Gtk.Switch(), "preserve_symlinks")
        self._add_row(grid, "Download Google-flagged files", Gtk.Switch(),
                      "acknowledge_abuse",
                      "Google's scanner flags harmless old .exe/.rar/.7z files and "
                      "refuses to serve them. With this off, one such file aborts "
                      "every sync and leaves it stuck asking for a re-baseline. "
                      "rclone only writes them to disk; nothing is executed.")

        self._add_section(grid, "Exclude patterns (one per line)")
        view = Gtk.TextView()
        view.set_monospace(True)
        view.set_tooltip_text(_tooltip_for(
            "exclude",
            "One glob per line. A pattern without a slash matches at any depth.",
        ))
        frame = Gtk.Frame()
        frame.set_size_request(-1, 190)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        frame.add(scroller)
        row = grid._row  # type: ignore[attr-defined]
        grid.attach(frame, 0, row, 2, 1)
        grid._row = row + 1  # type: ignore[attr-defined]
        self._widgets["exclude"] = view

    def _build_interface_tab(self) -> None:
        grid = self._new_page("Interface", "The tray icon and its notifications.")
        self._add_row(grid, "Recent files shown",
                      Gtk.SpinButton.new_with_range(0, 50, 1), "recent_files_count")
        self._add_row(grid, "Show notifications", Gtk.Switch(), "show_notifications")
        self._add_row(grid, "Notify on conflicts", Gtk.Switch(), "notify_on_conflict")
        self._add_row(grid, "Notify on errors", Gtk.Switch(), "notify_on_error")
        self._add_row(grid, "Start on login", Gtk.Switch(), "autostart")

    def _build_advanced_tab(self) -> None:
        grid = self._new_page(
            "Advanced",
            "Only needed if the defaults do not fit your machine.",
        )
        self._add_row(grid, "rclone binary", Gtk.Entry(), "rclone_path",
                      "Empty means autodetect, downloading a current static build "
                      "if the system rclone is too old for bisync.")
        self._add_row(grid, "Extra rclone flags", Gtk.Entry(), "extra_flags")
        self._add_row(grid, "Log level", self._combo(("INFO", "DEBUG")), "log_level",
                      "Quieter levels are not offered: rclone reports per-file "
                      "actions at INFO, which the Recently changed list needs.")

        self._add_section(grid, "Detected")
        self.detected_label = Gtk.Label(label="…")
        self.detected_label.set_xalign(0.0)
        self.detected_label.set_line_wrap(True)
        self.detected_label.set_selectable(True)
        row = grid._row  # type: ignore[attr-defined]
        grid.attach(self.detected_label, 0, row, 2, 1)
        grid._row = row + 1  # type: ignore[attr-defined]
        self._refresh_detected()

    # ------------------------------------------------------------- utilities

    @staticmethod
    def _combo(values, blurbs: dict | None = None) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for value in values:
            text = f"{value} — {blurbs[value]}" if blurbs and value in blurbs else value
            combo.append(value, text)
        return combo

    @staticmethod
    def _list_remotes() -> list[str]:
        try:
            proc = subprocess.run(
                ["rclone", "listremotes"], capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0:
                return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
        return []

    def _refresh_detected(self) -> None:
        from .indicator import backend_name
        lines = [f"Tray backend: {backend_name()}", f"Config file: {CONFIG_PATH}"]
        try:
            info = rclone.resolve(self.cfg, allow_download=False)
            lines.append(f"rclone: {info}")
            if not info.bisync_is_stable:
                lines.append(
                    "⚠ This rclone still labels bisync EXPERIMENTAL. A current "
                    "static build will be downloaded on next start."
                )
        except rclone.RcloneError as exc:
            lines.append(f"rclone: not usable — {exc}")
        self.detected_label.set_text("\n".join(lines))

    # ------------------------------------------------------- load / collect

    def _load_into_widgets(self, cfg: Config) -> None:
        for option, widget in self._widgets.items():
            if option == "exclude":
                widget.get_buffer().set_text("\n".join(cfg.exclude))
                continue
            if option == "autostart":
                widget.set_active(autostart.is_enabled())
                continue

            value = getattr(cfg, option)
            if isinstance(widget, Gtk.Switch):
                widget.set_active(bool(value))
            elif isinstance(widget, Gtk.SpinButton):
                widget.set_value(int(value))
            elif isinstance(widget, Gtk.FileChooserButton):
                widget.set_filename(str(Path(value).expanduser()))
            elif isinstance(widget, Gtk.ComboBoxText):
                if widget.get_has_entry():
                    widget.get_child().set_text(str(value))
                else:
                    widget.set_active_id(str(value))
            elif isinstance(widget, Gtk.Entry):
                widget.set_text(str(value))

    def _collect(self) -> tuple[Config, bool]:
        """Build a Config from the widgets. Returns (config, autostart_wanted)."""
        new = Config(**{k: v for k, v in vars(self.cfg).items()})
        want_autostart = autostart.is_enabled()

        for option, widget in self._widgets.items():
            if option == "exclude":
                buf = widget.get_buffer()
                text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                new.exclude = [ln.strip() for ln in text.splitlines() if ln.strip()]
                continue
            if option == "autostart":
                want_autostart = widget.get_active()
                new.autostart = want_autostart
                continue

            if isinstance(widget, Gtk.Switch):
                setattr(new, option, widget.get_active())
            elif isinstance(widget, Gtk.SpinButton):
                setattr(new, option, int(widget.get_value()))
            elif isinstance(widget, Gtk.FileChooserButton):
                setattr(new, option, widget.get_filename() or getattr(self.cfg, option))
            elif isinstance(widget, Gtk.ComboBoxText):
                if widget.get_has_entry():
                    setattr(new, option, widget.get_child().get_text().strip())
                else:
                    setattr(new, option, widget.get_active_id() or getattr(self.cfg, option))
            elif isinstance(widget, Gtk.Entry):
                setattr(new, option, widget.get_text().strip())

        return new, want_autostart

    # ---------------------------------------------------------------- actions

    def _show_error(self, message: str) -> None:
        self.error_label.set_text(message)
        self.error_bar.show()

    def _on_save(self, _button) -> None:
        new_cfg, want_autostart = self._collect()
        problems = new_cfg.validate()
        if problems:
            self._show_error("Cannot save:\n• " + "\n• ".join(problems))
            return

        try:
            new_cfg.save()
        except OSError as exc:
            self._show_error(f"Could not write {CONFIG_PATH}: {exc}")
            return

        autostart.set_enabled(want_autostart)
        self.error_bar.hide()
        if self._on_saved:
            self._on_saved(new_cfg)
        self.destroy()

    def _on_reset(self, _button) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Restore default settings?",
        )
        dialog.format_secondary_text(
            "The remote and local folder are kept; everything else returns to its "
            "default. Nothing is written until you press Save."
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        defaults = Config(
            remote=self.cfg.remote,
            remote_path=self.cfg.remote_path,
            local_folder=self.cfg.local_folder,
        )
        self._load_into_widgets(defaults)

    def _on_edit_file(self, _button) -> None:
        if not CONFIG_PATH.exists():
            try:
                self.cfg.save()
            except OSError as exc:
                self._show_error(f"Could not create {CONFIG_PATH}: {exc}")
                return
        try:
            subprocess.Popen(
                ["xdg-open", str(CONFIG_PATH)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._show_error(f"Could not open an editor: {exc}")

    def _on_destroy(self, _widget) -> None:
        if self._on_closed:
            self._on_closed()


def GLib_escape(text: str) -> str:
    """Minimal Pango markup escaping for the labels built above."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
