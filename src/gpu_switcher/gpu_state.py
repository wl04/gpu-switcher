"""Derived GPU state: what runs now versus what is configured.

Pure logic (no Gtk). Combines the detection snapshot (running primary, rule
on disk, rule and session timestamps) with the last Apply/Reset outcome into
one state that the status banner renders. It covers both "what is configured
for the next login" and the startup check that GNOME honored the rule.
"""

from __future__ import annotations

from enum import Enum

from . import detect, i18n
from .state import BannerKind, Outcome


class GpuState(Enum):
    """AUTO: no rule; GNOME picked the GPU itself.
    ACTIVE: the running primary is the configured GPU (verified).
    PENDING_LOGIN: the rule changed this session and the udev refresh
        succeeded; a logout/login applies it.
    PENDING_REBOOT: the rule changed and the refresh failed or is unknown, or udev
        does not carry the tag at all; a reboot is the safe path.
    IGNORED: the rule predates the session and udev applies it, but GNOME picked
        another GPU.
    UNKNOWN: the state cannot be determined (journal unreadable, rule
        unreadable or unrecognized, conflicting rule, GPU not detected).
    NOT_APPLICABLE: not a GNOME Wayland session.
    """

    AUTO = "auto"
    ACTIVE = "active"
    PENDING_LOGIN = "pending_login"
    PENDING_REBOOT = "pending_reboot"
    IGNORED = "ignored"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


# Banner style and symbolic icon per state. The text stays the primary
# carrier of meaning; the icon is a second visual channel.
_PRESENTATION: dict[GpuState, tuple[BannerKind, str]] = {
    GpuState.AUTO: (BannerKind.INFO, "computer-symbolic"),
    GpuState.ACTIVE: (BannerKind.SUCCESS, "emblem-ok-symbolic"),
    GpuState.PENDING_LOGIN: (BannerKind.INFO, "view-refresh-symbolic"),
    GpuState.PENDING_REBOOT: (BannerKind.WARNING, "system-reboot-symbolic"),
    GpuState.IGNORED: (BannerKind.WARNING, "dialog-warning-symbolic"),
    GpuState.UNKNOWN: (BannerKind.INFO, "dialog-question-symbolic"),
    GpuState.NOT_APPLICABLE: (BannerKind.INFO, "dialog-information-symbolic"),
}


def presentation(state: GpuState) -> tuple[BannerKind, str]:
    """(banner kind, symbolic icon name) for a state."""
    return _PRESENTATION[state]


def _configured_card(detection: detect.Detection) -> str | None:
    return next(
        (g.card for g in detection.gpus if g.pci == detection.preferred_pci), None
    )


def _changed_since_session_start(detection: detect.Detection, outcome: Outcome) -> bool:
    """True when the rule was changed after the running gnome-shell started
    (this app's last Apply/Reset, or an earlier one found via the rule's
    mtime)."""
    if outcome in (Outcome.success_refreshed, Outcome.success_refresh_failed):
        return True
    mtime, start = detection.rule_mtime, detection.session_start
    return mtime is not None and start is not None and mtime > start


def _pending(outcome: Outcome, tag_applied: bool | None) -> GpuState:
    # The refresh result is not persisted, so with no evidence a reboot is the
    # safe advice. Tag evidence only makes the answer more cautious, never
    # less: a card without the tag never got the rule, while a card carrying it
    # may hold a tag left by a reload that failed.
    if outcome is Outcome.success_refreshed and tag_applied is not False:
        return GpuState.PENDING_LOGIN
    return GpuState.PENDING_REBOOT


def derive_state(detection: detect.Detection, outcome: Outcome) -> GpuState:
    """The state for a detection snapshot and the last Apply/Reset outcome."""
    if not detection.gnome_wayland:
        return GpuState.NOT_APPLICABLE
    if detection.conflicts or detection.rule_state in (
        detect.RuleState.UNRECOGNIZED,
        detect.RuleState.UNREADABLE,
    ):
        return GpuState.UNKNOWN
    changed = _changed_since_session_start(detection, outcome)
    running = detection.running_card
    if detection.rule_state is detect.RuleState.ABSENT:
        if changed:
            return _pending(outcome, detection.tag_applied)
        return GpuState.AUTO if running is not None else GpuState.UNKNOWN
    configured = _configured_card(detection)
    if configured is None:
        return GpuState.UNKNOWN
    if running == configured:
        return GpuState.ACTIVE
    if changed:
        return _pending(outcome, detection.tag_applied)
    if running is None:
        return GpuState.UNKNOWN
    if detection.tag_applied is False:
        # A rule that predates this session and carries no tag never reached
        # udev - a different fault from GNOME overriding a rule udev applied.
        return GpuState.PENDING_REBOOT
    return GpuState.IGNORED


def _gpu_name(detection: detect.Detection, card: str | None) -> str:
    gpu = next((g for g in detection.gpus if g.card == card), None)
    return gpu.name if gpu is not None else str(card)


def describe(state: GpuState, detection: detect.Detection) -> str:
    """User-facing banner text for a state; empty when nothing is to show."""
    _ = i18n._
    # Only the ignored state names the GPUs (the point is that they differ);
    # elsewhere the list's badges already mark the configured one.
    configured = (
        _gpu_name(detection, _configured_card(detection))
        if detection.preferred_pci
        else _("automatic selection")
    )
    if state is GpuState.AUTO:
        return _("Running on %s (automatic selection).") % _gpu_name(
            detection, detection.running_card
        )
    if state is GpuState.ACTIVE:
        return _("GNOME is using the configured GPU.")
    if state is GpuState.PENDING_LOGIN:
        if detection.preferred_pci:
            return _("Configured for next login. Log out and back in to apply it.")
        return _("Automatic selection applies at next login. Log out and back in.")
    if state is GpuState.PENDING_REBOOT:
        return _("The change applies after a reboot.")
    if state is GpuState.IGNORED:
        return _(
            "GNOME is running on %(running)s, not the configured %(configured)s. "
            "Check for conflicting rules or whether that GPU was available at login."
        ) % {
            "running": _gpu_name(detection, detection.running_card),
            "configured": configured,
        }
    if state is GpuState.UNKNOWN:
        if detection.running_card_error is not None:
            return (
                _("Running primary: could not read the session journal (%s).")
                % detection.running_card_error
            )
        return _("Running primary: unknown (verify in the journal after restart).")
    return ""
