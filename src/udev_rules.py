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
import re

# ---------------------------------------------------------------------------
# D-Bus error names: first-class, stable contract shared by the helper
# (which replies with them) and the app (which displays the message). The
# name is a valid D-Bus error name derived from the interface namespace.
# ---------------------------------------------------------------------------
DBUS_ERROR_NAMESPACE = "com.github.wl04.gpu_switcher.Error"
DBUS_ERROR_INTERNAL = f"{DBUS_ERROR_NAMESPACE}.Internal"
DBUS_ERROR_BUSY = f"{DBUS_ERROR_NAMESPACE}.Busy"

# The udev rule file managed by gpu-switcher (the same file the reference
# script mutter-primary-gpu.sh writes).
MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"

# ---------------------------------------------------------------------------
# D-Bus timeout budget: one source of truth shared by the helper (server
# side) and the app (client side), so the client's budget always exceeds
# the helper's worst case (interactive polkit dialog + every udevadm step)
# plus slack. Without that, the helper's own error is lost to a
# client-side timeout.
# ---------------------------------------------------------------------------
# The polkit interactive dialog (AllowUserInteraction) dominates the worst
# case: the user decides for up to this long.
POLKIT_TIMEOUT_MS = 170_000
# A single udevadm step (verify / reload-rules / trigger / tag check) fails
# fast in practice; this is the fail-safe bound for one step.
UDEVDADM_STEP_TIMEOUT_S = 30
# Number of udevadm steps on the SetPrimary path (verify, reload-rules,
# trigger, tag check); keep this count honest when the steps change.
SET_PRIMARY_STEP_COUNT = 4

# The client's budget must exceed the helper's worst case plus slack.
HELPER_TIMEOUT_MS = (
    POLKIT_TIMEOUT_MS + SET_PRIMARY_STEP_COUNT * UDEVDADM_STEP_TIMEOUT_S * 1000 + 30_000
)

# Mutter reads this tag from the udev database at session start; the ENV
# variable is retained for compatibility with the previous local rule.
MUTTER_TAG = "mutter-device-preferred-primary"
MUTTER_ENV = "MUTTER_DEVICE_PREFERRED_PRIMARY"

# PCI classes that may act as a display primary: VGA compatible
# (0x030000) and 3D controller (0x030200). The class alone is not
# sufficient: both resolvers also require a bound driver and a DRM card
# with a /dev/dri device, so render-only devices are still excluded.
DISPLAY_PCI_CLASSES = ("0x030000", "0x030200")

# The numeric GError code of G_IO_ERROR_TIMEOUT in glib/gio/giotypes.h
# (5 is G_IO_ERROR_IO, not a timeout). Both the app (outcome mapping) and
# the helper (polkit timeout handling) match g-io-error-quark with this
# code; pygobject does not expose the Gio.Error enum as a constant.
G_IO_ERROR_TIMEOUT = 7

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


def is_display_gpu(pci: str) -> str | None:
    """The DRM card bound to this PCI, when it is a display GPU.

    The shared display-GPU contract used by both the unprivileged app
    (detection) and the root helper (SetPrimary): the PCI must be a
    display class (DISPLAY_PCI_CLASSES), have a bound driver, and exactly
    one /dev/dri card (card[0-9]+) in /sys/class/drm must resolve to it
    with a /dev/dri/<card> node. No bound driver, no card, or two cards
    (ambiguous binding) all yield None.
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
        if not re.fullmatch(r"card[0-9]+", entry):
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


# Assignment operators for the preferred-GPU tag. TAG-= (unset) and
# TAG== (match expression) are not assignments and must not count.
_TAG_ASSIGN_RE = re.compile(r"TAG\s*(\+=|-=|==|=)")
_ENV_ASSIGN_RE = re.compile(r"ENV\s*\{[^}]*\}\s*(\+=|-=|==|=)")
_ASSIGN_OPS = ("+=", "=")


def _sets_preferred_gpu(line: str) -> bool:
    """True when this line assigns the preferred-GPU tag to the device.

    Only assignment forms count: TAG+=/TAG= with the tag as the assigned
    value, or ENV{...}= where the key is the legacy variable. Unassign
    (TAG-=) and match expressions (TAG==, ENV{...}==/!=) are not
    assignments and must not block the switch.
    """
    m = _TAG_ASSIGN_RE.search(line)
    if m is not None and m.group(1) in _ASSIGN_OPS and MUTTER_TAG in line:
        return True
    m = _ENV_ASSIGN_RE.search(line)
    return m is not None and m.group(1) in _ASSIGN_OPS and MUTTER_ENV in line


def rule_conflicts(
    rule_dirs: tuple[str, ...] = RULE_DIRS,
    managed_rule: str = MANAGED_RULE,
    *,
    on_unreadable: str,
) -> list[str]:
    """Paths of the other udev rules that set the preferred-GPU tag.

    Scans `*.rules` files in `rule_dirs` in the given order, skipping
    comment lines; a rule conflicts when any line assigns the mutter tag
    or the legacy ENV variable (assignment forms only: TAG+=/TAG=/ENV{}=;
    unassign TAG-= and match expressions TAG==/ENV{}!= do not count). The managed rule file itself is never a
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
            if any(_sets_preferred_gpu(l) for l in lines):
                conflicts.append(path)
    return conflicts
