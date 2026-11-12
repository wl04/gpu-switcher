#!/usr/bin/env bash
# Render the man page header (@VERSION@ / @DATE@) from debian/changelog.
# Usage: tools/render-man.sh <output> <source-man-page> <changelog>
#
# The top changelog entry is the single source of truth for the version (dpkg,
# tools/build-deb.sh and this header all read it there); the date is that
# entry's release date. LC_ALL=C in front of `date` keeps the month name English
# whatever locale builds the package.
set -euo pipefail
umask 022

[ $# -eq 3 ] || {
  echo "usage: $(basename "$0") <output> <source-man-page> <changelog>" >&2
  exit 2
}
OUT="$1"
SRC="$2"
CHANGELOG="$3"

VERSION="$(awk '/^gpu-switcher \(/{print $2; exit}' "$CHANGELOG" | tr -d '()')"
[ -n "$VERSION" ] || {
  echo "error: no version in the top entry of $CHANGELOG" >&2
  exit 1
}

# The trailer line is " -- <maintainer>  <RFC 2822 date>", the date separated
# from the maintainer by the two spaces Debian writes.
DATE_LINE="$(sed -n 's/^ -- .*  \(.*\)$/\1/p' "$CHANGELOG" | head -n 1)"
[ -n "$DATE_LINE" ] || {
  echo "error: no release date line in $CHANGELOG" >&2
  exit 1
}
MAN_DATE="$(LC_ALL=C date -d "$DATE_LINE" '+%-d %B %Y')" || {
  echo "error: cannot read the changelog date: $DATE_LINE" >&2
  exit 1
}

mkdir -p "$(dirname "$OUT")"
# The source page must stay templated: a hand-written version or date here is
# exactly the bug this script exists to prevent, so its absence is an error
# rather than something to render around.
for token in @VERSION@ @DATE@; do
  grep -q "$token" "$SRC" || {
    echo "error: $SRC does not contain $token" >&2
    exit 1
  }
done
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$MAN_DATE/" "$SRC" > "$OUT"

# A misspelled token (or one the source lost) would otherwise ship as literal
# text inside the installed manual.
if grep -q '@[A-Z][A-Z_]*@' "$OUT"; then
  echo "error: $OUT still contains an unrendered @…@ token" >&2
  exit 1
fi

echo "rendered $OUT: gpu-switcher $VERSION, $MAN_DATE"
