"""Unit tests for the explicit operation-outcome state (state.py).

Covers the outcome mapping (timeout -> unknown, a call that changed
nothing -> idle, success with/without a successful refresh), the
G_IO_ERROR_TIMEOUT detection via quark+code, the logout sensitivity rule,
and the banner style rule for a completed Apply/Reset.
"""

import os
import unittest

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale.
os.environ["LC_ALL"] = "en"

from gpu_switcher.state import (
    Outcome,
    banner_kind,
    is_gio_timeout,
    logout_enabled,
    outcome_from_result,
)


class OutcomeMapping(unittest.TestCase):
    def test_outcome_maps_timed_out_to_unknown(self):
        self.assertIs(outcome_from_result(True, True, True), Outcome.unknown)
        self.assertIs(outcome_from_result(False, False, True), Outcome.unknown)

    def test_outcome_maps_call_that_changed_nothing_to_idle(self):
        self.assertIs(outcome_from_result(False, False, False), Outcome.idle)

    def test_outcome_maps_success_to_refreshed_or_not(self):
        self.assertIs(outcome_from_result(True, True, False), Outcome.success_refreshed)
        self.assertIs(
            outcome_from_result(True, False, False), Outcome.success_refresh_failed
        )

    def test_is_gio_timeout_matches_quark_and_code_7_not_code_5(self):
        # G_IO_ERROR_TIMEOUT is 7; 5 is G_IO_ERROR_IO.
        quark = GLib.quark_from_string("g-io-error-quark")
        timeout = GLib.Error.new_literal(quark, "timed out", 7)
        io_error = GLib.Error.new_literal(quark, "io error", 5)
        other = GLib.Error.new_literal(quark, "name has no owner", 8)
        self.assertTrue(is_gio_timeout(timeout))
        self.assertFalse(is_gio_timeout(io_error))
        self.assertFalse(is_gio_timeout(other))


class LogoutSensitivity(unittest.TestCase):
    def test_logout_enabled_when_not_busy_and_success_refreshed(self):
        self.assertTrue(logout_enabled(False, Outcome.success_refreshed))

    def test_logout_disabled_when_busy(self):
        self.assertFalse(logout_enabled(True, Outcome.success_refreshed))

    def test_logout_disabled_when_success_refresh_failed(self):
        self.assertFalse(logout_enabled(False, Outcome.success_refresh_failed))

    def test_logout_disabled_when_outcome_unknown(self):
        self.assertFalse(logout_enabled(False, Outcome.unknown))

    def test_logout_disabled_when_idle(self):
        self.assertFalse(logout_enabled(False, Outcome.idle))


class BannerRule(unittest.TestCase):
    def test_banner_text_warns_reboot_required_when_refresh_failed(self):
        # warning style: the rule is on disk, a reboot is required
        # (the helper's refresh-failed message asserts the wording in
        # test_helper.SetPrimaryRefresh).
        self.assertEqual(banner_kind(Outcome.success_refresh_failed), "warning")
        self.assertEqual(banner_kind(Outcome.success_refreshed), "success")


if __name__ == "__main__":
    unittest.main()
