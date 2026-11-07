"""Pure radio-selection logic for the GPU list.

The UI must always mirror the configured state. Three-way mirror: the
selected card is the detected GPU matching the managed rule on disk; when
the rule pins a PCI no detected GPU carries, the selection is the distinct
configured-unavailable state (the rule still exists on disk, so this is not
auto); and auto only when no rule pins a GPU. Two named entry points share
the one rule - the initial UI build and the revert after a cancelled or
failed action - so each call site documents why it selects.
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


def _mirror_configured_state(detection: detect.Detection) -> str:
    """Card mirroring the configured state: the matching detected card,
    the unavailable sentinel when the rule pins an undetected GPU, or
    auto when no rule pins a GPU."""
    card = _configured_card(detection)
    if card is not None:
        return card
    if configured_unavailable_pci(detection) is not None:
        return SELECTION_UNAVAILABLE
    return SELECTION_AUTO


def initial_selection(detection: detect.Detection) -> str:
    """Card to select when building the UI: the configured card when a
    detected GPU matches the managed rule, the unavailable sentinel when
    the rule pins an undetected GPU, otherwise auto."""
    return _mirror_configured_state(detection)


def selection_after_failure(detection: detect.Detection) -> str:
    """Card to revert to when an action is cancelled or fails: the
    configured card when a detected GPU matches the managed rule, the
    unavailable sentinel when the rule pins an undetected GPU, otherwise
    auto (nothing on disk changed)."""
    return _mirror_configured_state(detection)
