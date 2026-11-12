"""Unit tests for the app-side D-Bus clients (gpu_switcher/bus.py).

Mocks Gio.bus_get_sync and the connection. Pins the session manager logout
call, the call dispatch (success vs. error handlers, remote-error prefix
stripping), which failed calls leave the helper outcome unknown, the timeout
budget, the localized failure message when the bus is unavailable, and the
interface contract between this app and the helper.
"""

import os
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# i18n binds gettext at import time: pin the locale before importing the app
# so English strings are asserted under any developer locale (LANGUAGE would
# override LC_ALL, so it is cleared).
os.environ["LC_ALL"] = "en"
os.environ.pop("LANGUAGE", None)

import gpu_switcher.core.udev as udev_rules
from gpu_switcher import bus
from gpu_switcher.helper import service


def _failed_helper_call(start: Any, error: GLib.Error) -> tuple[Any, ...]:
    """Run a helper call over a mocked connection whose reply is this error,
    and return the arguments the completion callback received.

    `start(callback)` begins the call (bus.set_primary or bus.set_auto); the
    ready callback the call registered on the connection is then invoked by
    hand, the way the GLib main loop would invoke it.
    """
    conn = mock.Mock()
    conn.call_finish.side_effect = error
    results: list[tuple[Any, ...]] = []

    with mock.patch.object(Gio, "bus_get_sync", return_value=conn):
        start(lambda *args: results.append(args))
        conn.call.call_args[0][9](conn, None)

    return results[0]


def _set_primary_failed(error: GLib.Error) -> tuple[Any, ...]:
    return _failed_helper_call(
        lambda cb: bus.set_primary("0000:12:00.0", "en", cb), error
    )


def _set_auto_failed(error: GLib.Error) -> tuple[Any, ...]:
    return _failed_helper_call(lambda cb: bus.set_auto("en", cb), error)


class TimeoutBudget(unittest.TestCase):
    def test_helper_timeout_exceeds_helper_worst_case(self) -> None:
        # Worst case: the interactive polkit dialog plus every udevadm
        # step (verify, reload, trigger, tag check) at the per-step
        # timeout; the client budget must exceed that so the helper's
        # result is never lost to a client-side timeout.
        worst_case_ms = (
            udev_rules.POLKIT_TIMEOUT_MS
            + udev_rules.SET_PRIMARY_STEP_COUNT
            * udev_rules.UDEVADM_STEP_TIMEOUT_S
            * 1000
        )
        self.assertGreater(bus.HELPER_TIMEOUT_MS, worst_case_ms)


class Logout(unittest.TestCase):
    def test_logout_calls_session_manager_logout_mode_1_on_session_bus(self) -> None:
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

    def test_logout_forwards_localized_error_when_session_bus_unavailable(self) -> None:
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

    def _bus_error(self) -> GLib.Error:
        return GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "no system bus", 8
        )

    def test_set_primary_forwards_bus_error_when_system_bus_unavailable(self) -> None:
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.set_primary("0000:12:00.0", "en", lambda *args: results.append(args))
        self.assertEqual(
            results,
            [
                (
                    False,
                    "Could not reach the GPU helper: no system bus.",
                    False,
                    False,
                )
            ],
        )

    def test_set_auto_forwards_bus_error_when_system_bus_unavailable(self) -> None:
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.set_auto("en", lambda *args: results.append(args))
        self.assertEqual(
            results,
            [(False, "Could not reach the GPU helper: no system bus.", False, False)],
        )

    def test_reboot_forwards_bus_error_when_system_bus_unavailable(self) -> None:
        results = []
        with mock.patch.object(Gio, "bus_get_sync", side_effect=self._bus_error()):
            bus.reboot(lambda *args: results.append(args))
        self.assertEqual(results, [(False, "Could not reach logind: no system bus.")])


if __name__ == "__main__":
    unittest.main()


