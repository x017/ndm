#!/usr/bin/env bash
# Install a private Python environment for all users. Never run the app as root.
set -euo pipefail
if [[ $(uname -s) != Linux ]]; then
    echo 'This installer supports Linux. Use pipx/uv tool install on other platforms.' >&2
    exit 1
fi
if [[ $EUID != 0 ]]; then
    echo 'Run: sudo bash scripts/install-system.sh [--uninstall]' >&2
    exit 1
fi
if [[ ${1:-} == --uninstall ]]; then
    rm -f /usr/local/bin/ndm /usr/local/share/applications/io.github.x017.ndm.desktop \
        /usr/local/share/icons/hicolor/scalable/apps/ndm.svg
    rm -rf /opt/ndm
    echo 'NDM removed. Per-user state, autostart entries and downloaded files are untouched.'
    exit 0
fi
if [[ $# != 0 ]]; then echo "Unknown argument: $1" >&2; exit 1; fi
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -e /opt/ndm || -e /usr/local/bin/ndm ]]; then
    echo 'Existing installation detected. Uninstall it first; user data is preserved.' >&2
    exit 1
fi
# Python's venv support may require the distro's python3-venv package.
# Access distro-provided gi/AppIndicator bindings; pip cannot supply their native libraries.
python3 -m venv --system-site-packages /opt/ndm
/opt/ndm/bin/python -m pip install --upgrade pip
/opt/ndm/bin/python -m pip install "$ROOT[tray]"
install -d /usr/local/bin /usr/local/share/applications /usr/local/share/icons/hicolor/scalable/apps
cat > /usr/local/bin/ndm <<'WRAPPER'
#!/bin/sh
exec /opt/ndm/bin/ndm "$@"
WRAPPER
chmod 755 /usr/local/bin/ndm
install -m 644 "$ROOT/packaging/linux/io.github.x017.ndm.desktop" /usr/local/share/applications/
install -m 644 "$ROOT/packaging/linux/ndm.svg" /usr/local/share/icons/hicolor/scalable/apps/
command -v update-desktop-database >/dev/null && update-desktop-database /usr/local/share/applications || true
echo 'Installed. Launch NDM from your application menu, or run ndm tray as your normal user.'
echo 'For CLI-only operation: ndm daemon. For the browser frontend: ndm web.'
