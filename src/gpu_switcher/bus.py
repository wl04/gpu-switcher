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

from . import i18n

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

# The helper waits on an interactive polkit dialog, which may stay open for
# a while; allow longer than that so the app-side call cannot time out
# first.
HELPER_TIMEOUT_MS = 180000
LOGIN1_TIMEOUT_MS = 15000


def set_primary(pci: str, locale: str, callback) -> None:
    """Ask the helper to write the managed rule for this PCI address.

    The helper also refreshes udev (reload-rules + scoped DRM trigger)
    under the same authorization, so the rule takes effect without a
    second prompt. callback(ok, message, backup_path, refresh_ok).
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(ss)", (pci, locale))

    def on_call(c, task):
        try:
            ok, message, backup, refresh_ok = c.call_finish(task)
        except GLib.Error as error:
            callback(False, error.message, "", False)
            return
        callback(bool(ok), message, backup, bool(refresh_ok))

    connection.call(HELPER_NAME, HELPER_PATH, HELPER_IFACE, "SetPrimary",
                    params, GLib.VariantType("(bssb)"), 0, HELPER_TIMEOUT_MS,
                    None, on_call)


def set_auto(locale: str, callback) -> None:
    """Ask the helper to remove the managed rule (auto selection).

    The helper also refreshes udev under the same authorization, so the
    auto selection takes effect without a second prompt.
    callback(ok, message, refresh_ok).
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(s)", (locale,))

    def on_call(c, task):
        try:
            ok, message, refresh_ok = c.call_finish(task)
        except GLib.Error as error:
            callback(False, error.message, False)
            return
        callback(bool(ok), message, bool(refresh_ok))

    connection.call(HELPER_NAME, HELPER_PATH, HELPER_IFACE, "SetAuto",
                    params, GLib.VariantType("(bsb)"), 0, HELPER_TIMEOUT_MS,
                    None, on_call)


def reboot(callback) -> None:
    """login1 Manager.Reboot(interactive) — standard polkit action, no
    custom policy. interactive=False: no interactive logind prompt.

    callback(ok: bool, message: str)
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(b)", (False,))

    def on_call(c, task):
        try:
            c.call_finish(task)
            callback(True, "")
        except GLib.Error as error:
            callback(False, error.message)

    connection.call(LOGIN1_NAME, LOGIN1_PATH, LOGIN1_MANAGER_IFACE, "Reboot",
                    params, GLib.VariantType("()"), 0, LOGIN1_TIMEOUT_MS,
                    None, on_call)


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
            LOGIN1_NAME, LOGIN1_PATH, LOGIN1_MANAGER_IFACE, "ListSessions",
            None, GLib.VariantType("(a(susso))"), 0, LOGIN1_TIMEOUT_MS, None)
    except GLib.Error:
        return ""
    uid = os.getuid()
    # Row: (session_id, uid, user, seat, object_path).
    rows = [(sid, seat, path) for sid, session_uid, _user, seat, path
            in result[0] if session_uid == uid]
    if wanted.isdigit():
        return next((path for sid, _seat, path in rows if sid == wanted), "")
    seated = [path for _sid, seat, path in rows if seat]
    return seated[0] if len(seated) == 1 else ""


def session_leader():
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
            LOGIN1_NAME, path, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (LOGIN1_SESSION_IFACE, "Leader")),
            GLib.VariantType("(v)"), 0, LOGIN1_TIMEOUT_MS, None)
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
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION)
    except GLib.Error as error:
        callback(False, i18n._("Could not log out: %s.") % error.message)
        return
    params = GLib.Variant("(u)", (SESSION_MANAGER_LOGOUT_MODE,))

    def on_call(c, task):
        try:
            c.call_finish(task)
            callback(True, "")
        except GLib.Error as error:
            callback(False, i18n._("Could not log out: %s.") % error.message)

    connection.call(SESSION_MANAGER_NAME, SESSION_MANAGER_PATH,
                    SESSION_MANAGER_IFACE, "Logout",
                    params, GLib.VariantType("()"), 0, LOGIN1_TIMEOUT_MS,
                    None, on_call)
