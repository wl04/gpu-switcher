"""GPU resolution and the managed rule text.

Used by the root helper (SetPrimary). Pure stdlib, no `gi`: the helper can
import this without pulling in the GUI module (which imports Gtk).
"""

from __future__ import annotations

import re

from gpu_switcher.core.udev import (
    MUTTER_ENV,
    MUTTER_TAG,
    is_display_gpu,
    read_text,
)

# Full `DDDD:BB:DD.D` PCI address (domain included, lowercase hex) - the
# exact form the managed rule and udev KERNELS== matching use.
_PCI_ADDRESS_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9]")


# sysfs `vendor` / `device` value: 0x followed by four lowercase hex digits.
_PCI_ID_RE = re.compile(r"0x[0-9a-f]{4}")


def is_valid_pci_format(pci: str) -> bool:
    """Full `DDDD:BB:DD.D` PCI address (domain included, lowercase hex).

    The exact form the managed rule and udev KERNELS== matching use;
    domain-omitted or non-hex addresses are rejected before any /sys read.
    """
    return _PCI_ADDRESS_RE.fullmatch(pci) is not None


def resolve_gpu(pci: str) -> tuple[str, str, str] | None:
    """(card, vendor, device) for a valid display controller, else None
    (also None when the vendor or device id cannot be read).

    Thin wrapper over the shared is_display_gpu contract (display class,
    bound driver, exactly one /dev/dri card for this PCI); the PCI address
    itself is validated first so no /sys read happens on bad input.
    """
    if not is_valid_pci_format(pci):
        return None
    card = is_display_gpu(pci)
    if card is None:
        return None
    pdir = f"/sys/bus/pci/devices/{pci}"
    vendor = read_text(f"{pdir}/vendor")
    device = read_text(f"{pdir}/device")
    # An unreadable attribute reads as "", which would yield a rule that never
    # matches while the switch is reported as done.
    if not (_PCI_ID_RE.fullmatch(vendor) and _PCI_ID_RE.fullmatch(device)):
        return None
    return card, vendor, device


def rule_text(pci: str, vendor: str, device: str) -> str:
    """The managed rule file content for the chosen GPU.

    Same rule line format as the reference script mutter-primary-gpu.sh.
    """
    return (
        f"# Managed by gpu-switcher; selected PCI {pci}\n"
        "# Mutter uses TAG; ENV is kept for compatibility with rules that still use it.\n"
        'SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", KERNEL=="card[0-9]*", '
        'KERNEL!="card*-*", SUBSYSTEMS=="pci", KERNELS=="' + pci + '",'
        ' ATTRS{vendor}=="' + vendor + '",'
        ' ATTRS{device}=="' + device + '",'
        f' ENV{{{MUTTER_ENV}}}="1",'
        f' TAG+="{MUTTER_TAG}"\n'
    )
