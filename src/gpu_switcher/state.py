"""Explicit operation-outcome state for the app.

Pure logic (GLib only, no Gtk): the app keeps one explicit outcome per
Apply/Reset; button sensitivity and the result banner are derived from it,
so the banner survives the _refresh() rebuild and Log out is enabled only
after a successful, refreshed operation.
"""

from __future__ import annotations

from enum import Enum

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


class Outcome(Enum):
    """What the last Apply/Reset left behind.

    idle: nothing ran or nothing changed (also the startup state).
    success_refreshed: applied and udev refreshed (tags fresh, so Log out
        is valid).
    success_refresh_failed: applied, but the refresh failed (reboot needed).
    unknown: the client call timed out, so the outcome is unknown (the app
        re-detects the rule on disk).
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


# Remote D-Bus errors meaning the helper vanished or never answered; the
# rule may already have been committed.
_AMBIGUOUS_DBUS_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.NoServer",
    }
)


def is_gio_timeout(error: GLib.Error) -> bool:
    """True when a GLib.Error is Gio.IOErrorEnum.TIMED_OUT."""
    return error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)


def is_outcome_unknown(error: GLib.Error) -> bool:
    """True when a failed method call does not prove the helper changed nothing.

    Covers the client timeout, a closed connection and D-Bus no-reply /
    service-disappearance errors. An explicit helper rejection arrives as a
    normal reply and never reaches this check.
    """
    if is_gio_timeout(error) or error.matches(
        Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED
    ):
        return True
    if not Gio.DBusError.is_remote_error(error):
        return False
    return Gio.DBusError.get_remote_error(error) in _AMBIGUOUS_DBUS_ERRORS


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
