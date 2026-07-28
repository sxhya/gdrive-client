#!/usr/bin/env bash
# Install gdrive-client for the current user (no root needed except for the
# optional apt packages, which the script only ever suggests).
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '%s[fail]%s %s\n' "$RED" "$RST" "$*" >&2; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/share/gdrive-client"
ICON_DIR="$PREFIX/share/icons/hicolor"
APP_DIR="$PREFIX/share/applications"

say "${BLD}gdrive-client installer${RST}"
say "  source : $SRC"
say "  prefix : $PREFIX"
say ""

# ---------------------------------------------------------------- dependencies

say "${BLD}Checking dependencies${RST}"

missing_apt=()

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  err "python3 >= 3.9 is required."
  exit 1
fi
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"

if python3 -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null; then
  ok "PyGObject with GTK 3"
else
  err "PyGObject / GTK 3 is missing."
  missing_apt+=(python3-gi gir1.2-gtk-3.0)
fi

# The tray needs one of these typelibs for a modern indicator; without either we
# fall back to the deprecated Gtk.StatusIcon, which GNOME no longer displays.
tray_backend=""
for ns in AyatanaAppIndicator3 AppIndicator3; do
  if python3 -c "import gi; gi.require_version('$ns','0.1')" 2>/dev/null; then
    tray_backend="$ns"; break
  fi
done
if [[ -n "$tray_backend" ]]; then
  ok "tray backend: $tray_backend"
else
  warn "no AppIndicator typelib -> will fall back to Gtk.StatusIcon (invisible on GNOME)."
  if apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
    missing_apt+=(gir1.2-ayatanaappindicator3-0.1)
  else
    missing_apt+=(gir1.2-appindicator3-0.1)   # Ubuntu 20.04 and older
  fi
fi

if python3 -c "import gi; gi.require_version('Notify','0.7')" 2>/dev/null; then
  ok "libnotify bindings"
else
  warn "libnotify bindings missing -> notifications will be logged instead of shown."
  missing_apt+=(gir1.2-notify-0.7)
fi

# GNOME hides all tray icons unless this extension is present and enabled.
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
  if dpkg -l gnome-shell-extension-appindicator 2>/dev/null | grep -q '^ii'; then
    ok "GNOME AppIndicator extension installed"
  else
    warn "GNOME needs gnome-shell-extension-appindicator to show tray icons."
    missing_apt+=(gnome-shell-extension-appindicator)
  fi
fi

if command -v rclone >/dev/null 2>&1; then
  rc_ver="$(rclone version 2>/dev/null | head -1 | sed -E 's/^rclone v?//')"
  rc_major="${rc_ver%%.*}"; rc_rest="${rc_ver#*.}"; rc_minor="${rc_rest%%.*}"
  if (( rc_major > 1 || (rc_major == 1 && rc_minor >= 66) )); then
    ok "rclone $rc_ver"
  else
    warn "rclone $rc_ver is too old for a non-experimental bisync;"
    warn "  the app will download a current static build into"
    warn "  ~/.local/share/gdrive-client/bin on first run (checksum-verified)."
  fi
else
  warn "rclone not found; the app will download a current static build on first run."
  warn "  You still need to authorise a remote afterwards: rclone config"
fi

if ((${#missing_apt[@]})); then
  say ""
  warn "Install the missing pieces with:"
  say "    sudo apt install ${missing_apt[*]}"
  say ""
  read -r -p "Continue installing anyway? [Y/n] " reply
  [[ "${reply:-Y}" =~ ^[Nn] ]] && exit 1
fi

# -------------------------------------------------------------------- install

say ""
say "${BLD}Installing${RST}"

mkdir -p "$BIN_DIR" "$LIB_DIR" "$APP_DIR"

# Copy the package and its data next to it, so data/icons resolves the same way
# it does from a checkout.
rm -rf "$LIB_DIR/gdrive_client" "$LIB_DIR/data"
cp -r "$SRC/gdrive_client" "$LIB_DIR/gdrive_client"
cp -r "$SRC/data" "$LIB_DIR/data"
find "$LIB_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
ok "package -> $LIB_DIR"

cat > "$BIN_DIR/gdrive-client" <<EOF
#!/usr/bin/env python3
import sys
sys.path.insert(0, "$LIB_DIR")
from gdrive_client.app import main
if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 755 "$BIN_DIR/gdrive-client"
ok "launcher -> $BIN_DIR/gdrive-client"

for size_dir in "$SRC"/data/icons/hicolor/*/apps; do
  [[ -d "$size_dir" ]] || continue
  size="$(basename "$(dirname "$size_dir")")"
  mkdir -p "$ICON_DIR/$size/apps"
  cp "$size_dir"/*.svg "$size_dir"/*.png "$ICON_DIR/$size/apps/" 2>/dev/null || true
done
ok "icons -> $ICON_DIR"

sed "s|^Exec=.*|Exec=$BIN_DIR/gdrive-client|" "$SRC/data/gdrive-client.desktop" \
  > "$APP_DIR/gdrive-client.desktop"
ok "menu entry -> $APP_DIR/gdrive-client.desktop"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

# ----------------------------------------------------------------- next steps

say ""
if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
  warn "$BIN_DIR is not on your PATH. Add this to ~/.profile:"
  say  "    export PATH=\"\$PATH:$BIN_DIR\""
fi

say "${BLD}Done.${RST} Next:"
say "  1. If you have no rclone remote yet:   rclone config"
say "  2. Check the setup:                    gdrive-client doctor"
say "  3. See what a first sync would do:     gdrive-client sync --dry-run"
say "  4. Start the tray:                     gdrive-client"
say ""
say "Settings live in ~/.gdrive-settings (also editable from the tray)."
