"""Shared udev rule constants, the display-GPU contract, and conflict scanning.

Shared by the unprivileged app and the root helper, so both agree on the
managed rule file, the mutter tag and what counts as a conflicting rule.

Pure stdlib, no `gi`: the helper can import it without the GUI stack.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# D-Bus error names: first-class, stable contract shared by the helper
# (which replies with them) and the app (which displays the message). The
# name is a valid D-Bus error name derived from the interface namespace.
# ---------------------------------------------------------------------------
DBUS_ERROR_NAMESPACE = "com.github.wl04.gpu_switcher.Error"
DBUS_ERROR_INTERNAL = f"{DBUS_ERROR_NAMESPACE}.Internal"
DBUS_ERROR_BUSY = f"{DBUS_ERROR_NAMESPACE}.Busy"

# The udev rule file managed by gpu-switcher (also written by the
# mutter-primary-gpu.sh script, so the two interoperate).
MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"

# D-Bus timeout budget, shared by the helper and the app: the client's call
# timeout must exceed the helper's worst case (interactive polkit dialog plus
# every udevadm step), or the helper's own error is lost to a client timeout.
#
# The interactive polkit dialog dominates the worst case.
POLKIT_TIMEOUT_MS = 170_000
# Upper bound for one udevadm step (verify / reload-rules / trigger / tag check).
UDEVADM_STEP_TIMEOUT_S = 30
# udevadm steps on the SetPrimary path: verify the new rule, then the refresh
# chain (reload-rules, DRM trigger, tag check). tests/test_helper.py compares
# this count with the commands the helper really runs.
SET_PRIMARY_STEP_COUNT = 4

# The client's budget must exceed the helper's worst case plus slack.
HELPER_TIMEOUT_MS = (
    POLKIT_TIMEOUT_MS + SET_PRIMARY_STEP_COUNT * UDEVADM_STEP_TIMEOUT_S * 1000 + 30_000
)

# Mutter reads this tag from the udev database at session start; the ENV
# variable is kept for compatibility with rules that still use it.
MUTTER_TAG = "mutter-device-preferred-primary"
MUTTER_ENV = "MUTTER_DEVICE_PREFERRED_PRIMARY"

# A DRM card node name. Anything else is not allowed to build a /sys path.
CARD_NAME_RE = re.compile(r"card[0-9]+")

# PCI classes that may act as a display primary: VGA compatible
# (0x030000) and 3D controller (0x030200). The class alone is not
# sufficient: both resolvers also require a bound driver and a DRM card
# with a /dev/dri device, so render-only devices are still excluded.
DISPLAY_PCI_CLASSES = ("0x030000", "0x030200")

# udev rule directories in load order; a rule name in an earlier directory
# shadows the same name in a later one, as in udev itself.
RULE_DIRS = (
    "/etc/udev/rules.d",
    "/run/udev/rules.d",
    "/usr/local/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
    "/lib/udev/rules.d",
)


@dataclass(frozen=True)
class RuleScan:
    """What one scan of the udev rule directories found.

    conflicts: other *.rules that assign the preferred-GPU tag.
    unreadable: *.rules files — or whole rule directories — whose contents could
        not be read. They may set the tag and no scan can tell; only the helper
        decides what they mean, because the app is not the one writing the rule.
    """

    conflicts: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def is_display_gpu(pci: str) -> str | None:
    """The DRM card bound to this PCI, when it is a display GPU.

    The PCI must be a display class (DISPLAY_PCI_CLASSES) with a bound driver,
    and exactly one card[0-9]+ in /sys/class/drm with a /dev/dri node must
    resolve to it. No driver, no card, or two cards (ambiguous) yield None.
    """
    pdir = f"/sys/bus/pci/devices/{pci}"
    if not os.path.isdir(pdir):
        return None
    if read_text(f"{pdir}/class") not in DISPLAY_PCI_CLASSES:
        return None
    if not os.path.islink(f"{pdir}/driver"):
        return None
    real = os.path.realpath(pdir)
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return None
    card = None
    for entry in entries:
        if not CARD_NAME_RE.fullmatch(entry):
            continue
        if os.path.realpath(f"/sys/class/drm/{entry}/device") != real:
            continue
        if not os.path.exists(f"/dev/dri/{entry}"):
            continue
        # A second card bound to this PCI makes the binding ambiguous.
        if card is not None:
            return None
        card = entry
    return card


def read_text(path: str) -> str:
    """File content stripped, or '' when the file cannot be read.

    Invalid UTF-8 bytes are replaced (U+FFFD): /sys and /proc values may
    hold them, and the detector must not die on them.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


