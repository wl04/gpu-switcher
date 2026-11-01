#!/usr/bin/env bash
# Release gpu-switcher: bump the version in debian/control, prepend the
# changelog entry, run the checks, build + lintian the .deb, commit, push,
# and create the GitHub release (tag v<version>, .deb as the asset).
#
# Usage: tools/release.sh <new-version> <changelog-bullets-file> [--dry-run]
#
#   <changelog-bullets-file>
#       A file holding the entry's bullet lines exactly as they should appear
#       in debian/changelog — every line starting with two spaces and '*',
#       e.g.:
#         * First bullet.
#         * Second bullet.
#   --dry-run
#       Do everything except git commit/push and the gh release step.
#
# The working tree must be clean before running (commit your changes first).
set -euo pipefail
umask 022
cd "$(dirname "$0")/.."

usage() {
  echo "Usage: $(basename "$0") <new-version> <changelog-bullets-file> [--dry-run]" >&2
  exit 2
}
[ $# -ge 2 ] || usage

NEW_VERSION="$1"
BULLETS_FILE="$2"
DRY=0
[ "${3:-}" = "--dry-run" ] && DRY=1
[ $# -le 3 ] || usage

# --- validate inputs --------------------------------------------------------
[[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?$ ]] || {
  echo "error: bad version format: $NEW_VERSION (want e.g. 0.1.0-24)" >&2
  exit 1
}
[ -s "$BULLETS_FILE" ] || {
  echo "error: changelog bullets file not found or empty: $BULLETS_FILE" >&2
  exit 1
}
if grep -qv '^  \* ' "$BULLETS_FILE"; then
  echo "error: every line of $BULLETS_FILE must be a changelog bullet" >&2
  echo "       starting with two spaces and '*'" >&2
  exit 1
fi

PKG="gpu-switcher"
CONTROL=debian/control
CHANGELOG=debian/changelog
OLD_VERSION="$(awk '/^Version: /{print $2; exit}' "$CONTROL")"
MAINTAINER="$(sed -n 's/^Maintainer: //p' "$CONTROL" | head -n 1)"
[ -n "$OLD_VERSION" ] && [ -n "$MAINTAINER" ] || {
  echo "error: could not read Version:/Maintainer: from $CONTROL" >&2
  exit 1
}
[ "$OLD_VERSION" != "$NEW_VERSION" ] || {
  echo "error: $NEW_VERSION is already the current version" >&2
  exit 1
}

if [ "$DRY" -eq 0 ]; then
  dirty="$(git status --porcelain)"
  [ -z "$dirty" ] || {
    echo "error: working tree is not clean; commit or stash first:" >&2
    echo "$dirty" >&2
    exit 1
  }
fi

# --- bump version, prepend the changelog entry ------------------------------
sed -i "s/^Version: .*/Version: $NEW_VERSION/" "$CONTROL"
DATE="$(LC_ALL=C date '+%a, %d %b %Y %H:%M:%S %z')"
{
  echo "$PKG ($NEW_VERSION) unstable; urgency=low"
  echo
  cat "$BULLETS_FILE"
  echo
  echo " -- $MAINTAINER  $DATE"
  echo
  cat "$CHANGELOG"
} > "$CHANGELOG.new"
mv "$CHANGELOG.new" "$CHANGELOG"

# --- checks, build, verify ----------------------------------------------------
make test
make package
DEB="../${PKG}_${NEW_VERSION}_all.deb"
[ -f "$DEB" ] || { echo "error: build did not produce $DEB" >&2; exit 1; }
lintian "$DEB"
SHA256="$(sha256sum "$DEB" | awk '{print $1}')"
echo "built: $DEB"
echo "sha256: $SHA256"

# --- release notes ------------------------------------------------------------
NOTES="$(mktemp)"
if [ "$DRY" -eq 0 ]; then
  trap 'rm -f "$NOTES"' EXIT
fi
{
  echo "## What's new (since $OLD_VERSION)"
  echo
  cat "$BULLETS_FILE"
  echo
  echo "## Install"
  echo
  echo '```sh'
  echo "sudo dpkg -i ${PKG}_${NEW_VERSION}_all.deb"
  echo '```'
  echo
  echo "Upgrading from an older version: stop a still-running helper first —"
  echo '`sudo pkill -f gpu_switcher_helper`.'
  echo
  echo "Requires: python3 (>= 3.12), python3-gi, libgtk-4-1, polkitd, dbus,"
  echo "udev, systemd, pciutils."
  echo
  echo "## Checksum"
  echo
  echo '```'
  echo "sha256  $SHA256"
  echo "file    ${PKG}_${NEW_VERSION}_all.deb"
  echo '```'
} > "$NOTES"

if [ "$DRY" -eq 1 ]; then
  echo "dry-run: skipped git commit/push and gh release."
  echo "notes file: $NOTES"
  exit 0
fi

# --- publish --------------------------------------------------------------------
git commit -am "Release: $NEW_VERSION"
git push
gh release create "v$NEW_VERSION" "$DEB" \
  --title "$NEW_VERSION" \
  --notes-file "$NOTES"
echo "released v$NEW_VERSION"
