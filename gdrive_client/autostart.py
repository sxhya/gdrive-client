"""Login autostart, via the XDG autostart spec.

A .desktop file in ~/.config/autostart is honoured by GNOME, Unity and XFCE
alike, which is why it is used in preference to a systemd user unit (XFCE 20.04
sessions do not reliably start user units for graphical apps).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Google Drive client
Comment=Two-way Google Drive sync with a tray icon
Exec={exec_line}
Icon=gdrive-client
Terminal=false
Categories=Network;FileTransfer;
X-GNOME-Autostart-enabled=true
StartupNotify=false
"""


def launch_command() -> str:
    """Best guess at how to relaunch this app.

    Prefers an installed `gdrive-client` on PATH, and otherwise re-invokes the
    same interpreter against the checked-out source tree so a git clone works
    without installing anything.
    """
    installed = shutil.which("gdrive-client")
    if installed:
        return installed
    root = Path(__file__).resolve().parent.parent
    return f"{sys.executable} -m gdrive_client"


def is_enabled() -> bool:
    return paths.AUTOSTART_FILE.exists()


def set_enabled(enabled: bool) -> None:
    target = paths.AUTOSTART_FILE
    if not enabled:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", target, exc)
        return

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        exec_line = launch_command()
        working_dir = Path(__file__).resolve().parent.parent
        body = TEMPLATE.format(exec_line=exec_line)
        if not shutil.which("gdrive-client"):
            # Running from a source checkout: the module form needs the tree on
            # sys.path, which Path=... gives us without touching PYTHONPATH.
            body += f"Path={working_dir}\n"
        target.write_text(body, encoding="utf-8")
        os.chmod(target, 0o644)
    except OSError as exc:
        log.warning("could not write %s: %s", target, exc)
