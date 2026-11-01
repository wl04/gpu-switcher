"""Pure radio-selection logic for the GPU list.

The UI must always mirror the configured state: the selected card is the
detected GPU matching the managed rule on disk, falling back to auto when
no detected GPU matches it (including when no GPUs are detected at all).
Two named entry points share the one rule - the initial UI build and the
revert after a cancelled or failed action - so each call site documents
why it selects.
"""
from __future__ import annotations

from . import detect

# The "System default (auto)" radio's card key (no real card has this name).
SELECTION_AUTO = "auto"


def _configured_card(detection: detect.Detection) -> str | None:
    """Card matching the managed rule's PCI, or None (auto selection)."""
    preferred = detection.preferred_pci
    return next((g.card for g in detection.gpus if g.pci == preferred), None)


def initial_selection(detection: detect.Detection) -> str:
    """Card to select when building the UI: the configured card when a
    detected GPU matches the managed rule, otherwise auto."""
    return _configured_card(detection) or SELECTION_AUTO


def selection_after_failure(detection: detect.Detection) -> str:
    """Card to revert to when an action is cancelled or fails: the
    configured card when a detected GPU matches the managed rule,
    otherwise auto (nothing on disk changed)."""
    return _configured_card(detection) or SELECTION_AUTO
