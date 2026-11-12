"""App-side D-Bus clients.

Talks to three services:

* the gpu-switcher helper (system bus): the polkit-gated privileged work;
* login1 (system bus): Reboot;
* the GNOME session manager (session bus): Log out.

All calls are asynchronous; callbacks run on the GLib main loop, so the UI
never blocks (the interactive polkit dialog can stay open for minutes). The
`locale` is passed explicitly because the root-activated helper has no user
locale of its own.
"""

from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.udev import HELPER_TIMEOUT_MS

from . import i18n, state

HELPER_NAME = "com.github.wl04.gpu_switcher.Helper"
HELPER_PATH = "/com/github/wl04/gpu_switcher/Helper"
HELPER_IFACE = "com.github.wl04.gpu_switcher.Helper"
# The helper publishes its own interface (helper/service.py INTROSPECTION_XML);
# these are the app's copy of it, named so a test can compare the two. Plain
# literals per call site: a renamed method or a reordered reply argument shows up
# as a D-Bus error rather than at compile time.
HELPER_SET_PRIMARY = "SetPrimary"
HELPER_SET_PRIMARY_IN = "(ss)"
HELPER_SET_PRIMARY_OUT = "(bssb)"
HELPER_SET_AUTO = "SetAuto"
HELPER_SET_AUTO_IN = "(s)"
HELPER_SET_AUTO_OUT = "(bsb)"

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"

SESSION_MANAGER_NAME = "org.gnome.SessionManager"
SESSION_MANAGER_PATH = "/org/gnome/SessionManager"
SESSION_MANAGER_IFACE = "org.gnome.SessionManager"
# Logout mode 1: no GNOME confirmation dialog (the app already confirmed).
SESSION_MANAGER_LOGOUT_MODE = 1

# HELPER_TIMEOUT_MS comes from core/udev.py, derived from the helper's worst
# case. Reboot and logout are different services doing different work, so each
# gets its own budget.
LOGIN1_TIMEOUT_MS = 15000
SESSION_MANAGER_TIMEOUT_MS = 15000


_REMOTE_ERROR_PREFIX = "GDBus.Error:"


def _strip_remote_prefix(message: str) -> str:
    """Drop the "GDBus.Error:<name>: " prefix GDBus adds to remote errors, so
    the user sees the service's own message. (Gio.DBusError.strip_remote_error
    edits the C GError in place and does not reach the Python exception.)"""
    if message.startswith(_REMOTE_ERROR_PREFIX):
        _name, separator, text = message.partition(": ")
        if separator:
            return text
    return message


def _call_async(
    bus_type: int,
    name: str,
    path: str,
    iface: str,
    method: str,
    params: GLib.Variant,
    reply_signature: str,
    timeout_ms: int,
    on_success,
    on_error,
    on_bus_error,
) -> None:
    """Issue one async D-Bus method call and dispatch to the handlers.

    on_success(reply) runs when the method returns, with the unpacked tuple
    of out-args (possibly empty); on_error(error, message) runs on a
    method-level error; on_bus_error(GLib.Error) runs when the bus connection
    itself cannot be acquired. The reply is delivered on the GLib main loop,
    so the UI never blocks while the interactive polkit dialog is open.

    `message` is the service's own text, with the bus prefix removed, and the
    error is passed unchanged: its prefix is what makes a remote error
    recognizable, so rewriting the message first would hide that the helper
    never answered (see state.is_outcome_unknown).
    """
    try:
        connection = Gio.bus_get_sync(bus_type)
    except GLib.Error as error:
        on_bus_error(error)
        return

    def on_call(c, task):
        try:
            reply = c.call_finish(task)
        except GLib.Error as error:
            on_error(error, _strip_remote_prefix(error.message))
            return
        on_success(reply)

    connection.call(
        name,
        path,
        iface,
        method,
        params,
        GLib.VariantType(reply_signature),
        0,
        timeout_ms,
        None,
        on_call,
    )


def _helper_failure_text(error: GLib.Error, message: str) -> str:
    """The app's sentence for a failed helper call, in the user's language.

    Some failures are answered by the bus itself in English: no service owns
    the helper's name, the helper stopped answering, the client gave up. A
    rejection the helper composed in the requested locale already explains
    itself and is passed through unchanged.
    """
    if state.is_helper_missing(error):
        return i18n._(
            "The GPU helper is not available. Install the gpu-switcher package."
        )
    if state.is_gio_timeout(error):
        return i18n._("The GPU helper did not answer in time.")
    if state.is_outcome_unknown(error):
        return i18n._("The GPU helper stopped answering.")
    return message


