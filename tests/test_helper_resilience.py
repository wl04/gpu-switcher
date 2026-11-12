"""Helper resilience: the in-progress lock and the idle timer must stay
correct when an operation is slow or fails unexpectedly."""

import os
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

from gi.repository import GLib

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

        with mock.patch.object(service, "scan_rules", side_effect=RuntimeError("boom")):
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
            mock.patch.object(
                service, "scan_rules", return_value=udev_rules.RuleScan()
            ),
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

    def test_set_primary_rechecks_gpu_after_authorization(self) -> None:
        invocation = _invocation()
        gpu: dict[str, Any] = {"value": ("card1", "0x1002", "0x7550")}

        def authorize_after_gpu_removed(
            _c: Any, _s: Any, _l: Any, on_done: Any, _x: Any = None
        ) -> None:
            gpu["value"] = None  # hot-unplugged while the prompt was open
            on_done(AuthStatus.AUTHORIZED, "")

        with (
            mock.patch.object(auth, "authorize", authorize_after_gpu_removed),
            mock.patch.object(
                service, "scan_rules", return_value=udev_rules.RuleScan()
            ),
            mock.patch.object(
                service, "resolve_gpu", side_effect=lambda _pci: gpu["value"]
            ),
            mock.patch.object(rule_store, "write_rule_atomically") as write,
        ):
            self._call_set_primary(invocation, ("0000:12:00.0", "en"))

        write.assert_not_called()
        ok, message, _backup, _refresh = invocation.return_value.call_args[0][
            0
        ].unpack()
        self.assertFalse(ok)
        self.assertIn("not a valid display controller", message)
        self.assertFalse(service._operation_in_progress)

    def test_set_primary_releases_lock_when_udev_callback_raises(self) -> None:
        invocation = _invocation()

        with (
            mock.patch.object(auth, "authorize", _authorize_immediately),
            mock.patch.object(
                service, "scan_rules", return_value=udev_rules.RuleScan()
            ),
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
            mock.patch.object(
                service, "scan_rules", return_value=udev_rules.RuleScan()
            ),
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


class StepTimeout(unittest.TestCase):
    def _start_step(self, outer: Any = None) -> tuple[Any, Any]:
        proc = mock.Mock()
        with mock.patch.object(hud.Gio.Subprocess, "new", return_value=proc):
            hud.run_udev_async(["/usr/bin/udevadm", "x"], mock.Mock(), outer)
        step_cancellable = proc.communicate_async.call_args[0][1]
        return proc, step_cancellable

    def test_run_udev_async_kills_child_when_step_times_out(self) -> None:
        proc, step_cancellable = self._start_step()

        step_cancellable.cancel()

        proc.force_exit.assert_called_once_with()

    def test_run_udev_async_cancels_step_when_operation_is_cancelled(self) -> None:
        outer = hud.Gio.Cancellable.new()
        proc, step_cancellable = self._start_step(outer)

        outer.cancel()

        self.assertTrue(step_cancellable.is_cancelled())
        proc.force_exit.assert_called_once_with()


class RealChildTimeout(unittest.TestCase):
    def test_run_udev_async_child_cannot_act_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "marker")
            code = (
                "import time, pathlib; time.sleep(1.0); "
                f"pathlib.Path({marker!r}).write_text('late')"
            )
            loop = GLib.MainLoop()
            results: list[tuple[int, str]] = []

            def on_done(rc: int, _out: str, err: str) -> None:
                results.append((rc, err))
                loop.quit()

            with (
                mock.patch.object(hud, "UDEVADM_STEP_TIMEOUT_S", 0.2),
                mock.patch.object(hud.sys, "stderr", mock.Mock()),
            ):
                hud.run_udev_async([sys.executable, "-c", code], on_done)
                GLib.timeout_add(5000, loop.quit)
                loop.run()
            time.sleep(1.5)

            self.assertEqual(len(results), 1)
            self.assertNotEqual(results[0][0], 0)
            self.assertFalse(os.path.exists(marker))


class RuleWriteFailure(unittest.TestCase):
    def test_write_rule_atomically_reports_nothing_changed_when_temp_creation_fails(
        self,
    ) -> None:
        on_done = mock.Mock()

        with (
            mock.patch.object(
                rule_store.tempfile, "mkstemp", side_effect=OSError("disk full")
            ),
            mock.patch.object(rule_store.sys, "stderr", mock.Mock()),
        ):
            rule_store.write_rule_atomically(
                "0000:12:00.0", "x", "card0", lambda m: m, on_done
            )

        on_done.assert_called_once()
        self.assertFalse(on_done.call_args[0][0])
        self.assertIn("nothing changed", on_done.call_args[0][1])


class RuleRemoval(unittest.TestCase):
    def test_unlink_managed_rule_fsyncs_directory_after_removal(self) -> None:
        calls: list[str] = []

        with (
            mock.patch.object(
                rule_store.os, "unlink", side_effect=lambda _p: calls.append("unlink")
            ),
            mock.patch.object(
                rule_store, "fsync_dir", side_effect=lambda _p: calls.append("fsync")
            ),
        ):
            rule_store.unlink_managed_rule()

        self.assertEqual(calls, ["unlink", "fsync"])
