#!/usr/bin/env bash
# Build the gpu-switcher .deb without debhelper (plain dpkg-deb).
# Usage: tools/build-deb.sh [output.deb]
set -euo pipefail
cd "$(dirname "$0")/.."

PKG="gpu-switcher"
VERSION="0.1.0-11"
OUT="${1:-../${PKG}_${VERSION}_all.deb}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/${PKG}_${VERSION}"

mkdir -p \
  "$ROOT/usr/bin" \
  "$ROOT/usr/lib/gpu-switcher/gpu_switcher" \
  "$ROOT/usr/lib/gpu-switcher/helper" \
  "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$ROOT/usr/share/icons/hicolor/symbolic/apps" \
  "$ROOT/usr/share/gpu-switcher/locale/en/LC_MESSAGES" \
  "$ROOT/usr/share/gpu-switcher/locale/ru/LC_MESSAGES" \
  "$ROOT/usr/share/polkit-1/actions" \
  "$ROOT/usr/share/dbus-1/system-services" \
  "$ROOT/usr/share/dbus-1/system.d"

# Compile the gettext catalogs (also leaves them in the source tree for dev runs).
mkdir -p data/locale/en/LC_MESSAGES data/locale/ru/LC_MESSAGES
python3 tools/po2mo.py po/gpu-switcher-en.po -o data/locale/en/LC_MESSAGES/gpu-switcher.mo
python3 tools/po2mo.py po/gpu-switcher-ru.po -o data/locale/ru/LC_MESSAGES/gpu-switcher.mo

install -D -m 0755 src/wrapper/gpu-switcher "$ROOT/usr/bin/gpu-switcher"
install -D -m 0644 src/gpu_switcher/__init__.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/__init__.py"
install -D -m 0644 src/gpu_switcher/i18n.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/i18n.py"
install -D -m 0644 src/gpu_switcher/main.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/main.py"
install -D -m 0644 src/gpu_switcher/detect.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/detect.py"
install -D -m 0644 src/gpu_switcher/bus.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/bus.py"
install -D -m 0755 src/helper/gpu_switcher_helper.py "$ROOT/usr/lib/gpu-switcher/helper/gpu_switcher_helper.py"
install -D -m 0644 data/gpu-switcher.desktop "$ROOT/usr/share/applications/com.github.wl04.gpu-switcher.desktop"
install -D -m 0644 data/icons/gpu-switcher.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/com.github.wl04.gpu-switcher.svg"
install -D -m 0644 data/icons/symbolic/gpu-switcher-symbolic.svg "$ROOT/usr/share/icons/hicolor/symbolic/apps/com.github.wl04.gpu-switcher-symbolic.svg"
install -D -m 0644 data/locale/en/LC_MESSAGES/gpu-switcher.mo "$ROOT/usr/share/gpu-switcher/locale/en/LC_MESSAGES/gpu-switcher.mo"
install -D -m 0644 data/locale/ru/LC_MESSAGES/gpu-switcher.mo "$ROOT/usr/share/gpu-switcher/locale/ru/LC_MESSAGES/gpu-switcher.mo"
install -D -m 0644 data/polkit/gpu-switcher.policy "$ROOT/usr/share/polkit-1/actions/gpu-switcher.policy"
# D-Bus activation: service file (name must be <service name>.service)
# and the security policy that lets root own the helper's name on the
# locked-down system bus.
install -D -m 0644 data/dbus/com.github.wl04.gpu_switcher.Helper.service \
  "$ROOT/usr/share/dbus-1/system-services/com.github.wl04.gpu_switcher.Helper.service"
install -D -m 0644 data/dbus/system.d/com.github.wl04.gpu_switcher.Helper.conf \
  "$ROOT/usr/share/dbus-1/system.d/com.github.wl04.gpu_switcher.Helper.conf"

# control: the binary stanza of debian/control
mkdir "$ROOT/DEBIAN"
awk '/^Package: /{found = 1} found {print}' debian/control > "$ROOT/DEBIAN/control"

# md5sums
( cd "$ROOT" && find usr -type f | sort | xargs -r md5sum > DEBIAN/md5sums )

dpkg-deb --build --root-owner-group -z9 "$ROOT" "$OUT"
echo "Built: $OUT"