def _on_helper_error(callback, error: GLib.Error, message: str) -> None:
    """Dispatch a failed SetPrimary/SetAuto call to the app callback.

    A helper no service owns never received the call, so nothing changed.
    A helper that was reached and then vanished leaves the outcome unknown:
    the rule may already be on disk.
    """
    callback(
        False,
        _helper_failure_text(error, message),
        False,
        state.is_outcome_unknown(error),
    )


def set_primary(pci: str, locale: str, callback) -> None:
    """Ask the helper to write the managed rule for this PCI address.

    The helper also refreshes udev under the same authorization, so no second
    prompt is needed. callback(ok, message, refresh_ok, outcome_unknown):
    outcome_unknown means the helper may already have changed the rule, so the
    app re-detects instead of assuming nothing happened. The helper's backup
    path is not passed on to the UI.
    """
    helper_unreachable = i18n._("Could not reach the GPU helper: %s.")
    _call_async(
        Gio.BusType.SYSTEM,
        HELPER_NAME,
        HELPER_PATH,
        HELPER_IFACE,
        HELPER_SET_PRIMARY,
        GLib.Variant(HELPER_SET_PRIMARY_IN, (pci, locale)),
        HELPER_SET_PRIMARY_OUT,
        HELPER_TIMEOUT_MS,
        lambda r: callback(bool(r[0]), r[1], bool(r[3]), False),
        lambda error, message: _on_helper_error(callback, error, message),
        lambda e: callback(False, helper_unreachable % e.message, False, False),
    )


def set_auto(locale: str, callback) -> None:
    """Ask the helper to remove the managed rule (auto selection).

    The helper also refreshes udev under the same authorization.
    callback(ok, message, refresh_ok, outcome_unknown): outcome_unknown means
    the helper may already have changed the rule.
    """
    helper_unreachable = i18n._("Could not reach the GPU helper: %s.")
    _call_async(
        Gio.BusType.SYSTEM,
        HELPER_NAME,
        HELPER_PATH,
        HELPER_IFACE,
        HELPER_SET_AUTO,
        GLib.Variant(HELPER_SET_AUTO_IN, (locale,)),
        HELPER_SET_AUTO_OUT,
        HELPER_TIMEOUT_MS,
        lambda r: callback(bool(r[0]), r[1], bool(r[2]), False),
        lambda error, message: _on_helper_error(callback, error, message),
        lambda e: callback(False, helper_unreachable % e.message, False, False),
    )


def reboot(callback) -> None:
    """login1 Manager.Reboot(interactive=False): logind's own polkit action
    applies; no extra prompt is requested.

    callback(ok: bool, message: str)
    """
    logind_unreachable = i18n._("Could not reach logind: %s.")
    _call_async(
        Gio.BusType.SYSTEM,
        LOGIN1_NAME,
        LOGIN1_PATH,
        LOGIN1_MANAGER_IFACE,
        "Reboot",
        GLib.Variant("(b)", (False,)),
        "()",
        LOGIN1_TIMEOUT_MS,
        lambda r: callback(True, ""),
        lambda _error, message: callback(False, message),
        lambda e: callback(False, logind_unreachable % e.message),
    )


def logout(callback) -> None:
    """GNOME session manager Logout for our own session (the fast path).

    Calls org.gnome.SessionManager.Logout(mode) on the session bus, so the
    session manager ends the session itself and honors inhibitors. Mode 1
    skips its confirmation dialog (the app already confirmed). The session
    manager exists only in a GNOME session, so a failed call also covers the
    non-GNOME case.

    callback(ok: bool, message: str)
    """
    logout_failed = i18n._("Could not log out: %s.")
    _call_async(
        Gio.BusType.SESSION,
        SESSION_MANAGER_NAME,
        SESSION_MANAGER_PATH,
        SESSION_MANAGER_IFACE,
        "Logout",
        GLib.Variant("(u)", (SESSION_MANAGER_LOGOUT_MODE,)),
        "()",
        SESSION_MANAGER_TIMEOUT_MS,
        lambda r: callback(True, ""),
        lambda _error, message: callback(False, logout_failed % message),
        lambda e: callback(False, logout_failed % e.message),
    )
