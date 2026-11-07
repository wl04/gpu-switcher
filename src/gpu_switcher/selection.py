"""Pure radio-selection logic for the GPU list.

The UI must always mirror the configured state. Three-way mirror: the
selected card is the detected GPU matching the managed rule on disk; when
the rule pins a PCI no detected GPU carries, the selection is the distinct
configured-unavailable state (the rule still exists on disk, so this is not
auto); and auto only when no rule pins a GPU. One entry point,
mirror_configured_state, serves both the initial UI build and the revert
after a cancelled or failed action; each call site documents why it
selects.
"""

from __future__ import annotations

from . import detect

# The "System default (auto)" radio's card key (no real card has this name).
SELECTION_AUTO = "auto"

# Card key for the "Configured GPU (not currently detected)" state: the
# managed rule pins a PCI no detected GPU carries.
SELECTION_UNAVAILABLE = "configured_unavailable"


def _configured_card(detection: detect.Detection) -> str | None:
    """Card matching the managed rule's PCI, or None."""
    preferred = detection.preferred_pci
    return next((g.card for g in detection.gpus if g.pci == preferred), None)


def configured_unavailable_pci(detection: detect.Detection) -> str | None:
    """The rule's PCI when it pins a GPU that is not currently detected,
    else None."""
    card = _configured_card(detection)
    if card is None:
        return detection.preferred_pci
    return None


def apply_target(
    detection: detect.Detection, selected_card: str
) -> tuple[str | None, str | None]:
    """Validate a radio selection for Apply.

    Returns (target, error): target is SELECTION_AUTO or the selected
    GPU's PCI when the selection can be applied; error is the
    SELECTION_UNAVAILABLE sentinel for the unavailable row, or the
    selected card name when it is no longer detected (the UI re-checks
    and refuses instead of silently switching to auto). At most one of
    the two is non-None.
    """
    if selected_card == SELECTION_AUTO:
        return SELECTION_AUTO, None
    if selected_card == SELECTION_UNAVAILABLE:
        return None, SELECTION_UNAVAILABLE
    gpu = next((g for g in detection.gpus if g.card == selected_card), None)
    if gpu is None:
        return None, selected_card
    return gpu.pci, None


def mirror_configured_state(detection: detect.Detection) -> str:
    """The card mirroring the configured state: the detected card matching
    the managed rule, the unavailable sentinel when the rule pins an
    undetected GPU, or auto when no rule pins a GPU.

    Both call sites rely on this single rule: the initial UI build and the
    revert after a cancelled or failed action. Each call site in main.py
    documents *why* it selects, since the logic here is intent-agnostic.
    """
    card = _configured_card(detection)
    if card is not None:
        return card
    if configured_unavailable_pci(detection) is not None:
        return SELECTION_UNAVAILABLE
    return SELECTION_AUTO
