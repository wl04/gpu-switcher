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
# The version's single source of truth is the top changelog entry
# (debian/control carries no Version: field, per Debian policy).
OLD_VERSION="$(awk '/^'"$PKG"' \(/{print $2; exit}' "$CHANGELOG" | tr -d '()')"
MAINTAINER="$(sed -n 's/^Maintainer: //p' "$CONTROL" | head -n 1)"
[ -n "$OLD_VERSION" ] && [ -n "$MAINTAINER" ] || {
  echo "error: could not read the top changelog entry / Maintainer:" >&2
  echo "       from $CHANGELOG and $CONTROL" >&2
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

# --- prepend the changelog entry (establishes the new version) --------------
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
# Catalog consistency gate (reference compiler), one invocation per
# catalog. -o /dev/null: GNU msgfmt -c still writes the default
# messages.mo to the CWD without an explicit output file.
msgfmt -c -o /dev/null po/gpu-switcher-en.po
msgfmt -c -o /dev/null po/gpu-switcher-ru.po
make package
DEB="../${PKG}_${NEW_VERSION}_all.deb"
[ -f "$DEB" ] || { echo "error: build did not produce $DEB" >&2; exit 1; }

# Cross-check: the debhelper path must produce the same package contents.
# The orig tarball goes in the parent of the source directory (where
# dpkg-buildpackage looks for it) and is archived from HEAD, so the
# working tree must be clean. dpkg-buildpackage writes the .deb to the
# same default path the canonical build used, so stage the canonical
# build aside, compare contents (mtime columns dropped: the two builds
# run seconds apart), and restore it.
UPSTREAM="${NEW_VERSION%-*}"
git archive --prefix="${PKG}_${UPSTREAM}/" HEAD | xz -c \
  > "../${PKG}_${UPSTREAM}.orig.tar.xz"
mv "$DEB" "${DEB}.staged"
dpkg-buildpackage -us -uc -b
diff <(dpkg-deb --contents "$DEB" | awk '{print $1, $2, $3, $6}' | sort) \
     <(dpkg-deb --contents "${DEB}.staged" | awk '{print $1, $2, $3, $6}' | sort) \
  || {
    echo "error: dpkg-buildpackage package differs from the canonical build;" >&2
    echo "       inspect ${DEB} and ${DEB}.staged" >&2
    exit 1
  }
mv "${DEB}.staged" "$DEB"
rm -f "../${PKG}_${UPSTREAM}.orig.tar.xz" \
      "../${PKG}_${NEW_VERSION}_amd64.buildinfo"

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
  echo "Requires: python3 (>= 3.12), python3-gi, gir1.2-gtk-4.0 (>= 4.10),"
  echo "libgtk-4-1 (>= 4.10), polkitd, dbus, udev, systemd, pciutils."
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
