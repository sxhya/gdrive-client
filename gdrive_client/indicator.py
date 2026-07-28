"""Tray icon backend selection.

Three tiers, because no single API covers the two targets:

  1. AyatanaAppIndicator3 - current Ubuntu (24.04 GNOME needs the
     gnome-shell-extension-appindicator, which Ubuntu ships and enables).
  2. AppIndicator3 - the original libappindicator, which is what Unity and
     older Xubuntu/XFCE panels expose.
  3. Gtk.StatusIcon - deprecated since GTK 3.14 but still functional, and it is
     the only one of the three that needs no extra typelib.  This is the safety
     net for a bare Xubuntu 20.04 install.

Both indicator libraries want an icon *name* plus a theme search path rather
than a file, while StatusIcon wants a file; the wrapper hides that difference.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

log = logging.getLogger(__name__)

_APPINDICATOR = None
_FLAVOUR = ""

for _namespace, _module in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(_namespace, _module)
        _APPINDICATOR = getattr(
            __import__("gi.repository", fromlist=[_namespace]), _namespace
        )
        _FLAVOUR = _namespace
        break
    except (ValueError, ImportError, AttributeError):
        continue


def backend_name() -> str:
    return _FLAVOUR or "Gtk.StatusIcon"


def have_appindicator() -> bool:
    return _APPINDICATOR is not None


class TrayIcon:
    """One tray icon with a menu, on whichever backend is available."""

    def __init__(self, app_id: str, icon_theme_path: Path, png_dir: Path, title: str):
        self._png_dir = png_dir
        self._icon_name = ""
        self._menu: Gtk.Menu | None = None
        self._activate_cb = None

        if _APPINDICATOR is not None:
            self._impl = _APPINDICATOR.Indicator.new(
                app_id, "", _APPINDICATOR.IndicatorCategory.APPLICATION_STATUS
            )
            self._impl.set_icon_theme_path(str(icon_theme_path))
            self._impl.set_status(_APPINDICATOR.IndicatorStatus.ACTIVE)
            if hasattr(self._impl, "set_title"):
                self._impl.set_title(title)
            self._kind = "appindicator"
        else:
            self._impl = Gtk.StatusIcon()
            self._impl.set_title(title)
            self._impl.set_visible(True)
            self._impl.connect("popup-menu", self._on_status_icon_popup)
            self._impl.connect("activate", self._on_status_icon_activate)
            self._kind = "statusicon"
            log.warning(
                "no AppIndicator typelib found; falling back to the deprecated "
                "Gtk.StatusIcon. Install gir1.2-ayatanaappindicator3-0.1 "
                "(or gir1.2-appindicator3-0.1) for a better tray experience."
            )

    # ------------------------------------------------------------------ icon

    def set_icon(self, name: str, description: str = "") -> None:
        """Set the icon by base name, e.g. 'gdrive-client-syncing'."""
        if name == self._icon_name:
            return
        self._icon_name = name
        if self._kind == "appindicator":
            self._impl.set_icon_full(name, description or name)
        else:
            png = self._png_dir / f"{name}.png"
            if png.exists():
                self._impl.set_from_file(str(png))
            else:
                self._impl.set_from_icon_name(name)

    def set_tooltip(self, text: str) -> None:
        if self._kind == "appindicator":
            # libappindicator has no tooltip concept; the title is the closest
            # thing and is what GNOME shows on hover.
            if hasattr(self._impl, "set_title"):
                self._impl.set_title(text)
        else:
            self._impl.set_tooltip_text(text)

    # ------------------------------------------------------------------ menu

    def set_menu(self, menu: Gtk.Menu) -> None:
        self._menu = menu
        menu.show_all()
        if self._kind == "appindicator":
            self._impl.set_menu(menu)

    def set_secondary_activate(self, item: Gtk.MenuItem) -> None:
        """Middle-click target, where the backend supports one."""
        if self._kind == "appindicator" and hasattr(self._impl, "set_secondary_activate_target"):
            self._impl.set_secondary_activate_target(item)

    def set_activate_callback(self, callback) -> None:
        """Left-click handler; only meaningful for the StatusIcon backend."""
        self._activate_cb = callback

    # StatusIcon needs explicit popup handling.
    def _on_status_icon_popup(self, icon, button, activate_time):
        if self._menu is not None:
            self._menu.popup(None, None, Gtk.StatusIcon.position_menu,
                             icon, button, activate_time)

    def _on_status_icon_activate(self, icon):
        if self._activate_cb is not None:
            self._activate_cb()
        elif self._menu is not None:
            self._menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, 1, 0)
