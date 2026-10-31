"""App-side D-Bus clients (M2).

The GUI talks to two system-bus services:

* the gpu-switcher helper — a root, D-Bus-activated service that performs
  the polkit-gated privileged work (writing / removing the managed udev
  rule);
* login1 (systemd-logind) — for Reboot (M2) and Log out (M3 fast path).

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

HELPER_NAME = "com.github.wl04.gpu_switcher.Helper"
HELPER_PATH = "/com/github/wl04/gpu_switcher/Helper"
HELPER_IFACE = "com.github.wl04.gpu_switcher.Helper"

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"

# The helper waits on an interactive polkit dialog, which may stay open for
# a while; allow longer than that so the app-side call cannot time out
# first.
HELPER_TIMEOUT_MS = 180000
LOGIN1_TIMEOUT_MS = 15000


def set_primary(pci: str, locale: str, callback) -> None:
    """Ask the helper to write the managed rule for this PCI address.

    callback(ok: bool, message: str, backup_path: str)
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(ss)", (pci, locale))

    def on_call(c, task):
        try:
            ok, message, backup = c.call_finish(task)
        except GLib.Error as error:
            callback(False, error.message, "")
            return
        callback(bool(ok), message, backup)

    connection.call(HELPER_NAME, HELPER_PATH, HELPER_IFACE, "SetPrimary",
                    params, GLib.VariantType("(bss)"), 0, HELPER_TIMEOUT_MS,
                    None, on_call)


def set_auto(locale: str, callback) -> None:
    """Ask the helper to remove the managed rule (auto selection).

    callback(ok: bool, message: str)
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(s)", (locale,))

    def on_call(c, task):
        try:
            ok, message = c.call_finish(task)
        except GLib.Error as error:
            callback(False, error.message)
            return
        callback(bool(ok), message)

    connection.call(HELPER_NAME, HELPER_PATH, HELPER_IFACE, "SetAuto",
                    params, GLib.VariantType("(bs)"), 0, HELPER_TIMEOUT_MS,
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


def logout(callback) -> None:
    """login1 Session.Logout(interactive) for our own session (M3 fast
    path). interactive=False: no interactive logind prompt.

    callback(ok: bool, message: str)
    """
    session_id = os.environ.get("XDG_SESSION_ID", "")
    if not session_id.isdigit():
        callback(False, "XDG_SESSION_ID is not set; cannot log out.")
        return
    session_path = "/org/freedesktop/login1/session%d" % int(session_id)
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    params = GLib.Variant("(b)", (False,))

    def on_call(c, task):
        try:
            c.call_finish(task)
            callback(True, "")
        except GLib.Error as error:
            callback(False, error.message)

    connection.call(LOGIN1_NAME, session_path, LOGIN1_SESSION_IFACE, "Logout",
                    params, GLib.VariantType("()"), 0, LOGIN1_TIMEOUT_MS,
                    None, on_call)
