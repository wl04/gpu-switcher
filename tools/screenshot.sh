#!/usr/bin/env bash
# Run the app from the source tree in a chosen UI language and capture
# docs/screenshot.png, independent of the system language.
#
# Usage: tools/screenshot.sh [language]     (default: en)
#
# With gnome-screenshot installed the focused app window is captured after a
# short delay; otherwise the app just starts so you can capture it yourself.
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
export PYTHONPATH="$root/src"

python3 -m gpu_switcher.main &
app_pid=$!
trap 'kill "$app_pid" 2>/dev/null || true' EXIT

if command -v gnome-screenshot >/dev/null; then
    sleep 4  # let detection finish and the window take focus
    gnome-screenshot --window --file="$out"
    echo "Saved $out"
else
    echo "gnome-screenshot not found; capture the window manually, then close it."
    wait "$app_pid"
fi
