"""Shared udev rule constants and conflict scanning.

Used by both the unprivileged app (gpu_switcher/detect.py) and the root
helper (helper/gpu_switcher_helper.py): the same managed rule file, the
same mutter tag, and the same conflict rule — any *other* udev rule that
sets the preferred-GPU tag blocks the switch.

Pure stdlib, no `gi`: the root helper can import this without pulling in
the GUI package (`gpu_switcher/__init__` imports Gtk).
"""
from __future__ import annotations

import os

# The udev rule file managed by gpu-switcher (the same file the reference
# script mutter-primary-gpu.sh writes).
MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"

# Mutter reads this tag from the udev database at session start; the ENV
# variable is retained for compatibility with the previous local rule.
MUTTER_TAG = "mutter-device-preferred-primary"
MUTTER_ENV = "MUTTER_DEVICE_PREFERRED_PRIMARY"

# udev rule directories in load order; a base name that appears in an
# earlier directory shadows the later one (the later copy is ignored),
# mirroring udev's rule loading.
RULE_DIRS = (
    "/etc/udev/rules.d",
    "/run/udev/rules.d",
    "/usr/local/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
    "/lib/udev/rules.d",
)


def read_text(path: str) -> str:
    """File content stripped, or '' when the file cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def rule_conflicts(rule_dirs: tuple[str, ...] = RULE_DIRS,
                   managed_rule: str = MANAGED_RULE,
                   *, on_unreadable: str) -> list[str]:
    """Paths of the other udev rules that set the preferred-GPU tag.

    Scans `*.rules` files in `rule_dirs` in the given order, skipping
    comment lines; a rule conflicts when any line contains the mutter tag
    or the legacy ENV variable. The managed rule file itself is never a
    conflict. A base name seen in an earlier directory shadows the later
    one (the later copy is ignored).

    `on_unreadable`: "skip" ignores files that cannot be read (app side:
    the authoritative check runs in the root helper); "flag" reports them
    as conflicts (helper side: a rule root cannot read must block).
    """
    conflicts = []
    seen = set()
    for d in rule_dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".rules") or entry in seen:
                continue
            seen.add(entry)
            path = os.path.join(d, entry)
            if path == managed_rule:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                if on_unreadable == "flag":
                    conflicts.append(path)
                continue
            lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
            if any(MUTTER_TAG in l or MUTTER_ENV in l for l in lines):
                conflicts.append(path)
    return conflicts
