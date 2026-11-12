"""Which display outputs a DRM card has, and whether anything is plugged in.

sysfs names every connector after its card: `/sys/class/drm/card1-HDMI-A-1/status`
reports `connected`, `disconnected` or `unknown`. This is read-only and needs no
privilege, and it answers a question the rest of detection cannot: switching the
primary adapter to a card with no connected display leaves the session with no
screen until a monitor is plugged in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

from .udev import CARD_NAME_RE, read_text

SYSFS_DRM_DIR = "/sys/class/drm"

# A connector entry is "<card>-<connector>", e.g. card1-HDMI-A-1, card2-eDP-1.
_CONNECTOR_ENTRY_RE = re.compile(r"^(card[0-9]+)-(.+)$")

# Internal framebuffers exposed as connectors: nothing plugs into them, and they
# report no useful status.
_WRITEBACK_PREFIX = "Writeback"

# The two statuses that carry meaning. Anything else - "unknown" included, which
# drivers report for outputs they cannot probe - is not evidence either way.
CONNECTED_STATUS = "connected"
DISCONNECTED_STATUS = "disconnected"


class DisplayState(Enum):
    """CONNECTED: an output on this card reports a monitor.
    NONE: the card reports outputs and none of them is connected.
    UNKNOWN: the card reported no outputs, or a status that means nothing."""

    CONNECTED = "connected"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Connector:
    """One display output of a card, as sysfs names it."""

    card: str
    name: str  # the connector part of the entry name: "HDMI-A-1"
    status: str  # what the kernel reports: "connected", "disconnected", ...


@dataclass(frozen=True)
class Displays:
    """What sysfs says about a card's outputs: the verdict, and the names of the
    outputs connected now (the evidence behind a CONNECTED verdict)."""

    state: DisplayState
    connected_names: list[str] = field(default_factory=list)


def connectors(card: str, *, drm_dir: str = SYSFS_DRM_DIR) -> list[Connector]:
    """The display outputs sysfs reports for `card`, sorted by connector name.

    Returns an empty list when the card has no connector entries or `drm_dir`
    cannot be listed: no outputs were seen, which is not the same as an output
    that is free.
    """
    if not CARD_NAME_RE.fullmatch(card):
        return []
    try:
        entries = sorted(os.listdir(drm_dir))
    except OSError:
        return []
    found: list[Connector] = []
    for entry in entries:
        match = _CONNECTOR_ENTRY_RE.fullmatch(entry)
        if match is None:
            continue  # card nodes, render nodes, and anything unexpected
        entry_card, name = match.group(1), match.group(2)
        if entry_card != card or name.startswith(_WRITEBACK_PREFIX):
            continue
        status = read_text(os.path.join(drm_dir, entry, "status")).strip().lower()
        found.append(Connector(card=card, name=name, status=status))
    return found


def displays(card: str, *, drm_dir: str = SYSFS_DRM_DIR) -> Displays:
    """Whether a monitor is connected to `card`, as far as sysfs can say.

    UNKNOWN rather than NONE when nothing was seen or a status was unreadable or
    meaningless: reporting "no display" is a warning to the user, and it must not
    come from the kernel failing to answer.
    """
    found = connectors(card, drm_dir=drm_dir)
    connected = [c.name for c in found if c.status == CONNECTED_STATUS]
    statuses = {c.status for c in found}
    if connected:
        state = DisplayState.CONNECTED
    elif statuses <= {DISCONNECTED_STATUS} and statuses:
        state = DisplayState.NONE
    else:
        state = DisplayState.UNKNOWN
    return Displays(state=state, connected_names=connected)


def display_state(card: str, *, drm_dir: str = SYSFS_DRM_DIR) -> DisplayState:
    """The verdict of `displays`, for a caller that does not need the names."""
    return displays(card, drm_dir=drm_dir).state
