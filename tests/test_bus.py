"""Unit tests for the app-side D-Bus clients (gpu_switcher/bus.py).

Mocks Gio.bus_get_sync and the returned connection: pins the session
manager logout call (session bus, destination, path, interface, method,
mode argument) and the localized failure forwarding when the session
bus is unavailable.
"""
import os
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale.
os.environ["LC_ALL"] = "en"

from gpu_switcher import bus


class Logout(unittest.TestCase):
    def test_Logout_CallsSessionManagerLogout_Mode1OnSessionBus(self):
        conn = mock.Mock()
        with mock.patch.object(Gio, "bus_get_sync",
                              return_value=conn) as mocked:
            bus.logout(lambda _ok, _message: None)
        # The assertion must use the captured mock: after the with-block
        # the patch is undone and Gio.bus_get_sync is the real function
        # again (a gi.FunctionInfo, which has no call_args).
        self.assertIs(mocked.call_args[0][0], Gio.BusType.SESSION)
        dest, path, iface, method, params = conn.call.call_args[0][:5]
        self.assertEqual(dest, "org.gnome.SessionManager")
        self.assertEqual(path, "/org/gnome/SessionManager")
        self.assertEqual(iface, "org.gnome.SessionManager")
        self.assertEqual(method, "Logout")
        self.assertEqual(params.unpack(), (1,))

    def test_Logout_ForwardsLocalizedError_WhenSessionBusUnavailable(self):
        results = []
        error = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "no session bus", 8)
        with mock.patch.object(Gio, "bus_get_sync", side_effect=error):
            bus.logout(lambda ok, message: results.append((ok, message)))
        self.assertEqual(results,
                         [(False, "Could not log out: no session bus.")])


if __name__ == "__main__":
    unittest.main()