# Assignments of the preferred-GPU tag. Only "=" and "+=" followed by a
# quoted value assign; TAG-= (unset), TAG== / TAG!= (match expressions) never
# match because the operator must be directly followed by the opening quote.
# The lookbehind keeps names such as XTAG from matching.
_TAG_ASSIGN_RE = re.compile(r'(?<![\w])TAG\s*\+?=\s*"([^"]*)"')
_ENV_ASSIGN_RE = re.compile(
    r"(?<![\w])ENV\s*\{\s*" + re.escape(MUTTER_ENV) + r'\s*\}\s*\+?=\s*"'
)


def join_continuations(text: str) -> list[str]:
    """Logical rule lines: a trailing backslash joins the next line."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.endswith("\\"):
            pending += raw[:-1]
            continue
        lines.append(pending + raw)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def sets_preferred_gpu(line: str) -> bool:
    """True when this line assigns the preferred-GPU tag to the device.

    Only assignment forms count: TAG+=/TAG= with the tag as the assigned
    value, or ENV{legacy variable}=. Every assignment on the line is
    inspected, so a preceding match expression (TAG=="seat") does not hide
    it. Unassign (TAG-=) and match expressions (TAG==, TAG!=, ENV{...}==)
    are not assignments and must not block the switch.
    """
    if any(m.group(1) == MUTTER_TAG for m in _TAG_ASSIGN_RE.finditer(line)):
        return True
    return _ENV_ASSIGN_RE.search(line) is not None


def scan_rules(
    rule_dirs: tuple[str, ...] = RULE_DIRS,
    managed_rule: str = MANAGED_RULE,
) -> RuleScan:
    """Scan `*.rules` in `rule_dirs` for rules that set the preferred-GPU tag.

    Directories are scanned in udev's own order and comment lines are skipped.
    A file conflicts when a line assigns the mutter tag or the legacy ENV
    variable (see sets_preferred_gpu). The managed rule itself never conflicts,
    and a base name in an earlier directory shadows the same name in a later one.

    A directory that does not exist is not a finding. A directory that cannot be
    listed, and a `*.rules` file that cannot be read, are reported in
    `RuleScan.unreadable`: they might set the tag, and reporting them as
    conflicts would accuse a file the scan never read.
    """
    conflicts: list[str] = []
    unreadable: list[str] = []
    seen: set[str] = set()
    for d in rule_dirs:
        try:
            entries = sorted(os.listdir(d))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            # A directory root cannot list may still hold a conflicting rule.
            unreadable.append(d)
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
                unreadable.append(path)
                continue
            if any(
                sets_preferred_gpu(line)
                for line in join_continuations(text)
                if not line.strip().startswith("#")
            ):
                conflicts.append(path)
    return RuleScan(conflicts=conflicts, unreadable=unreadable)


TAG_QUERY_TIMEOUT_S = 10


def tag_applied(card: str, *, tag: str = MUTTER_TAG) -> bool | None:
    """Ask udev — without privileges — whether a card carries a rule tag now.

    `udevadm info --query=property` reports the TAGS list udev currently holds for
    a device. That is the only unprivileged way to tell whether a written rule
    actually took effect: the rule file on disk says what should happen, the tag
    says what udev is doing.

    Returns None when udev cannot be consulted at all (udevadm missing or
    refusing, a device udev does not know, no answer in time). Unknown is not the
    same as not applied: reporting an untagged card would tell the user their rule
    had no effect. A device udev answers with no TAGS line carries no tags, so it
    is honestly False.
    """
    if not CARD_NAME_RE.fullmatch(card):
        # The device path is built from this name, so nothing that is not a plain
        # card name is handed to udevadm at all.
        return None
    try:
        completed = subprocess.run(
            ["udevadm", "info", "--query=property", f"--path=/sys/class/drm/{card}"],
            capture_output=True,
            text=True,
            timeout=TAG_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name == "TAGS":
            # Tags arrive colon-delimited with a colon at both ends, so a whole tag
            # is always wrapped in colons and a longer tag that merely starts with
            # this one does not match.
            return f":{tag}:" in value
    return False
