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

import udev_rules
from gpu_switcher import bus


class TimeoutBudget(unittest.TestCase):
    def test_helper_timeout_exceeds_helper_worst_case(self):
        # Worst case: the interactive polkit dialog plus every udevadm
        # step (verify, reload, trigger, tag check) at the per-step
        # timeout; the client budget must exceed that so the helper's
        # result is never lost to a client-side timeout.
        worst_case_ms = (
            udev_rules.POLKIT_TIMEOUT_MS
            + udev_rules.SET_PRIMARY_STEP_COUNT
            * udev_rules.UDEVDADM_STEP_TIMEOUT_S
            * 1000
        )
        self.assertGreater(bus.HELPER_TIMEOUT_MS, worst_case_ms)


class Logout(unittest.TestCase):
    def test_logout_calls_session_manager_logout_mode_1_on_session_bus(self):
        conn = mock.Mock()
        with mock.patch.object(Gio, "bus_get_sync", return_value=conn) as mocked:
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

    def test_logout_forwards_localized_error_when_session_bus_unavailable(self):
        results = []
        error = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "no session bus", 8
        )
        with mock.patch.object(Gio, "bus_get_sync", side_effect=error):
            bus.logout(lambda ok, message: results.append((ok, message)))
        self.assertEqual(results, [(False, "Could not log out: no session bus.")])


class BusGuard(unittest.TestCase):
    """bus_get_sync failures must be forwarded to the completion callback
    as a localized error, not escape the action callback."""

    def _bus_error(self):
        return GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "no system bus", 8
        )

    def test_set_primary_forwards_bus_error_when_system_bus_unavailable(self):
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.set_primary("0000:12:00.0", "en", lambda *args: results.append(args))
        self.assertEqual(
            results,
            [
                (
                    False,
                    "Could not reach the GPU helper: no system bus.",
                    "",
                    False,
                    False,
                )
            ],
        )

    def test_set_auto_forwards_bus_error_when_system_bus_unavailable(self):
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.set_auto("en", lambda *args: results.append(args))
        self.assertEqual(
            results,
            [(False, "Could not reach the GPU helper: no system bus.", False, False)],
        )

    def test_reboot_forwards_bus_error_when_system_bus_unavailable(self):
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.reboot(lambda *args: results.append(args))
        self.assertEqual(results, [(False, "Could not reach logind: no system bus.")])


if __name__ == "__main__":
    unittest.main()