class CallDispatch(unittest.TestCase):
    def _call(
        self,
        finish_result: object,
        on_success: Callable[[GLib.Variant], None],
        on_error: Callable[[GLib.Error, str], None],
    ) -> None:
        conn = mock.Mock()
        conn.call_finish.side_effect = (
            finish_result if isinstance(finish_result, Exception) else None
        )
        if not isinstance(finish_result, Exception):
            conn.call_finish.return_value = finish_result
        with mock.patch.object(Gio, "bus_get_sync", return_value=conn):
            bus._call_async(
                Gio.BusType.SYSTEM,
                "n",
                "/p",
                "i",
                "M",
                GLib.Variant("()", ()),
                "()",
                1000,
                on_success,
                on_error,
                mock.Mock(),
            )
        callback = conn.call.call_args[0][9]
        callback(conn, None)

    def test_call_async_does_not_report_error_when_success_handler_raises_glib_error(
        self,
    ) -> None:
        on_error = mock.Mock()

        def failing_success(_reply: object) -> None:
            raise GLib.Error.new_literal(GLib.quark_from_string("x"), "late", 1)

        with self.assertRaises(GLib.Error):
            self._call((), failing_success, on_error)

        on_error.assert_not_called()

    def test_call_async_strips_prefix_but_keeps_error_classifiable(self) -> None:
        on_error = mock.Mock()
        remote = Gio.DBusError.new_for_dbus_error(
            "com.github.wl04.gpu_switcher.Error.Busy",
            "Another operation is in progress.",
        )

        self._call(remote, mock.Mock(), on_error)

        error, message = on_error.call_args[0]
        self.assertEqual(message, "Another operation is in progress.")
        self.assertTrue(Gio.DBusError.is_remote_error(error))


class OutcomeUnknownAfterCall(unittest.TestCase):
    """Which failed helper calls leave the outcome unknown.

    A helper that was killed, restarted or lost mid-call may already have
    committed the rule: the app must keep the unknown-outcome warning and
    re-read the state instead of showing a plain failure. An explicit
    rejection answered by the helper is the other case: nothing changed.
    """

    UNKNOWN_ERROR_NAMES = (
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.NoServer",
    )

    def test_set_primary_reports_unknown_outcome_for_every_lost_helper_error(
        self,
    ) -> None:
        for name in self.UNKNOWN_ERROR_NAMES:
            with self.subTest(error=name):
                _ok, _message, _refresh_ok, timed_out = _set_primary_failed(
                    Gio.DBusError.new_for_dbus_error(name, "The helper went away.")
                )

                self.assertTrue(timed_out)

    def test_set_primary_reports_unknown_outcome_as_a_full_result(self) -> None:
        # The callback shape the app relies on: failed, the app's own sentence
        # for a helper that vanished, no refresh, outcome unknown.
        result = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "org.freedesktop.DBus.Error.NoReply", "Did not receive a reply"
            )
        )

        self.assertEqual(
            result,
            (False, "The GPU helper stopped answering.", False, True),
        )

    def test_set_auto_reports_unknown_outcome_for_lost_helper_error(self) -> None:
        results = _set_auto_failed(
            Gio.DBusError.new_for_dbus_error(
                "org.freedesktop.DBus.Error.Disconnected", "Connection closed"
            )
        )

        self.assertTrue(results[3])

    def test_set_primary_reports_known_outcome_for_helper_rejection(self) -> None:
        # The helper answered: it refused the call, so nothing changed.
        _ok, message, _refresh_ok, timed_out = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "com.github.wl04.gpu_switcher.Error.Busy",
                "Another operation is in progress.",
            )
        )

        self.assertFalse(timed_out)
        self.assertEqual(message, "Another operation is in progress.")

    def test_set_primary_reports_known_outcome_for_failed_authorization(self) -> None:
        _ok, _message, _refresh_ok, timed_out = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "org.freedesktop.PolicyKit1.Error.Failed",
                "The authorization check failed.",
            )
        )

        self.assertFalse(timed_out)


