"""Explicit operation-outcome state for the app.

Pure logic (GLib only, no Gtk): instead of a bare fast_path_pending flag,
the app keeps one explicit outcome per Apply/Reset; button sensitivity
and the result banner are derived from it, so the banner survives the
_refresh() rebuild and Log out is enabled only after a successful,
refreshed operation.
"""

from __future__ import annotations

from enum import Enum

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from gpu_switcher.core.udev import G_IO_ERROR_TIMEOUT


class Outcome(Enum):
    """What the last Apply/Reset left behind.

    idle: no operation ran, or it changed nothing (startup state).
    success_refreshed: the change was applied and the udev refresh
        succeeded (tags fresh; fast-path logout is valid).
    success_refresh_failed: the change was applied but the udev refresh
        failed (rule on disk; a reboot is required).
    unknown: the client call timed out; the helper's outcome is not
        known (the app re-detects and reconciles with the rule on disk).
    """

    idle = "idle"
    success_refreshed = "success_refreshed"
    success_refresh_failed = "success_refresh_failed"
    unknown = "unknown"


class BannerKind(Enum):
    """The result banner's style (main.py maps these to Adwaita classes)."""

    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"


def outcome_from_result(ok: bool, refresh_ok: bool, timed_out: bool) -> Outcome:
    """Outcome for a completed SetPrimary/SetAuto result.

    A timeout is unknown regardless of the rest: the client gave up
    before the helper finished. A call that changed nothing (ok=False,
    no timeout) returns to idle.
    """
    if timed_out:
        return Outcome.unknown
    if not ok:
        return Outcome.idle
    return Outcome.success_refreshed if refresh_ok else Outcome.success_refresh_failed


def is_gio_timeout(error: GLib.Error) -> bool:
    """True when a GLib.Error is G_IO_ERROR_TIMEOUT (g-io-error-quark, 7)."""
    return error.matches(GLib.quark_from_string("g-io-error-quark"), G_IO_ERROR_TIMEOUT)


def actions_enabled(gnome_wayland: bool) -> bool:
    """Privileged actions (Apply / Reset / Reboot / Log out) are enabled
    only in a GNOME Wayland session; DESIGN.md supports GNOME only."""
    return gnome_wayland


def logout_enabled(busy: bool, outcome: Outcome) -> bool:
    """Sensitivity rule: Log out is enabled iff not busy AND the outcome
    is success_refreshed — the tags are fresh, so a logout/login switches
    the primary without another prompt. A failed refresh, a call that
    changed nothing, or an unknown outcome keeps it disabled (reboot is
    the path left)."""
    return not busy and outcome is Outcome.success_refreshed


def banner_kind(outcome: Outcome) -> BannerKind:
    """Banner style for a completed, non-timeout Apply/Reset: success when
    the refresh succeeded, warning when it failed (rule on disk, a reboot
    is required)."""
    if outcome is Outcome.success_refreshed:
        return BannerKind.SUCCESS
    return BannerKind.WARNING
