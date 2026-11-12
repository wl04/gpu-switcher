#!/usr/bin/env bash
# Release gpu-switcher: prepend the debian/changelog entry (the version's
# source of truth), sync pyproject.toml, run the checks, build + lintian the
# .deb, commit, push, and create the GitHub release (tag v<version>, .deb as
# the asset).
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
#       Do everything except git commit/push and the gh release step;
#       debian/changelog and pyproject.toml are restored afterwards (the built
#       .deb is kept in the parent directory).
#
# Environment:
#   URGENCY   changelog urgency (default: low)
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
[ $# -le 3 ] || usage
if [ $# -eq 3 ]; then
  [ "$3" = "--dry-run" ] || usage
  DRY=1
fi

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
if [ "$DRY" -eq 1 ]; then
  BACKUP="$(mktemp -d)"
  cp "$CHANGELOG" "$BACKUP/changelog"
  cp pyproject.toml "$BACKUP/pyproject.toml"
  trap 'cp "$BACKUP/changelog" "$CHANGELOG"; cp "$BACKUP/pyproject.toml" pyproject.toml; rm -rf "$BACKUP"' EXIT
fi
DATE="$(LC_ALL=C date '+%a, %d %b %Y %H:%M:%S %z')"
{
  echo "$PKG ($NEW_VERSION) unstable; urgency=${URGENCY:-low}"
  echo
  cat "$BULLETS_FILE"
  echo
  echo " -- $MAINTAINER  $DATE"
  echo
  cat "$CHANGELOG"
} > "$CHANGELOG.new"
mv "$CHANGELOG.new" "$CHANGELOG"

# Keep pyproject.toml in sync with the changelog (the single source of
# truth). pip metadata carries the upstream version (no Debian -N revision).
UPSTREAM="${NEW_VERSION%-*}"
sed -i "s/^version = \".*\"/version = \"$UPSTREAM\"/" pyproject.toml

# --- checks, build, verify ----------------------------------------------------
# The same gates CI runs, so a release is never cut on a state CI would reject.
make test
make lint
make lint-shell
make typecheck
make coverage
# Catalog consistency gate (reference compiler), one invocation per catalog in
# po/ - the same source of the language set as the Makefile and the tests.
# -o /dev/null: GNU msgfmt -c still writes the default
# messages.mo to the CWD without an explicit output file.
for catalog in po/gpu-switcher-*.po; do
  msgfmt -c -o /dev/null "$catalog"
done
make package
DEB="../${PKG}_${NEW_VERSION}_all.deb"
[ -f "$DEB" ] || { echo "error: build did not produce $DEB" >&2; exit 1; }

# Both build paths (dh via debian/rules, plain via tools/build-deb.sh) install
# the same files through `make install` and the same maintainer scripts from
# debian/, so their package contents are identical by construction; no
# cross-check is needed.
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
  echo "sudo apt install ./${PKG}_${NEW_VERSION}_all.deb"
  echo '```'
  echo
  echo "apt resolves any missing dependencies from the repository; offline, with"
  echo "all dependencies present, \`sudo dpkg -i\` also works."
  echo
  echo "The package reloads the systemd unit and restarts a helper that is still"
  echo "running after an upgrade, so no manual restart is needed."
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