class HelperFailureText(unittest.TestCase):
    """What the user is told when a helper call fails.

    Failures the bus answers for us are English D-Bus text, so the app says
    them in the user's language. A rejection the helper composed in the
    requested locale already explains itself and must reach the banner
    unchanged.
    """

    def test_set_primary_names_the_missing_helper_when_bus_has_no_such_service(
        self,
    ) -> None:
        _ok, message, _refresh_ok, _timed_out = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "The name com.github.wl04.gpu_switcher.Helper was not provided "
                "by any .service files",
            )
        )

        self.assertEqual(
            message,
            "The GPU helper is not available. Install the gpu-switcher package.",
        )

    def test_set_primary_names_its_own_timeout_when_helper_never_replied(self) -> None:
        _ok, message, _refresh_ok, _timed_out = _set_primary_failed(
            GLib.Error.new_literal(
                Gio.io_error_quark(),
                "Timeout was reached",
                int(Gio.IOErrorEnum.TIMED_OUT),
            )
        )

        self.assertEqual(message, "The GPU helper did not answer in time.")

    def test_set_primary_names_a_helper_that_stopped_answering(self) -> None:
        _ok, message, _refresh_ok, _timed_out = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "org.freedesktop.DBus.Error.NoReply", "Did not receive a reply"
            )
        )

        self.assertEqual(message, "The GPU helper stopped answering.")

    def test_set_auto_reports_the_same_helper_failure_texts(self) -> None:
        cases = (
            (
                Gio.DBusError.new_for_dbus_error(
                    "org.freedesktop.DBus.Error.ServiceUnknown", "no such service"
                ),
                "The GPU helper is not available. Install the gpu-switcher package.",
            ),
            (
                Gio.DBusError.new_for_dbus_error(
                    "org.freedesktop.DBus.Error.NoServer", "Failed to connect"
                ),
                "The GPU helper stopped answering.",
            ),
        )
        for error, expected in cases:
            with self.subTest(error=error.message):
                self.assertEqual(_set_auto_failed(error)[1], expected)

    def test_set_primary_keeps_the_helper_own_rejection_message(self) -> None:
        _ok, message, _refresh_ok, _timed_out = _set_primary_failed(
            Gio.DBusError.new_for_dbus_error(
                "com.github.wl04.gpu_switcher.Error.Busy",
                "Another operation is in progress.",
            )
        )

        self.assertEqual(message, "Another operation is in progress.")


class HelperInterfaceContract(unittest.TestCase):
    """The helper publishes its interface from INTROSPECTION_XML; this app
    carries its own copy of those names and signatures. Both sides of that seam
    are pinned here because a mismatch is not a crash to fix — it is a D-Bus
    error in front of a user who was promised a GPU switch.
    """

    # What the app calls, as it calls it. Whole dicts: adding a helper method
    # means adding it here.
    _CALLED = {
        bus.HELPER_SET_PRIMARY: (bus.HELPER_SET_PRIMARY_IN, bus.HELPER_SET_PRIMARY_OUT),
        bus.HELPER_SET_AUTO: (bus.HELPER_SET_AUTO_IN, bus.HELPER_SET_AUTO_OUT),
    }

    @staticmethod
    def _attribute(element: ET.Element, name: str) -> str:
        """A required XML attribute, or a failure that says which one is gone."""
        value = element.get(name)
        if value is None:
            raise AssertionError(f"<{element.tag}> has no {name!r} attribute")
        return value

    def _published(self) -> dict[str, tuple[str, str]]:
        """method name -> (in signature, out signature) from the helper's XML.

        Each signature is wrapped in parentheses, the way a D-Bus signature is
        written, so it compares directly with what the app hands to GLib.Variant.
        """
        root = ET.fromstring(service.INTROSPECTION_XML)
        published: dict[str, tuple[str, str]] = {}
        for method in root.iter("method"):
            args = method.findall("arg")
            in_types = "".join(
                self._attribute(arg, "type")
                for arg in args
                if self._attribute(arg, "direction") == "in"
            )
            out_types = "".join(
                self._attribute(arg, "type")
                for arg in args
                if self._attribute(arg, "direction") == "out"
            )
            published[self._attribute(method, "name")] = (
                f"({in_types})",
                f"({out_types})",
            )
        return published

    def test_helper_interface_matches_the_names_and_signatures_the_app_calls(
        self,
    ) -> None:
        self.assertEqual(self._published(), self._CALLED)

    def test_helper_bus_name_object_path_and_interface_match_the_app(self) -> None:
        # Renaming on one side only is enough to make the helper unreachable,
        # and the failure looks like a missing package.
        root = ET.fromstring(service.INTROSPECTION_XML)
        self.assertEqual(self._attribute(root, "name"), bus.HELPER_PATH)
        interface = root.find("interface")
        if interface is None:
            raise AssertionError("introspection XML has no <interface> element")
        self.assertEqual(self._attribute(interface, "name"), bus.HELPER_IFACE)
        self.assertEqual(service.SERVICE_NAME, bus.HELPER_NAME)

    def test_helper_introspection_xml_is_accepted_by_gio(self) -> None:
        # ElementTree tolerates what Gio refuses: a mistyped attribute or an
        # argument without a type parses fine here and leaves the helper unable
        # to register its object on the bus.
        node = Gio.DBusNodeInfo.new_for_xml(service.INTROSPECTION_XML)
        self.assertIsNotNone(node)
        self.assertEqual(len(node.interfaces), 1)
        self.assertEqual(
            sorted(method.name for method in node.interfaces[0].methods),
            sorted(self._CALLED),
        )
