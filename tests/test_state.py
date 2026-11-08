"""Unit tests for the explicit operation-outcome state (state.py).

Covers the outcome mapping (timeout -> unknown, no change -> idle, success
with or without a refresh), G_IO_ERROR_TIMEOUT detection, the Log out
sensitivity rule and the banner style rule.
"""

import os
import unittest

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale.
os.environ["LC_ALL"] = "en"

from gpu_switcher.state import (
    BannerKind,
    Outcome,
    actions_enabled,
    banner_kind,
    is_gio_timeout,
    is_outcome_unknown,
    logout_enabled,
    outcome_from_result,
)


class OutcomeMapping(unittest.TestCase):
    def test_outcome_maps_timed_out_to_unknown(self) -> None:
        self.assertIs(outcome_from_result(True, True, True), Outcome.unknown)
        self.assertIs(outcome_from_result(False, False, True), Outcome.unknown)

    def test_outcome_maps_call_that_changed_nothing_to_idle(self) -> None:
        self.assertIs(outcome_from_result(False, False, False), Outcome.idle)

    def test_outcome_maps_success_to_refreshed_or_not(self) -> None:
        self.assertIs(outcome_from_result(True, True, False), Outcome.success_refreshed)
        self.assertIs(
            outcome_from_result(True, False, False), Outcome.success_refresh_failed
        )

    def test_is_gio_timeout_matches_public_enum_only(self) -> None:
        quark = Gio.io_error_quark()
        timeout = GLib.Error.new_literal(
            quark, "timed out", int(Gio.IOErrorEnum.TIMED_OUT)
        )
        io_error = GLib.Error.new_literal(
            quark, "io error", int(Gio.IOErrorEnum.FAILED)
        )
        not_symlink = GLib.Error.new_literal(
            quark, "x", int(Gio.IOErrorEnum.NOT_SYMBOLIC_LINK)
        )
        self.assertTrue(is_gio_timeout(timeout))
        self.assertFalse(is_gio_timeout(io_error))
        self.assertFalse(is_gio_timeout(not_symlink))

    def test_is_outcome_unknown_true_for_timeout_and_closed(self) -> None:
        quark = Gio.io_error_quark()
        for code in (Gio.IOErrorEnum.TIMED_OUT, Gio.IOErrorEnum.CLOSED):
            error = GLib.Error.new_literal(quark, "x", int(code))
            self.assertTrue(is_outcome_unknown(error))

    def test_is_outcome_unknown_true_for_dbus_no_reply(self) -> None:
        for name in ("NoReply", "ServiceUnknown", "Disconnected"):
            error = Gio.DBusError.new_for_dbus_error(
                f"org.freedesktop.DBus.Error.{name}", "gone"
            )
            self.assertTrue(is_outcome_unknown(error), name)

    def test_is_outcome_unknown_false_for_other_failures(self) -> None:
        quark = Gio.io_error_quark()
        no_bus = GLib.Error.new_literal(quark, "no bus", int(Gio.IOErrorEnum.FAILED))
        denied = Gio.DBusError.new_for_dbus_error(
            "org.freedesktop.DBus.Error.AccessDenied", "no"
        )
        self.assertFalse(is_outcome_unknown(no_bus))
        self.assertFalse(is_outcome_unknown(denied))


class LogoutSensitivity(unittest.TestCase):
    def test_logout_enabled_when_not_busy_and_success_refreshed(self) -> None:
        self.assertTrue(logout_enabled(False, Outcome.success_refreshed))

    def test_logout_disabled_when_busy(self) -> None:
        self.assertFalse(logout_enabled(True, Outcome.success_refreshed))

    def test_logout_disabled_when_success_refresh_failed(self) -> None:
        self.assertFalse(logout_enabled(False, Outcome.success_refresh_failed))

    def test_logout_disabled_when_outcome_unknown(self) -> None:
        self.assertFalse(logout_enabled(False, Outcome.unknown))

    def test_logout_disabled_when_idle(self) -> None:
        self.assertFalse(logout_enabled(False, Outcome.idle))


class ActionsEnabled(unittest.TestCase):
    """Outside a GNOME Wayland session the privileged actions must stay
    disabled (DESIGN.md: GNOME is the only supported session)."""

    def test_actions_enabled_only_in_gnome_wayland_session(self) -> None:
        self.assertTrue(actions_enabled(True))
        self.assertFalse(actions_enabled(False))


class BannerRule(unittest.TestCase):
    def test_banner_text_warns_reboot_required_when_refresh_failed(self) -> None:
        # warning style: the rule is on disk, a reboot is required
        # (the helper's refresh-failed message asserts the wording in
        # test_helper.SetPrimaryRefresh).
        self.assertEqual(
            banner_kind(Outcome.success_refresh_failed), BannerKind.WARNING
        )
        self.assertEqual(banner_kind(Outcome.success_refreshed), BannerKind.SUCCESS)


if __name__ == "__main__":
    unittest.main()
