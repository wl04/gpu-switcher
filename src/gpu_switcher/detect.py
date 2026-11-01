"""Unprivileged primary-GPU detection (M1).

Everything here only reads /sys, the gnome-shell journal, udev properties
and the managed udev rule file; no privileged operation is required.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess

from udev_rules import MANAGED_RULE, read_text, rule_conflicts

JOURNAL_UNIT = "/usr/bin/gnome-shell"

# Display names for when lspci is unavailable.
_KNOWN_GPUS = {
    ("1002", "13c0"): "AMD Radeon Graphics (integrated)",
    ("1002", "7550"): "AMD Radeon RX 9070 XT",
}


@dataclasses.dataclass
class Gpu:
    card: str       # "card2"
    node: str       # "/dev/dri/card2"
    pci: str        # "0000:12:00.0"
    vendor: str     # "1002"
    device: str     # "13c0"
    name: str       # human-readable name (lspci or fallback)
    in_use: bool    # running primary of this boot (journal)
    preferred: bool # configured preferred (managed rule on disk)


@dataclasses.dataclass
class Detection:
    gpus: list
    running_card: str | None  # card picked by gnome-shell this boot
    running_card_error: str | None  # set when the journal could not be read
    preferred_pci: str | None  # PCI from the managed rule on disk
    conflicts: list[str]       # other udev rules setting the tag
    gnome_wayland: bool


_LSPCI_LINE_RE = re.compile(
    r"^\s*([0-9a-f]{4}:)?([0-9a-f]{2}:[0-9a-f]{2}\.[0-9])\s+.*?\]:\s+(.*)$")


def parse_lspci_names(output: str) -> dict:
    """Map PCI address -> display name from `lspci -nn` text.

    Handles both `0000:03:00.0` and domain-omitted `03:00.0` address
    forms, and strips the trailing `[1002:7550]` ID / `(rev ..)` parts.
    """
    names: dict = {}
    for line in output.splitlines():
        m = _LSPCI_LINE_RE.match(line)
        if not m:
            continue
        pci = (m.group(1) or "0000:") + m.group(2)
        # Strip the trailing '(rev xx)', then the '[1002:7550]' ID that
        # precedes it in `lspci -nn` output.
        name = re.sub(r"\s*\([^)]*\)\s*$", "", m.group(3))
        name = re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]\s*$", "", name)
        names[pci] = name.strip()
    return names


def lspci_names() -> dict:
    """Map PCI address -> display name via `lspci -nn`; {} when lspci is
    missing or fails (names then fall back to vendor:device)."""
    try:
        out = subprocess.run(["lspci", "-nn"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    return parse_lspci_names(out.stdout)


def fallback_name(vendor: str, device: str) -> str:
    key = (vendor.lower(), device.lower())
    if key in _KNOWN_GPUS:
        return _KNOWN_GPUS[key]
    return f"PCI v0x{vendor} d0x{device}"


# gnome-shell logs the primary selection once per session start, in two
# forms:
#   rule mode:  GPU /dev/dri/cardN selected primary given udev rule
#   auto mode:  Boot VGA GPU /dev/dri/cardN selected as primary
PRIMARY_LINE_RE = re.compile(r"GPU /dev/dri/(card[0-9]+) selected (?:as )?primary")


def primary_card_from_line(line: str) -> str | None:
    """Card name ('cardN') from a gnome-shell primary-selection journal line."""
    m = PRIMARY_LINE_RE.search(line)
    return m.group(1) if m else None


def last_primary_card(lines: list[str]) -> str | None:
    """Card from the last primary-selection journal line, or None."""
    card = None
    for line in lines:
        card = primary_card_from_line(line) or card
    return card


def running_primary() -> tuple[str | None, str | None]:
    """Card picked by gnome-shell this boot (e.g. 'card2'), and the error
    text when the journal could not be read (None on success)."""
    try:
        out = subprocess.run(["journalctl", "-b", "-q", JOURNAL_UNIT],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"journalctl could not be run ({error})"
    if out.returncode != 0:
        return None, f"journalctl exited with code {out.returncode}"
    return last_primary_card(out.stdout.splitlines()), None


def managed_rule_pci_from_text(text: str) -> str | None:
    """PCI address from a managed udev rule's text, or None (auto selection)."""
    m = re.search(r'KERNELS=="([^"]+)"', text)
    return m.group(1) if m else None


def managed_rule_pci() -> str | None:
    """PCI address from the managed rule file, or None (auto selection)."""
    return managed_rule_pci_from_text(read_text(MANAGED_RULE))


def is_gnome_wayland(session_type: str, gnome_shell_present: bool) -> bool:
    """True when the session is Wayland and gnome-shell is installed."""
    return session_type.lower() == "wayland" and gnome_shell_present


def gnome_wayland_session() -> bool:
    """Whether the current desktop session is GNOME on Wayland."""
    return is_gnome_wayland(os.environ.get("XDG_SESSION_TYPE", ""),
                            shutil.which("gnome-shell") is not None)


def detect() -> Detection:
    """Full detection snapshot used by the UI (and later by M2)."""
    gpus = []
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        entries = []
    names = lspci_names()
    for entry in entries:
        # Only real cards ("cardN"); connectors are "cardN-CONN".
        if not re.match(r"^card[0-9]+$", entry):
            continue
        pci = os.path.basename(os.path.realpath(f"/sys/class/drm/{entry}/device"))
        pdir = f"/sys/bus/pci/devices/{pci}"
        if not os.path.isdir(pdir) or read_text(f"{pdir}/class") != "0x030000":
            continue
        node = f"/dev/dri/{entry}"
        if not os.path.exists(node):
            continue
        vendor = read_text(f"{pdir}/vendor").removeprefix("0x").lower()
        device = read_text(f"{pdir}/device").removeprefix("0x").lower()
        gpus.append(Gpu(
            card=entry,
            node=node,
            pci=pci,
            vendor=vendor,
            device=device,
            name=names.get(pci) or fallback_name(vendor, device),
            in_use=False,
            preferred=False,
        ))
    running_card, running_card_error = running_primary()
    preferred = managed_rule_pci()
    for g in gpus:
        g.in_use = g.card == running_card
        g.preferred = g.pci == preferred
    return Detection(gpus=gpus, running_card=running_card,
                     running_card_error=running_card_error,
                     preferred_pci=preferred,
                     conflicts=rule_conflicts(on_unreadable="skip"),
                     gnome_wayland=gnome_wayland_session())
