#!/bin/sh
# Build dist/k-link_<version>_all.deb with dpkg-deb only (no debhelper needed).
set -e
cd "$(dirname "$0")/.."
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/klink/__init__.py)
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
PY=$ROOT/usr/lib/python3/dist-packages
mkdir -p "$PY" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/user" \
         "$ROOT/usr/share/doc/k-link" "$ROOT/DEBIAN"
cp -r src/klink "$PY/"
find "$PY" -name __pycache__ -prune -exec rm -rf {} +
cat > "$ROOT/usr/bin/k-link" <<'SH'
#!/usr/bin/python3
from klink.cli import main
main()
SH
chmod 755 "$ROOT/usr/bin/k-link"
cp packaging/k-link.service "$ROOT/usr/lib/systemd/user/"
cp README.md "$ROOT/usr/share/doc/k-link/"
cat > "$ROOT/DEBIAN/control" <<CTL
Package: k-link
Version: $VER
Architecture: all
Maintainer: C31AG <adan@tecnofutura.net>
Depends: python3 (>= 3.9), bluez, pipewire, pipewire-bin, wireplumber, libspa-0.2-bluetooth
Recommends: sox, pipewire-pulse
Section: hamradio
Priority: optional
Homepage: https://github.com/pyopower/k-link
Description: radio gateway through a Bluetooth K-plug PTT adapter
 Links a handheld radio to AllStarLink, DVSwitch and other USRP peers
 through the Bluetooth adapter of wireless speaker-mic kits (Abbree and
 similar) plugged into the radio's K connector: no cables or sound card.
 .
 After installing: k-link config && k-link pair && k-link test, then
 systemctl --user enable --now k-link
CTL
mkdir -p dist
dpkg-deb --root-owner-group --build "$ROOT" "dist/k-link_${VER}_all.deb"
