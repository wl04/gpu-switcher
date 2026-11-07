"""Pure radio-selection logic for the GPU list.

The selection mirrors the configured state: the detected GPU matching the
managed rule; a separate "unavailable" state when the rule pins a PCI no
detected GPU has (the rule is still on disk, so this is not auto); auto only
when no rule pins a GPU.
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

    Returns (target, error), at most one of them non-None. target is
    SELECTION_AUTO or the selected GPU's PCI. error is SELECTION_UNAVAILABLE
    for the unavailable row, or the card name when it is no longer detected
    (the UI refuses and re-checks rather than falling back to auto).
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

    Used for the initial build and to revert after a cancelled or failed
    action: the UI always shows what is on disk, not an un-applied choice.
    """
    card = _configured_card(detection)
    if card is not None:
        return card
    if configured_unavailable_pci(detection) is not None:
        return SELECTION_UNAVAILABLE
    return SELECTION_AUTO
