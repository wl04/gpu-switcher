"""Helper resilience: the in-progress lock and the idle timer must stay
correct when an operation is slow or fails unexpectedly."""

import unittest
from typing import Any
from unittest import mock

from gpu_switcher.core import udev as udev_rules
from gpu_switcher.helper import auth, rule_store, service
from gpu_switcher.helper import udev as hud
from gpu_switcher.helper.auth import AuthStatus


def _invocation() -> Any:
    return mock.Mock(spec_set=["return_value", "return_dbus_error", "get_message"])


def _authorize_immediately(
    _connection: Any,
    _sender: Any,
    _locale: Any,
    on_done: Any,
    _cancellable: Any = None,
) -> None:
    on_done(AuthStatus.AUTHORIZED, "")


def _never_authorize(
    _connection: Any,
    _sender: Any,
    _locale: Any,
    _on_done: Any,
    _cancellable: Any = None,
) -> None:
    pass


class IdleTimeout(unittest.TestCase):
    def setUp(self) -> None:
        service._operation_in_progress = False

    def tearDown(self) -> None:
        service._operation_in_progress = False

    def test_idle_timeout_keeps_loop_running_while_operation_in_progress(self) -> None:
        service._operation_in_progress = True
        loop = mock.Mock()

        with mock.patch.object(service, "_LOOP", loop):
            keep_ticking = service._on_idle_timeout()

        self.assertTrue(keep_ticking)
        loop.quit.assert_not_called()

    def test_idle_timeout_quits_loop_when_no_operation_in_progress(self) -> None:
        loop = mock.Mock()

        with mock.patch.object(service, "_LOOP", loop):
            keep_ticking = service._on_idle_timeout()

        self.assertFalse(keep_ticking)
        loop.quit.assert_called_once_with()


class LockRelease(unittest.TestCase):
    def setUp(self) -> None:
        service._operation_in_progress = False

    def tearDown(self) -> None:
        service._operation_in_progress = False

    def _call_set_primary(self, invocation: Any, params: Any) -> None:
        service.handle_method_call(
            mock.Mock(),
            "sender",
            service.OBJECT_PATH,
            service.INTERFACE,
            "SetPrimary",
            params,
            invocation,
        )

    def test_set_primary_releases_lock_when_planning_raises_unexpectedly(self) -> None:
        invocation = _invocation()

        with mock.patch.object(
            service, "rule_conflicts", side_effect=RuntimeError("boom")
        ):
            self._call_set_primary(invocation, ("0000:12:00.0", "en"))

        self.assertFalse(service._operation_in_progress)
        self.assertEqual(
            invocation.return_dbus_error.call_args[0][0],
            udev_rules.DBUS_ERROR_INTERNAL,
        )

    def test_set_primary_replies_internal_error_when_authorized_step_raises(
        self,
    ) -> None:
        invocation = _invocation()

        with (
            mock.patch.object(auth, "authorize", _authorize_immediately),
            mock.patch.object(service, "rule_conflicts", return_value=[]),
            mock.patch.object(
                service, "resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(
                rule_store, "write_rule_atomically", side_effect=OSError("disk")
            ),
        ):
            self._call_set_primary(invocation, ("0000:12:00.0", "en"))

        self.assertFalse(service._operation_in_progress)
        self.assertEqual(
            invocation.return_dbus_error.call_args[0][0],
            udev_rules.DBUS_ERROR_INTERNAL,
        )
        invocation.return_value.assert_not_called()

    def test_set_primary_releases_lock_when_udev_callback_raises(self) -> None:
        invocation = _invocation()

        with (
            mock.patch.object(auth, "authorize", _authorize_immediately),
            mock.patch.object(service, "rule_conflicts", return_value=[]),
            mock.patch.object(
                service, "resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(
                rule_store,
                "write_rule_atomically",
                side_effect=lambda *a, **k: hud.report_callback_error("boom"),
            ),
        ):
            self._call_set_primary(invocation, ("0000:12:00.0", "en"))

        self.assertFalse(service._operation_in_progress)
        self.assertEqual(
            invocation.return_dbus_error.call_args[0][0],
            udev_rules.DBUS_ERROR_INTERNAL,
        )

    def test_set_primary_keeps_other_calls_lock_when_params_are_malformed(
        self,
    ) -> None:
        with (
            mock.patch.object(auth, "authorize", _never_authorize),
            mock.patch.object(service, "rule_conflicts", return_value=[]),
            mock.patch.object(
                service, "resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
        ):
            first = _invocation()
            self._call_set_primary(first, ("0000:12:00.0", "en"))
            second = _invocation()
            self._call_set_primary(second, ("only-one",))

        self.assertTrue(service._operation_in_progress)
        self.assertEqual(
            second.return_dbus_error.call_args[0][0], udev_rules.DBUS_ERROR_INTERNAL
        )
