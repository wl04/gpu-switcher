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
  "$ROOT/usr/lib/python3/dist-packages/gpu_switcher" \
  "$ROOT/usr/lib/systemd/system" \
  "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$ROOT/usr/share/icons/hicolor/symbolic/apps" \
  "$ROOT/usr/share/polkit-1/actions" \
  "$ROOT/usr/share/dbus-1/system-services" \
  "$ROOT/usr/share/dbus-1/system.d" \
  "$ROOT/usr/share/doc/gpu-switcher" \
  "$ROOT/usr/share/man/man1"

# Validation gate for the polkit policy: well-formed XML, and per the polkit
# DTD exactly one <defaults> per action holding (allow_any, allow_active) or
# (allow_any, allow_active, allow_inactive) in that order. xmllint and lxml may
# be missing, so this uses the stdlib ElementTree plus an explicit check.
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

# The single package file list (binary, python package, data, locale,
# polkit, D-Bus, systemd unit, copyright) lives in the Makefile `install`
# target; calling it keeps this plain build in sync with the dh path.
make install DESTDIR="$ROOT"
# The man page header (@VERSION@ / @DATE@) is rendered from debian/changelog by
# the Makefile; the dh path installs the same file via
# debian/gpu-switcher.manpages.
make man
# Debian docs. Policy requires the installed changelog and man page to be
# gzip-compressed; the copyright file stays plain text.
# -n: no embedded name/timestamp, for reproducible builds.
gzip -9n -c debian/changelog > "$ROOT/usr/share/doc/gpu-switcher/changelog.Debian.gz"
gzip -9n -c build/man/gpu-switcher.1 > "$ROOT/usr/share/man/man1/gpu-switcher.1.gz"
chmod 0644 "$ROOT/usr/share/doc/gpu-switcher/"* "$ROOT/usr/share/man/man1/"*
# The maintainer scripts and the lintian override: the same files the dh path
# installs via dh_installdeb, so both build paths ship identical lifecycle
# handling.
install -D -m 0755 debian/postinst "$ROOT/DEBIAN/postinst"
install -D -m 0755 debian/postrm "$ROOT/DEBIAN/postrm"
install -D -m 0644 debian/gpu-switcher.lintian-overrides \
  "$ROOT/usr/share/lintian/overrides/gpu-switcher"

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

# md5sums
( cd "$ROOT" && find usr -type f | sort | xargs -r md5sum > DEBIAN/md5sums )

dpkg-deb --build --root-owner-group -z9 "$ROOT" "$OUT"
echo "Built: $OUT"
