#!/usr/bin/env bash
# lanlog installer -- one project, one installer for the logger, the CLI, and
# the tray icon.
#
#   ./install.sh              install the launchers, icon and autostart
#   ./install.sh --gui-deps   offer to install GTK/PyGObject for the tray first
#
# What it writes:
#   ~/.local/bin/lanlog          CLI / dashboard launcher (project venv python)
#   ~/.local/bin/lanlog-tray     tray launcher (system python -- PyGObject is a
#                                system package and is not importable from a venv)
#   ~/.local/share/icons/hicolor/scalable/apps/lanlog-tray.svg
#   ~/.config/autostart/lanlog-tray.desktop
#
# Nothing here needs root. The only privileged step is --gui-deps, which is
# offered and printed rather than run silently.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINDIR="${HOME}/.local/bin"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

say()  { printf '\n%s\n' "$1"; }
ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  warn  %s\n' "$1"; }
die()  { printf '  fail  %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- sanity
[ -f "${HERE}/bin/lanlog" ] || die "bin/lanlog not found -- run this from the project directory."
[ -f "${HERE}/bin/lanlog-tray" ] || die "bin/lanlog-tray not found."

# The logger and CLI are stdlib-only; a venv keeps them off the system python
# without installing anything. The tray needs the SYSTEM python, so the two
# launchers deliberately use different interpreters.
if [ ! -x "${HERE}/.venv/bin/python" ]; then
    say "Creating the virtualenv for the logger/CLI"
    python3 -m venv "${HERE}/.venv" || die "could not create a venv"
fi
ok "venv -> ${HERE}/.venv"

# ---------------------------------------------------------------- gui deps
if [ "${1:-}" = "--gui-deps" ]; then
    say "Checking the tray's GUI dependencies"
    if ! /usr/bin/python3 -c "import gi" >/dev/null 2>&1; then
        warn "PyGObject (python3-gobject) is missing."
        echo "         Fedora:  sudo dnf install python3-gobject gtk3 libappindicator-gtk3"
        echo "         Debian:  sudo apt install python3-gobject libgtk-3-0 libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1"
        echo "         Arch:    sudo pacman -S --needed python-gobject gtk3 libappindicator"
        if [ -t 0 ]; then
            printf '         Run the install command for your distro now? [y/N] '
            read -r r || r=n
            case "$r" in
                [yY]*) case "$( (. /etc/os-release; echo "$ID $ID_LIKE") )" in
                           *fedora*) sudo dnf install -y python3-gobject gtk3 libappindicator-gtk3 ;;
                           *debian*|*ubuntu*) sudo apt install -y python3-gobject libgtk-3-0 libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1 ;;
                           *arch*) sudo pacman -S --needed --noconfirm python-gobject gtk3 libappindicator ;;
                           *) warn "unknown distro -- install python3-gobject, gtk3 and an appindicator library"; ;;
                       esac ;;
                *) warn "skipped -- the CLI and logger work regardless." ;;
            esac
        fi
    else
        ok "PyGObject present"
    fi
fi

# ---------------------------------------------------------------- launchers
say "Installing the launchers"
mkdir -p "${BINDIR}"

cat > "${BINDIR}/lanlog" <<LAUNCHER
#!/usr/bin/env bash
# lanlog -- CLI/dashboard, run with the project venv's interpreter.
exec "${HERE}/.venv/bin/python" "${HERE}/bin/lanlog" "\$@"
LAUNCHER
chmod +x "${BINDIR}/lanlog"
ok "lanlog -> ${BINDIR}/lanlog"

cat > "${BINDIR}/lanlog-tray" <<LAUNCHER
#!/bin/sh
# lanlog tray icon. System python, NOT the venv: PyGObject is a system package
# and is not importable from inside a venv.
: "\${LANLOG_DB:=${HERE}/lanlog.db}"
export LANLOG_DB
exec /usr/bin/python3 "${HERE}/bin/lanlog-tray" "\$@"
LAUNCHER
chmod +x "${BINDIR}/lanlog-tray"
ok "lanlog-tray -> ${BINDIR}/lanlog-tray"

# Prove the CLI imports and can read the database, without opening anything.
if "${HERE}/.venv/bin/python" -c "
import sys; sys.path.insert(0, '${HERE}')
from lanlog import db, report
db.connect()
" 2>/dev/null; then
    ok "lanlog package imports and the database opens"
else
    warn "the lanlog package did not import -- run 'lanlog devices' to see why"
fi

# ---------------------------------------------------------------- icon
say "Installing the icon"
mkdir -p "${ICONS}"
cat > "${ICONS}/lanlog-tray.svg" <<'SVG'
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <g fill="none" stroke="#2e3436" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round">
    <rect x="1.5" y="3" width="13" height="8" rx="1"/>
    <path d="M5 13.5h6M8 11v2.5"/>
  </g>
  <circle cx="8" cy="7" r="1.6" fill="#3584e4"/>
</svg>
SVG
ok "icon -> ${ICONS}/lanlog-tray.svg"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$(dirname "$(dirname "$(dirname "${ICONS}")")")" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- autostart
say "Setting up autostart"
mkdir -p "${AUTOSTART_DIR}"
cat > "${AUTOSTART_DIR}/lanlog-tray.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=lanlog tray
Comment=System tray icon for lanlog
Exec=env LANLOG_DB=${HERE}/lanlog.db ${BINDIR}/lanlog-tray
Icon=lanlog-tray
Terminal=false
X-GNOME-Autostart-enabled=true
NoDisplay=false
DESKTOP
ok "autostart entry -> ${AUTOSTART_DIR}/lanlog-tray.desktop"

# ---------------------------------------------------------------- done
say "Done"
cat <<EOF

    Logger service (records in the background, survives logout):

        lanlog start
        systemctl --user status lan-logger

    Live dashboard / CLI:

        lanlog
        lanlog devices
        lanlog domains

    Tray icon:

        lanlog-tray            (or just log in again -- autostart is set)

    A tray host must be running for the icon to appear. Check with:
        ${HERE}/bin/lanlog-tray-check

    To uninstall:
        rm -f ${BINDIR}/lanlog ${BINDIR}/lanlog-tray
        rm -f ${AUTOSTART_DIR}/lanlog-tray.desktop ${ICONS}/lanlog-tray.svg
        systemctl --user disable --now lan-logger
EOF
