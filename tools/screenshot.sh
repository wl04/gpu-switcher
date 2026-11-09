#!/usr/bin/env bash
# Render the app window from the source tree in a chosen UI language to
# docs/screenshot.png (or docs/screenshot-<lang>.png), independent of the
# system language. Needs a graphical session; no screenshot tool required.
#
# Usage: tools/screenshot.sh [language]     (default: en)
#
# Run `make locale` first so the source-tree catalogs exist.
set -euo pipefail

lang="${1:-en}"
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/docs/screenshot.png"
[[ "$lang" != "en" ]] && out="$root/docs/screenshot-$lang.png"

# LANGUAGE has top priority for gettext and GTK; LC_ALL must name an existing
# locale or GTK keeps the system language for its own strings.
if [[ "$lang" == "en" ]]; then locale="C.UTF-8"; else locale="${lang}_${lang^^}.UTF-8"; fi
export LANGUAGE="$lang" LC_ALL="$locale" LANG="$locale"

exec python3 "$root/tools/screenshot.py" "$out"
