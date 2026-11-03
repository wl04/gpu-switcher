#!/usr/bin/env bash
# Build the gpu-switcher .deb without debhelper (plain dpkg-deb).
# Usage: tools/build-deb.sh [output.deb]
set -euo pipefail
umask 022
cd "$(dirname "$0")/.."

PKG="gpu-switcher"
# Single source of truth for the version: the top entry of debian/changelog,
# the same source dpkg-buildpackage/dpkg-parsechangelog read it from.
VERSION="$(awk '/^'"$PKG"' \(/{print $2; exit}' debian/changelog | tr -d '()')"
[ -n "$VERSION" ] || { echo "top changelog entry not found in debian/changelog" >&2; exit 1; }
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
  "$ROOT/usr/share/dbus-1/system.d" \
  "$ROOT/usr/share/doc/gpu-switcher" \
  "$ROOT/usr/share/man/man1"

# Compile the gettext catalogs with msgfmt (also leaves them in the source
# tree for dev runs).
mkdir -p data/locale/en/LC_MESSAGES data/locale/ru/LC_MESSAGES
msgfmt -o data/locale/en/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-en.po
msgfmt -o data/locale/ru/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-ru.po

# Validation gate: the polkit policy must be well-formed XML and
# structurally valid against the polkit policyconfig DTD, which allows
# exactly one <defaults> per action containing (allow_any, allow_active)
# or (allow_any, allow_active, allow_inactive) in that order. xmllint and
# lxml are not available in the build environment, so the stdlib
# ElementTree parse plus this explicit structural check is the guard.
python3 - <<'PY'
import sys
import xml.etree.ElementTree as ET

path = "data/polkit/gpu-switcher.policy"
try:
    root = ET.parse(path)
except ET.ParseError as exc:
    sys.exit(f"error: {path} is not well-formed XML: {exc}")

allowed_sequences = (
    ("allow_any", "allow_active"),
    ("allow_any", "allow_active", "allow_inactive"),
)

errors = []
actions = root.findall("action")
if not actions:
    errors.append("no <action> elements found")
for action in actions:
    action_id = action.get("id", "?")
    defaults = action.findall("defaults")
    if len(defaults) != 1:
        errors.append(
            f"action {action_id}: exactly one <defaults> block is "
            f"required (found {len(defaults)})")
        continue
    children = tuple(child.tag for child in defaults[0])
    if children not in allowed_sequences:
        errors.append(
            f"action {action_id}: <defaults> children must be exactly "
            "(allow_any, allow_active) or (allow_any, allow_active, "
            f"allow_inactive) in that order (found {children})")

if errors:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    sys.exit(1)
print(f"polkit policy OK: {len(actions)} action(s)")
PY

install -D -m 0755 src/wrapper/gpu-switcher "$ROOT/usr/bin/gpu-switcher"
install -D -m 0644 src/gpu_switcher/__init__.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/__init__.py"
install -D -m 0644 src/gpu_switcher/i18n.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/i18n.py"
install -D -m 0644 src/gpu_switcher/main.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/main.py"
install -D -m 0644 src/gpu_switcher/detect.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/detect.py"
install -D -m 0644 src/gpu_switcher/selection.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/selection.py"
install -D -m 0644 src/gpu_switcher/bus.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/bus.py"
install -D -m 0644 src/gpu_switcher/state.py "$ROOT/usr/lib/gpu-switcher/gpu_switcher/state.py"
install -D -m 0644 src/udev_rules.py "$ROOT/usr/lib/gpu-switcher/udev_rules.py"
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
# Debian docs and man page. Policy requires the installed changelog and
# man page to be gzip-compressed; the copyright file stays plain text.
# -n: no embedded name/timestamp, for reproducible builds.
gzip -9n -c debian/changelog > "$ROOT/usr/share/doc/gpu-switcher/changelog.Debian.gz"
install -D -m 0644 debian/copyright "$ROOT/usr/share/doc/gpu-switcher/copyright"
gzip -9n -c man/gpu-switcher.1 > "$ROOT/usr/share/man/man1/gpu-switcher.1.gz"
chmod 0644 "$ROOT/usr/share/doc/gpu-switcher/"* "$ROOT/usr/share/man/man1/"*
# The postrm maintainer script.
install -D -m 0755 debian/postrm "$ROOT/DEBIAN/postrm"

# control: the binary stanza of debian/control, with Version and Maintainer
# injected (they live in the source stanza, where they belong).
MAINTAINER="$(sed -n 's/^Maintainer: //p' debian/control | head -n 1)"
mkdir -p "$ROOT/DEBIAN"
{
  echo "Package: $PKG"
  echo "Version: $VERSION"
  echo "Maintainer: $MAINTAINER"
  awk '/^Package: /{found = 1; next} found {print}' debian/control
} > "$ROOT/DEBIAN/control"

# Guard: every module under src/gpu_switcher must be installed above
# (hand-maintained list; the both-builds cross-check in release.sh
# converges the debhelper path).
for f in src/gpu_switcher/*.py; do
  [ -f "$ROOT/usr/lib/gpu-switcher/gpu_switcher/$(basename "$f")" ] || {
    echo "error: $f is missing from the package file list" >&2
    exit 1
  }
done

# md5sums
( cd "$ROOT" && find usr -type f | sort | xargs -r md5sum > DEBIAN/md5sums )

dpkg-deb --build --root-owner-group -z9 "$ROOT" "$OUT"
echo "Built: $OUT"
