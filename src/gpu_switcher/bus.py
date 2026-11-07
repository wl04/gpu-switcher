"""App-side D-Bus clients.

The GUI talks to two services:

* the gpu-switcher helper on the system bus — a root, D-Bus-activated
  service that performs the polkit-gated privileged work (writing /
  removing the managed udev rule, reloading and re-triggering udev
  rules);
* login1 (systemd-logind) on the system bus — for Reboot; and the GNOME
  session manager on the session bus — for Log out (the session manager
  performs the logout with its own handling; inhibitors are honored).

All calls are asynchronous: the callback is delivered on the GLib main
loop, so the UI never blocks while the interactive polkit dialog is open.
The `locale` arguments are passed explicitly because the helper runs as a
root-activated process with no user locale of its own.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.udev import HELPER_TIMEOUT_MS

from . import i18n, state

HELPER_NAME = "com.github.wl04.gpu_switcher.Helper"
HELPER_PATH = "/com/github/wl04/gpu_switcher/Helper"
HELPER_IFACE = "com.github.wl04.gpu_switcher.Helper"

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"

SESSION_MANAGER_NAME = "org.gnome.SessionManager"
SESSION_MANAGER_PATH = "/org/gnome/SessionManager"
SESSION_MANAGER_IFACE = "org.gnome.SessionManager"
# Logout mode: 1 = no confirmation interface. The app has already
# confirmed; mode 0 would show GNOME's own dialog again.
SESSION_MANAGER_LOGOUT_MODE = 1

# ---------------------------------------------------------------------------
# The D-Bus timeout budget constants (POLKIT_TIMEOUT_MS,
# UDEVDADM_STEP_TIMEOUT_S, SET_PRIMARY_STEP_COUNT, HELPER_TIMEOUT_MS) are
# defined in gpu_switcher/core/udev.py — the shared module the helper can
# import — so
# the client's budget is always derived from the helper's worst case, not
# from a duplicated literal.

LOGIN1_TIMEOUT_MS = 15000


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
    of out-args (possibly empty); on_error(GLib.Error) runs on a method-level
    error; on_bus_error(GLib.Error) runs when the bus connection itself cannot
    be acquired. The reply is delivered on the GLib main loop, so the UI never
    blocks while the interactive polkit dialog is open.
    """
    try:
        connection = Gio.bus_get_sync(bus_type)
    except GLib.Error as error:
        on_bus_error(error)
        return

    def on_call(c, task):
        try:
            on_success(c.call_finish(task))
        except GLib.Error as error:
            on_error(error)

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


def set_primary(pci: str, locale: str, callback) -> None:
    """Ask the helper to write the managed rule for this PCI address.

    The helper also refreshes udev (reload-rules + scoped DRM trigger)
    under the same authorization, so the rule takes effect without a
    second prompt. callback(ok, message, refresh_ok, timed_out) — timed_out
    is True when the client call timed out, in which case the helper's
    outcome is unknown. The helper's backup path is not surfaced to the UI.
    """
    helper_unreachable = i18n._("Could not reach the GPU helper: %s.")
    _call_async(
        Gio.BusType.SYSTEM,
        HELPER_NAME,
        HELPER_PATH,
        HELPER_IFACE,
        "SetPrimary",
        GLib.Variant("(ss)", (pci, locale)),
        "(bssb)",
        HELPER_TIMEOUT_MS,
        lambda r: callback(bool(r[0]), r[1], bool(r[3]), False),
        lambda e: callback(False, e.message, False, state.is_gio_timeout(e)),
        lambda e: callback(False, helper_unreachable % e.message, False, False),
    )


def set_auto(locale: str, callback) -> None:
    """Ask the helper to remove the managed rule (auto selection).

    The helper also refreshes udev under the same authorization, so the
    auto selection takes effect without a second prompt.
    callback(ok, message, refresh_ok, timed_out) — timed_out is True when
    the client call timed out, in which case the helper's outcome is
    unknown.
    """
    helper_unreachable = i18n._("Could not reach the GPU helper: %s.")
    _call_async(
        Gio.BusType.SYSTEM,
        HELPER_NAME,
        HELPER_PATH,
        HELPER_IFACE,
        "SetAuto",
        GLib.Variant("(s)", (locale,)),
        "(bsb)",
        HELPER_TIMEOUT_MS,
        lambda r: callback(bool(r[0]), r[1], bool(r[2]), False),
        lambda e: callback(False, e.message, False, state.is_gio_timeout(e)),
        lambda e: callback(False, helper_unreachable % e.message, False, False),
    )


def reboot(callback) -> None:
    """login1 Manager.Reboot(interactive) — standard polkit action, no
    custom policy. interactive=False: no interactive logind prompt.

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
        lambda e: callback(False, e.message),
        lambda e: callback(False, logind_unreachable % e.message),
    )


def _our_session_path() -> str:
    """Our logind session object path, or '' if it cannot be determined.

    XDG_SESSION_ID names the session, but the object path uses a
    separate internal id (/org/freedesktop/login1/session/_N), so the
    path must be resolved from logind's session list: the session of
    our uid that has a seat, or the one named by XDG_SESSION_ID when
    it is set.
    """
    wanted = os.environ.get("XDG_SESSION_ID", "")
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        result = connection.call_sync(
            LOGIN1_NAME,
            LOGIN1_PATH,
            LOGIN1_MANAGER_IFACE,
            "ListSessions",
            None,
            GLib.VariantType("(a(susso))"),
            0,
            LOGIN1_TIMEOUT_MS,
            None,
        )
    except GLib.Error:
        return ""
    uid = os.getuid()
    # Row: (session_id, uid, user, seat, object_path).
    rows = [
        (sid, seat, path)
        for sid, session_uid, _user, seat, path in result[0]
        if session_uid == uid
    ]
    if wanted.isdigit():
        return next((path for sid, _seat, path in rows if sid == wanted), "")
    seated = [path for _sid, seat, path in rows if seat]
    return seated[0] if len(seated) == 1 else ""


def session_leader() -> int | None:
    """The pid of the session leader of our logind session, or None when
    it cannot be determined.

    Resolves our session object path (see _our_session_path) and reads
    the session's Leader property (org.freedesktop.login1.Session.Leader,
    u) via Properties.Get. detect.py uses this to scope the gnome-shell
    journal attribution to our session.
    """
    path = _our_session_path()
    if not path:
        return None
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        result = connection.call_sync(
            LOGIN1_NAME,
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (LOGIN1_SESSION_IFACE, "Leader")),
            GLib.VariantType("(v)"),
            0,
            LOGIN1_TIMEOUT_MS,
            None,
        )
    except GLib.Error:
        return None
    try:
        return int(result[0])
    except (TypeError, ValueError):
        return None


def logout(callback) -> None:
    """GNOME session manager Logout for our own session (the fast path).

    Calls org.gnome.SessionManager.Logout(mode) on the session bus: the
    session manager performs the logout with its own handling (inhibitors
    are honored) instead of the forceful logind Session.Terminate. Mode 1:
    no confirmation dialog — the app already confirms. The session manager
    only exists inside a running GNOME session, so a failed call doubles
    as the non-GNOME guard.

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
        LOGIN1_TIMEOUT_MS,
        lambda r: callback(True, ""),
        lambda e: callback(False, logout_failed % e.message),
        lambda e: callback(False, logout_failed % e.message),
    )
