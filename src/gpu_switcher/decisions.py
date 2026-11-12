"""The decisions a front-end makes before it draws anything.

The window is not the thing that decides: it turns these answers into widgets.
What Apply means for a selection, which warning a snapshot deserves first, what a
GPU row advertises and whether the state row has anything to say are answers about
the situation, and a command-line front-end needs the same ones. Keeping them here
also keeps them testable without a display.

Every string is produced through `i18n._`, so a caller that prints these answers
outside the window translates them the same way the window does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import detect, i18n
from .core.connectors import DisplayState
from .gpu_state import GpuState, describe, presentation
from .selection import (
    SELECTION_AUTO,
    SELECTION_UNAVAILABLE,
    apply_target,
    configured_unavailable_pci,
)
from .state import BannerKind


class ApplyKind(Enum):
    """What Apply should do with a selection.

    CONFIRM: there is a target to send, and the user has to agree to it.
    EXPLAIN: there is nothing to send, and the reason has to be said instead
    (silently falling back to auto would rewrite a rule the user did not ask about).
    RECHECK: the selection no longer matches what was detected, so the world has
    to be read again rather than acted upon.
    """

    CONFIRM = "confirm"
    EXPLAIN = "explain"
    RECHECK = "recheck"


@dataclass(frozen=True)
class ApplyDecision:
    """One answer about Apply, ready for any front-end to present.

    `target` is set only for CONFIRM (a PCI, or selection.SELECTION_AUTO).
    `banner` is set only for EXPLAIN and RECHECK: the text and its style.
    `confirm_text` is set only for CONFIRM: what to ask the user to agree to.
    """

    kind: ApplyKind
    target: str | None = None
    banner: tuple[str, BannerKind] | None = None
    confirm_text: str | None = None


def gpu_badges(gpu: detect.Gpu) -> list[str]:
    """What a GPU row advertises about this card, in display order.

    The display marks come last: which adapter GNOME runs on and which one is
    configured to run on it is what the rest of the window is about, while whether
    a cable is plugged into the card is a property of the card itself. Nothing is
    said about displays when sysfs could not answer: "no display" is a claim a
    user acts on.
    """
    badges: list[str] = []
    if gpu.in_use:
        badges.append(i18n._("In use now"))
    if gpu.preferred:
        badges.append(i18n._("Preferred (configured)"))
    if gpu.display_state is DisplayState.NONE:
        badges.append(i18n._("No display connected"))
    elif gpu.connected_displays:
        badges.append(i18n._("Display: %s") % ", ".join(gpu.connected_displays))
    return badges


def apply_warning(gpu: detect.Gpu) -> str | None:
    """A risk the confirmation has to name, or None when the target is ordinary.

    Switching the primary adapter to a card whose outputs are all free leaves the
    session without a screen until a cable is moved onto one of them. Apply cannot
    fix that, so the user has to be able to expect it. No warning for a card with a
    monitor on it, and none when the kernel did not answer.
    """
    if gpu.display_state is not DisplayState.NONE:
        return None
    return (
        i18n._(
            "Warning: no display is currently connected to %s. Switching to it may "
            "leave the session without a screen until a monitor is plugged into one of "
            "its outputs."
        )
        % gpu.name
    )


def apply_decision(detection: detect.Detection, selected_card: str) -> ApplyDecision:
    """What Apply means for a card selection: confirm, explain, or re-check.

    `selected_card` is a card name from the last detection, or one of
    selection.SELECTION_AUTO / selection.SELECTION_UNAVAILABLE. The GPU is looked
    up in this snapshot only, so a card that vanished since the rows were built
    comes back as RECHECK rather than as a write for a GPU that is not there.
    Never raises for a selection that no longer matches: an unknown selection is a
    re-check, not an error.
    """
    target, error = apply_target(detection, selected_card)
    if error is not None:
        if error == SELECTION_UNAVAILABLE:
            pci = configured_unavailable_pci(detection)
            return ApplyDecision(
                ApplyKind.EXPLAIN,
                banner=(
                    i18n._(
                        "The configured GPU (PCI %s) is not currently "
                        "detected, so there is nothing to apply. The rule "
                        "stays on disk; use 'Reset to auto' to remove it."
                    )
                    % pci,
                    BannerKind.WARNING,
                ),
            )
        return ApplyDecision(
            ApplyKind.RECHECK,
            banner=(
                i18n._("The selected GPU is no longer detected; re-checking…"),
                BannerKind.WARNING,
            ),
        )

    if target == SELECTION_AUTO:
        return ApplyDecision(
            ApplyKind.CONFIRM,
            target=SELECTION_AUTO,
            confirm_text=i18n._(
                "Apply removes the managed udev rule, returning to automatic "
                "GPU selection.\n\n"
                "If a rule is present, a backup is saved. The change takes "
                "effect after a reboot, or after logging out and back in."
            ),
        )

    assert target is not None  # apply_target returns a PCI whenever error is None
    gpu = next(g for g in detection.gpus if g.pci == target)
    confirm_text = i18n._(
        "Apply sets %s (PCI %s) as the primary display adapter.\n\n"
        "The managed udev rule is written; if a rule is present, a "
        "backup is saved. The change takes effect after a reboot, or "
        "after logging out and back in."
    ) % (gpu.name, gpu.pci)
    warning = apply_warning(gpu)
    if warning is not None:
        confirm_text += "\n\n" + warning
    return ApplyDecision(ApplyKind.CONFIRM, target=target, confirm_text=confirm_text)


def startup_banner(
    detection: detect.Detection,
) -> tuple[str, BannerKind] | None:
    """The warning a snapshot deserves first, or None when nothing needs saying.

    Several things can be wrong at once and a front-end has room for one warning,
    so this picks it: a configured GPU that is not currently detected comes first,
    because every other reading of the state depends on it, then a rule the app
    cannot interpret, then a foreign rule setting the tag. None for a snapshot with
    nothing to resolve.
    """
    unavailable_pci = configured_unavailable_pci(detection)
    if unavailable_pci is not None:
        return (
            i18n._(
                "The configured GPU (PCI %s) is not currently "
                "detected. The rule remains on disk; it takes "
                "effect when the GPU is available again. Use "
                "'Reset to auto' to remove it."
            )
            % unavailable_pci,
            BannerKind.WARNING,
        )
    if detection.rule_state in (
        detect.RuleState.UNRECOGNIZED,
        detect.RuleState.UNREADABLE,
    ):
        return (
            i18n._(
                "The managed udev rule could not be interpreted, so the "
                "configured GPU is unknown (this is not automatic "
                "selection). Use 'Reset to auto' or Apply to rewrite it."
            ),
            BannerKind.WARNING,
        )
    if detection.conflicts:
        return (
            i18n._(
                "Another udev rule sets the preferred GPU: %s. "
                "Resolve it manually first."
            )
            % detection.conflicts[0],
            BannerKind.WARNING,
        )
    return None


def state_status(
    state: GpuState, detection: detect.Detection
) -> tuple[str, BannerKind, str] | None:
    """What the state row says: (text, style, icon name), or None when it has
    nothing to add.

    Silent in two cases worth naming: a state with no text at all (not a GNOME
    Wayland session, where the session warning already says that), and an unknown
    state whose cause another warning already explains - a rule that cannot be
    read, a rule that is not understood, a rule pinning an absent GPU, or a
    conflicting rule. Repeating "unknown" under such a warning adds a line and no
    information.
    """
    text = describe(state, detection)
    rule_problem = (
        configured_unavailable_pci(detection) is not None
        or detection.conflicts
        or detection.rule_state
        in (detect.RuleState.UNRECOGNIZED, detect.RuleState.UNREADABLE)
    )
    if not text or (state is GpuState.UNKNOWN and rule_problem):
        return None
    kind, icon_name = presentation(state)
    return text, kind, icon_name
