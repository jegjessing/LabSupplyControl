#!/usr/bin/env bash
#
# Install LabSupplyControl desktop integration (KDE, GNOME, any freedesktop DE).
# User-level — no root required. Safe to re-run to update paths/icon.
#
#   ./install.sh        (or: bash install.sh)
#
# This adds an application-menu entry and icon that launch the app from this
# repo. It does NOT install the Python dependencies — see the README for those.

set -euo pipefail

APP_ID="labsupplycontrol"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"

mkdir -p "$APPS_DIR" "$ICON_DIR"

# Icon, installed under a theme name so Icon=labsupplycontrol resolves.
cp -f "$REPO_DIR/assets/icon.png" "$ICON_DIR/$APP_ID.png"

# Desktop entry with absolute paths baked in.
cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=LabSupplyControl
GenericName=Power Supply Control
Comment=Remote control for Nice-Power / KUAIQU bench DC power supplies
Exec=python3 "$REPO_DIR/app.py"
Path=$REPO_DIR
Icon=$APP_ID
Terminal=false
Categories=Utility;Electronics;
Keywords=power supply;PSU;bench;lab;charger;voltage;current;
StartupWMClass=$APP_ID
EOF
chmod +x "$APPS_DIR/$APP_ID.desktop"

# Refresh menu / icon caches (ignored if the tools aren't installed).
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed LabSupplyControl for '$USER':"
echo "  launcher : $APPS_DIR/$APP_ID.desktop"
echo "  icon     : $ICON_DIR/$APP_ID.png"
echo
echo "It should show up in your application menu shortly (log out/in if not)."
echo "Remove it again with: ./uninstall.sh"
