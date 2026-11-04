"""Unit tests for the root D-Bus helper (helper/gpu_switcher_helper.py).

Covers the pure PCI-format validation, the /sys resolution failure paths
(stubbed filesystem), the managed rule line format (golden string matching
the reference script mutter-primary-gpu.sh), the polkit authorization
(subject binding, cancellation id, result statuses — mocked D-Bus), the
distinct authorization-failure messages of SetPrimary, the SetPrimary/SetAuto
transaction failure paths (verify, backup, rename, unlink) and the
idempotent no-write path, the missing-catalog translator fallback, and the
IPC contract agreement between the app side (gpu_switcher/bus.py), the
helper side and the INTROSPECTION_XML method signatures.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

import helper.gpu_switcher_helper as h
from gpu_switcher import bus


class IsValidPciFormat(unittest.TestCase):
    def test_is_valid_pci_format_accepts_full_pci_address(self):
        self.assertTrue(h.is_valid_pci_format("0000:12:00.0"))

    def test_is_valid_pci_format_rejects_domain_omitted_address(self):
        self.assertFalse(h.is_valid_pci_format("12:00.0"))

    def test_is_valid_pci_format_rejects_non_hex_address(self):
        self.assertFalse(h.is_valid_pci_format("0000:GG:00.0"))


class RuleText(unittest.TestCase):
    def test_rule_text_produces_reference_rule_line_for_valid_pci(self):
        self.assertEqual(
            h._rule_text("0000:12:00.0", "0x1002", "0x7550"),
            "# Managed by gpu-switcher; selected PCI 0000:12:00.0\n"
            "# Mutter uses TAG; ENV is retained for compatibility "
            "with the previous local rule.\n"
            'SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", '
            'KERNEL=="card[0-9]*", KERNEL!="card*-*", '
            'SUBSYSTEMS=="pci", KERNELS=="0000:12:00.0", '
            'ATTRS{vendor}=="0x1002", ATTRS{device}=="0x7550", '
            'ENV{MUTTER_DEVICE_PREFERRED_PRIMARY}="1", '
            'TAG+="mutter-device-preferred-primary"\n',
        )


class ResolveGpu(unittest.TestCase):
    def test_resolve_gpu_returns_none_for_malformed_pci(self):
        self.assertIsNone(h._resolve_gpu("12:00.0"))
        self.assertIsNone(h._resolve_gpu("0000:12:00"))
        self.assertIsNone(h._resolve_gpu("not-a-pci"))

    def test_resolve_gpu_returns_none_when_pci_not_a_display_controller(self):
        with (
            mock.patch("os.path.isdir", return_value=True),
            mock.patch("helper.gpu_switcher_helper.read_text", return_value="0x080000"),
        ):
            self.assertIsNone(h._resolve_gpu("0000:12:00.0"))

    def test_resolve_gpu_accepts_3d_controller_class(self):
        with (
            mock.patch("os.path.isdir", return_value=True),
            mock.patch("os.path.islink", return_value=True),
            mock.patch("os.path.realpath", return_value="/dev/pci/0000:12:00.0"),
            mock.patch("os.listdir", return_value=["card1"]),
            mock.patch("os.path.exists", return_value=True),
            mock.patch(
                "helper.gpu_switcher_helper.read_text",
                side_effect=lambda path: (
                    "0x030200"
                    if path.endswith("/class")
                    else "0x1002"
                    if path.endswith("/vendor")
                    else "0x7550"
                    if path.endswith("/device")
                    else ""
                ),
            ),
        ):
            self.assertEqual(
                h._resolve_gpu("0000:12:00.0"), ("card1", "0x1002", "0x7550")
            )


class Translator(unittest.TestCase):
    def test_translator_returns_identity_when_catalog_missing(self):
        with mock.patch(
            "helper.gpu_switcher_helper.LOCALE_BASE", "/nonexistent-gpu-switcher-locale"
        ):
            tr = h._translator("ru")
        self.assertEqual(tr("No catalog here"), "No catalog here")


class NormalizeLocale(unittest.TestCase):
    """The locale arrives from an unauthenticated D-Bus caller, so it is
    mapped to the allowlisted catalog languages before gettext sees it.
    Path-shaped values would let gettext open (or block on) files outside
    LOCALE_BASE."""

    def test_normalize_locale_keeps_supported_languages(self):
        self.assertEqual(h._normalize_locale("en"), "en")
        self.assertEqual(h._normalize_locale("ru"), "ru")
        self.assertEqual(h._normalize_locale("ru_RU.UTF-8"), "ru")
        self.assertEqual(h._normalize_locale("en_US"), "en")

    def test_normalize_locale_falls_back_to_english_for_unsupported_or_path_like(self):
        for locale in (
            "fr",
            "",
            None,
            "ru/ru",
            "..",
            "/tmp/evil/LC_MESSAGES/gpu-switcher.mo",
            "C:\\evil",
            "x" * 33,
        ):
            self.assertEqual(h._normalize_locale(locale), "en", locale)


class TranslatorLocale(unittest.TestCase):
    def test_translator_passes_only_allowlisted_languages_to_gettext(self):
        """Contract: only allowlisted language identifiers reach
        gettext.translation, so catalog loading cannot leave LOCALE_BASE.
        """
        fake = mock.Mock(gettext=lambda s: s)
        with mock.patch(
            "helper.gpu_switcher_helper.gettext.translation", return_value=fake
        ) as translation:
            for locale in (
                "ru",
                "ru_RU.UTF-8",
                "fr",
                "/tmp/evil/LC_MESSAGES/gpu-switcher.mo",
                "..",
            ):
                h._translator(locale)
        for call in translation.call_args_list:
            for lang in call.kwargs["languages"]:
                self.assertIn(lang, ("en", "ru"), lang)


class LogBounded(unittest.TestCase):
    """Every log line is bounded and flattened before it reaches the file:
    caller-supplied strings (pci, locale) arrive unauthenticated, so a
    single call must not grow the root-owned log without bound or forge
    entries."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpu-switcher-log-")
        self.log_path = os.path.join(self.tmp, "helper.log")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_log_truncates_overlong_caller_text(self):
        with mock.patch.object(h, "LOG_PATH", self.log_path):
            h._log("pci=" + "a" * 5000 + " locale=x")
        with open(self.log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        # prefix "gpu-switcher-helper " + UTC timestamp + one space,
        # then the capped message (cap plus the truncation marker).
        self.assertLessEqual(
            len(lines[0]),
            len("gpu-switcher-helper ")
            + 20
            + 1
            + h._LOG_MAX_CHARS
            + len("…[truncated]"),
        )
        self.assertTrue(lines[0].endswith("…[truncated]"))

    def test_log_flattens_newlines_and_drops_control_characters(self):
        """An embedded newline must not forge a second log entry."""
        with mock.patch.object(h, "LOG_PATH", self.log_path):
            h._log("pci=x\nlocale=y\x01")
        with open(self.log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)  # no forged second entry
        self.assertIn("\\n", lines[0])
        self.assertNotIn("\x01", lines[0])
        self.assertTrue(lines[0].endswith("pci=x\\nlocale=y"))


class MethodCallRateLimit(unittest.TestCase):
    """A client can invoke the helper without authorization; per-call
    logging is rate-limited per (sender, method) window."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpu-switcher-log-")
        self.log_path = os.path.join(self.tmp, "helper.log")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_method_call_allowed_suppresses_beyond_limit_once_per_window(self):
        with (
            mock.patch.object(h, "_method_call_counts", {}),
            mock.patch.object(h, "LOG_PATH", self.log_path),
        ):
            for _ in range(h._METHOD_CALL_LIMIT):
                self.assertTrue(h._method_call_allowed(":1.1", "Status"))
            for _ in range(3):
                self.assertFalse(h._method_call_allowed(":1.1", "Status"))
            # A different sender keeps its own budget.
            self.assertTrue(h._method_call_allowed(":1.2", "Status"))
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content.count("rate limit"), 1)

    def test_method_call_allowed_cleans_stale_entries_on_fresh_call(self):
        now = time.monotonic()
        stale = (":stale", "Status")
        counts = {stale: [5, now - 3 * h._METHOD_CALL_WINDOW_S, False]}
        with (
            mock.patch.object(h, "_method_call_counts", counts),
            mock.patch.object(h, "LOG_PATH", self.log_path),
        ):
            self.assertTrue(h._method_call_allowed(":1.1", "Status"))
        self.assertNotIn(stale, counts)
        self.assertIn((":1.1", "Status"), counts)


class Authorize(unittest.TestCase):
    """polkit authorization: subject bound to the caller's bus name, a
    unique cancellation id, the result statuses, and timeout cancellation."""

    def test_authorize_builds_system_bus_name_subject_from_message_sender(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((True, False, {}),)
        h._authorize(conn, ":1.42", "en")
        args = conn.call_sync.call_args[0]
        self.assertEqual(args[0], "org.freedesktop.PolicyKit1")
        self.assertEqual(args[3], "CheckAuthorization")
        subject, action, details, flags, _cancellation = args[4].unpack()
        self.assertEqual(subject, ("system-bus-name", {"name": ":1.42"}))
        self.assertEqual(action, h.POLKIT_ACTION)
        self.assertEqual(details, {})
        self.assertEqual(flags, 1)

    def test_authorize_passes_unique_cancellation_id_per_invocation(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((True, False, {}),)
        h._authorize(conn, "sender", "en")
        first_id = conn.call_sync.call_args[0][4].unpack()[4]
        h._authorize(conn, "sender", "en")
        second_id = conn.call_sync.call_args[0][4].unpack()[4]
        self.assertIsInstance(first_id, str)
        self.assertIsInstance(second_id, str)
        self.assertNotEqual(first_id, second_id)

    def test_authorize_returns_authorized_status_when_polkit_says_authorized(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((True, False, {}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "authorized")
        self.assertEqual(detail, "")

    def test_authorize_returns_denied_status_when_polkit_says_not_authorized(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((False, True, {}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "denied")
        self.assertEqual(detail, "")

    def test_authorize_returns_cancelled_status_when_dialog_was_dismissed(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((False, True, {"polkit.dismissed": "1"}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "cancelled")
        self.assertEqual(detail, "")

    def test_authorize_returns_error_status_when_polkit_call_fails(self):
        conn = mock.Mock()
        conn.call_sync.side_effect = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "name has no owner", 8
        )
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "error")
        self.assertIn("name has no owner", detail)
        # No cancel attempt for a non-timeout failure.
        self.assertEqual(conn.call_sync.call_count, 1)

    def test_authorize_cancels_pending_check_when_polkit_times_out(self):
        def call_sync(*args):
            if args[3] == "CheckAuthorization":
                raise GLib.Error.new_literal(
                    GLib.quark_from_string("g-io-error-quark"), "timed out", 5
                )
            return (True,)

        conn = mock.Mock()
        conn.call_sync.side_effect = call_sync
        status, _detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "timeout")
        check = conn.call_sync.call_args_list[0][0]
        cancel = conn.call_sync.call_args_list[1][0]
        self.assertEqual(cancel[3], "CancelCheckAuthorization")
        self.assertEqual(cancel[4].unpack(), (check[4].unpack()[4],))


class SetPrimaryAuthorization(unittest.TestCase):
    def test_set_primary_returns_distinct_messages_for_denial_vs_service_error(self):
        def run(status, detail):
            invocation = mock.Mock()
            with mock.patch.object(h, "_authorized", return_value=(status, detail)):
                h.handle_set_primary(
                    mock.Mock(), "sender", ("0000:12:00.0", "en"), invocation
                )
            return invocation.return_value.call_args[0][0].unpack()

        denied = run("denied", "")
        error = run("error", "polkitd is not running")

        self.assertEqual(
            denied, (False, "Authorization was denied. Nothing changed.", "", False)
        )
        self.assertEqual(
            error,
            (
                False,
                "Could not check authorization (polkitd is not running). "
                "Nothing changed.",
                "",
                False,
            ),
        )


class TriggerDrm(unittest.TestCase):
    def test_trigger_drm_passes_settle_flag(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h._trigger_drm()
        argv = run.call_args[0][0]
        self.assertEqual(
            argv, ["udevadm", "trigger", "--subsystem-match=drm", "--settle"]
        )

    def test_trigger_drm_returns_false_when_trigger_times_out(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("udevadm", 30)
        ):
            self.assertFalse(h._trigger_drm())


class ParseUdevCurrentTags(unittest.TestCase):
    def test_parse_udev_current_tags_extracts_current_tag_list_from_property_output(
        self,
    ):
        # udev prints CURRENT_TAGS colon-separated with a leading and
        # trailing colon, alongside the cumulative TAGS line.
        output = (
            "DEVPATH=/devices/pci/0000:12:00.0\n"
            "TAGS=:mutter-device-preferred-primary:seat:uaccess:\n"
            "CURRENT_TAGS=:mutter-device-preferred-primary:seat:uaccess:\n"
            "ID_PATH=pci-0000:12:00.0\n"
        )
        self.assertEqual(
            h.parse_udev_current_tags(output),
            ["mutter-device-preferred-primary", "seat", "uaccess"],
        )

    def test_parse_udev_current_tags_returns_empty_list_for_empty_current_tags_line(
        self,
    ):
        self.assertEqual(h.parse_udev_current_tags("DEVPATH=/x\nCURRENT_TAGS=\n"), [])
        self.assertEqual(h.parse_udev_current_tags("DEVPATH=/x\n"), [])

    def test_parse_udev_current_tags_ignores_stale_tag_in_cumulative_tags_line(self):
        # The cumulative TAGS line still carries the tag from a previous
        # rule, but the last uevent (rule no longer matching) set none.
        output = (
            "DEVPATH=/x\n"
            "TAGS=:mutter-device-preferred-primary:seat:\n"
            "CURRENT_TAGS=:seat:\n"
        )
        self.assertEqual(h.parse_udev_current_tags(output), ["seat"])


class CardHasTag(unittest.TestCase):
    def test_card_has_tag_returns_true_when_mutter_tag_in_current_tags(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0,
                stderr="",
                stdout="DEVPATH=/x\n"
                "TAGS=:mutter-device-preferred-primary:\n"
                "CURRENT_TAGS=:mutter-device-preferred-primary:\n",
            )
            self.assertTrue(h._card_has_tag("card1"))
        argv = run.call_args[0][0]
        self.assertIn("--path=/sys/class/drm/card1", argv)

    def test_card_has_tag_returns_false_when_tag_only_in_stale_cumulative_tags(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0,
                stderr="",
                stdout="DEVPATH=/x\n"
                "TAGS=:mutter-device-preferred-primary:\n"
                "CURRENT_TAGS=:seat:\n",
            )
            self.assertFalse(h._card_has_tag("card1"))

    def test_card_has_tag_returns_false_when_udevadm_missing(self):
        # An unverifiable tag must be treated as absent: without the tag
        # mutter will not select the primary even after a relogin.
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            self.assertFalse(h._card_has_tag("card1"))
        run.assert_not_called()


class SetPrimaryRefresh(unittest.TestCase):
    """SetPrimary verifies the tag is present in the card's current udev
    tags (what mutter reads at session start) after the settled refresh."""

    def test_set_primary_returns_refresh_failed_when_tag_missing_after_refresh(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp5-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        def run(argv, **kwargs):
            # udevadm info (the tag query) reports no current tag; everything
            # else (verify, reload-rules, settled trigger) succeeds.
            if argv[:2] == ["udevadm", "info"]:
                return mock.Mock(
                    returncode=0, stderr="", stdout="DEVPATH=/x\nCURRENT_TAGS=\n"
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("os.chown"),
            mock.patch("subprocess.run", side_effect=run),
        ):
            h.handle_set_primary(
                mock.Mock(), "sender", ("0000:12:00.0", "en"), invocation
            )
        shutil.rmtree(tmp)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "Rule written for 0000:12:00.0. The udev refresh failed; "
                "a reboot is required.",
                "",
                False,
            ),
        )


class VerifyRule(unittest.TestCase):
    """udevadm verify must distinguish verified / unsupported / failed:
    an error or timeout is a failure, never an implicit verification."""

    def test_verify_rule_returns_verified_when_udevadm_verify_succeeds(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(h._verify_rule("/tmp/rule"), h.REFRESH_VERIFIED)

    def test_verify_rule_returns_unsupported_when_udevadm_missing(self):
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            self.assertEqual(h._verify_rule("/tmp/rule"), h.REFRESH_UNSUPPORTED)
        run.assert_not_called()

    def test_verify_rule_returns_failed_when_udevadm_verify_errors(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run", side_effect=OSError("spawn failed")),
        ):
            self.assertEqual(h._verify_rule("/tmp/rule"), h.REFRESH_FAILED)

    def test_verify_rule_returns_failed_when_udevadm_verify_times_out(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired("udevadm", 30)
            ),
        ):
            self.assertEqual(h._verify_rule("/tmp/rule"), h.REFRESH_FAILED)

    def test_verify_rule_returns_failed_when_udevadm_verify_rejects(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="bad")
            self.assertEqual(h._verify_rule("/tmp/rule"), h.REFRESH_FAILED)


class RefreshUdev(unittest.TestCase):
    """The refresh returns the verified / unsupported / failed state,
    never a bare success it cannot stand behind."""

    def test_refresh_skips_gracefully_when_udevadm_missing(self):
        # Missing udevadm is "unsupported": the refresh could not be
        # verified, so it must not be reported as success.
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            self.assertEqual(h._refresh_udev("card1"), h.REFRESH_UNSUPPORTED)
        run.assert_not_called()

    def test_refresh_returns_verified_when_all_steps_succeed(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):

            def run_side(argv, **kwargs):
                if argv[:2] == ["udevadm", "info"]:
                    return mock.Mock(
                        returncode=0,
                        stderr="",
                        stdout="DEVPATH=/x\n"
                        "CURRENT_TAGS=:mutter-device-preferred-primary:\n",
                    )
                return mock.Mock(returncode=0, stdout="", stderr="")

            run.side_effect = run_side
            self.assertEqual(h._refresh_udev("card1"), h.REFRESH_VERIFIED)

    def test_refresh_returns_failed_when_tag_missing(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):

            def run_side(argv, **kwargs):
                if argv[:2] == ["udevadm", "info"]:
                    return mock.Mock(
                        returncode=0, stderr="", stdout="DEVPATH=/x\nCURRENT_TAGS=\n"
                    )
                return mock.Mock(returncode=0, stdout="", stderr="")

            run.side_effect = run_side
            self.assertEqual(h._refresh_udev("card1"), h.REFRESH_FAILED)

    def test_refresh_returns_failed_when_tag_unverifiable(self):
        # udevadm info fails: the tag cannot be verified, so the refresh
        # must not report success.
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):

            def run_side(argv, **kwargs):
                if argv[:2] == ["udevadm", "info"]:
                    return mock.Mock(returncode=1, stdout="", stderr="no such device")
                return mock.Mock(returncode=0, stdout="", stderr="")

            run.side_effect = run_side
            self.assertEqual(h._refresh_udev("card1"), h.REFRESH_FAILED)

    def test_refresh_returns_failed_when_reload_fails(self):
        with (
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):

            def run_side(argv, **kwargs):
                if argv[:2] == ["udevadm", "control"]:
                    return mock.Mock(returncode=1, stdout="", stderr="boom")
                return mock.Mock(returncode=0, stdout="", stderr="")

            run.side_effect = run_side
            self.assertEqual(h._refresh_udev("card1"), h.REFRESH_FAILED)


class SetPrimaryTransaction(unittest.TestCase):
    """SetPrimary write-transaction failure paths and the idempotent
    no-write path."""

    PCI = "0000:12:00.0"

    def test_set_primary_returns_verify_rejected_when_udevadm_verify_fails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        def run(argv, **kwargs):
            # udevadm verify rejects the new rule; nothing else runs.
            if argv[:2] == ["udevadm", "verify"]:
                return mock.Mock(returncode=1, stdout="", stderr="bad rule")
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("os.chown"),
            mock.patch("subprocess.run", side_effect=run),
        ):
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (False, "udevadm verify rejected the rule; nothing changed.", "", False),
        )
        self.assertFalse(os.path.exists(managed))
        shutil.rmtree(tmp)

    def test_set_primary_returns_backup_failure_when_copy_fails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        old = 'SUBSYSTEMS=="pci", KERNELS=="0000:00:00.0"\n'
        with open(managed, "w", encoding="utf-8") as f:
            f.write(old)

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("os.chown"),
            mock.patch("shutil.copy2", side_effect=OSError("no space")),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (False, "Backup of the existing rule failed; nothing changed.", "", False),
        )
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)
        shutil.rmtree(tmp)

    def test_set_primary_returns_nothing_changed_when_rename_fails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        old = 'SUBSYSTEMS=="pci", KERNELS=="0000:00:00.0"\n'
        with open(managed, "w", encoding="utf-8") as f:
            f.write(old)

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("os.chown"),
            mock.patch("subprocess.run") as run,
            mock.patch("os.rename", side_effect=OSError("read-only filesystem")),
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (False, "Failed to write the managed rule; nothing changed.", "", False),
        )
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)
        shutil.rmtree(tmp)

    def test_set_primary_skips_write_when_rule_already_current(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        content = h._rule_text(self.PCI, "0x1002", "0x7550")
        with open(managed, "w", encoding="utf-8") as f:
            f.write(content)

        def run(argv, **kwargs):
            # reload-rules and the settled trigger succeed; the tag query
            # reports the mutter tag in the card's current tags.
            if argv[:2] == ["udevadm", "info"]:
                return mock.Mock(
                    returncode=0,
                    stderr="",
                    stdout="DEVPATH=/x\n"
                    "CURRENT_TAGS=:mutter-device-preferred-primary:\n",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run", side_effect=run),
        ):
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "The rule is already set for 0000:12:00.0. Udev rules reloaded.",
                "",
                True,
            ),
        )
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), content)
        # The write path was skipped entirely: no backup, no temp file.
        self.assertEqual(os.listdir(tmp), ["61-mutter-primary-gpu.rules"])
        shutil.rmtree(tmp)


class SetPrimaryUnsupported(unittest.TestCase):
    """With udevadm unavailable the verification is skipped (not a
    rejection) and the rule is still written, but the refresh reports
    unsupported and refresh_ok stays False - the logout fast path is not
    enabled."""

    PCI = "0000:12:00.0"

    def test_set_primary_writes_rule_when_udevadm_unavailable(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp12-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value=None),
            mock.patch("os.chown"),
            mock.patch("subprocess.run") as run,
        ):
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "Rule written for 0000:12:00.0. The udev refresh could "
                "not be verified (udevadm unavailable); a reboot is "
                "required.",
                "",
                False,
            ),
        )
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), h._rule_text(self.PCI, "0x1002", "0x7550"))
        run.assert_not_called()
        shutil.rmtree(tmp)

    def test_set_primary_reports_unsupported_when_udevadm_unavailable_when_rule_already_current(
        self,
    ):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp12-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        content = h._rule_text(self.PCI, "0x1002", "0x7550")
        with open(managed, "w", encoding="utf-8") as f:
            f.write(content)

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            h.handle_set_primary(mock.Mock(), "sender", (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "The rule is already set for 0000:12:00.0. The udev "
                "refresh could not be verified (udevadm unavailable); "
                "a reboot is required.",
                "",
                False,
            ),
        )
        run.assert_not_called()
        shutil.rmtree(tmp)


class SetAutoUnsupported(unittest.TestCase):
    """SetAuto with udevadm unavailable still performs the file change
    (remove / already-absent) but reports the refresh as unsupported and
    refresh_ok False - the logout fast path is not enabled."""

    def test_set_auto_reports_unsupported_when_udevadm_unavailable_when_rule_already_absent(
        self,
    ):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp13-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        self.assertFalse(os.path.exists(managed))

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as run,
        ):
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "No managed rule (auto selection already in effect). "
                "The udev refresh could not be verified (udevadm "
                "unavailable); a reboot is required.",
                False,
            ),
        )
        run.assert_not_called()
        shutil.rmtree(tmp)

    def test_set_auto_reports_unsupported_when_udevadm_unavailable_when_rule_removed(
        self,
    ):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp13-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        with open(managed, "w", encoding="utf-8") as f:
            f.write("old rule\n")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value=None),
            mock.patch("os.chown"),
        ):
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertFalse(os.path.exists(managed))
        backups = [
            f
            for f in os.listdir(tmp)
            if f.startswith("61-mutter-primary-gpu.rules.bak.")
        ]
        self.assertEqual(len(backups), 1)
        backup_path = os.path.join(tmp, backups[0])
        expected = (
            "Managed rule removed. A backup was saved to "
            f"{backup_path}. The udev refresh could not be verified "
            "(udevadm unavailable); a reboot is required."
        )
        self.assertEqual(result, (True, expected, False))
        shutil.rmtree(tmp)


class SetAutoTransaction(unittest.TestCase):
    def test_set_auto_returns_nothing_removed_when_unlink_fails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        with open(managed, "w", encoding="utf-8") as f:
            f.write("old rule\n")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("os.chown"),
            mock.patch("os.unlink", side_effect=OSError("read-only filesystem")),
        ):
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (False, "Failed to remove the managed rule; nothing changed.", False),
        )
        self.assertTrue(os.path.exists(managed))
        shutil.rmtree(tmp)


class SetAutoAlreadyAuto(unittest.TestCase):
    """A previous Reset may have removed the rule while its udev refresh
    failed, so the already-absent path must still refresh udev (as
    SetPrimary does for an unchanged rule) instead of returning
    unverified success."""

    def _absent_rule(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp6-")
        self.addCleanup(shutil.rmtree, tmp)
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        self.assertFalse(os.path.exists(managed))
        return managed

    def test_set_auto_refreshes_udev_when_rule_already_absent(self):
        managed = self._absent_rule()
        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "No managed rule (auto selection already in effect). "
                "Udev rules reloaded.",
                True,
            ),
        )
        argvs = [c[0][0] for c in run.call_args_list]
        self.assertIn(["udevadm", "control", "--reload-rules"], argvs)
        self.assertIn(
            ["udevadm", "trigger", "--subsystem-match=drm", "--settle"], argvs
        )

    def test_set_auto_returns_refresh_failed_when_trigger_fails_with_absent_rule(self):
        managed = self._absent_rule()

        def run(argv, **kwargs):
            if len(argv) > 1 and argv[1] == "trigger":
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with (
            mock.patch.object(h, "_authorized", return_value=("authorized", "")),
            mock.patch.object(h, "MANAGED_RULE", managed),
            mock.patch("shutil.which", return_value="/usr/bin/udevadm"),
            mock.patch("subprocess.run", side_effect=run),
        ):
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (
                True,
                "No managed rule (auto selection already in effect). "
                "The udev refresh failed; a reboot is required.",
                False,
            ),
        )


class BusContract(unittest.TestCase):
    def test_bus_contract_matches_helper_service_name_and_path_and_interface(self):
        self.assertEqual(bus.HELPER_NAME, h.SERVICE_NAME)
        self.assertEqual(bus.HELPER_PATH, h.OBJECT_PATH)
        self.assertEqual(bus.HELPER_IFACE, h.INTERFACE)

    def test_bus_contract_matches_introspection_methods_and_signatures(self):
        """INTROSPECTION_XML must declare the signatures the app side
        (bus.py) actually sends and expects, method for method."""

        def flat(sig: str) -> str:
            # A variant's format string is parenthesized for tuples, while
            # the introspection's per-arg concatenation is not.
            if sig.startswith("(") and sig.endswith(")"):
                return sig[1:-1]
            return sig

        expected = {
            "Status": ("s", "ss"),
            "SetPrimary": ("ss", "bssb"),
            "SetAuto": ("s", "bsb"),
        }
        node = Gio.DBusNodeInfo.new_for_xml(h.INTROSPECTION_XML)
        iface = node.interfaces[0]
        self.assertEqual(iface.name, h.INTERFACE)
        self.assertEqual(iface.name, bus.HELPER_IFACE)
        for name, signatures in expected.items():
            method = iface.lookup_method(name)
            declared = (
                "".join(a.signature for a in method.in_args),
                "".join(a.signature for a in method.out_args),
            )
            self.assertEqual(declared, signatures, name)

        sent = []
        connection = mock.Mock()
        connection.call.side_effect = lambda *args: sent.append(args)
        with mock.patch.object(Gio, "bus_get_sync", return_value=connection):
            bus.set_primary("0000:12:00.0", "en", lambda *a: None)
            bus.set_auto("en", lambda *a: None)
        self.assertEqual(len(sent), 2)
        for args, (method_name, signatures) in zip(
            sent,
            (("SetPrimary", expected["SetPrimary"]), ("SetAuto", expected["SetAuto"])),
        ):
            bus_name, bus_path, iface_name, method, params, reply_type = args[:6]
            self.assertEqual(
                (bus_name, bus_path, iface_name, method),
                (bus.HELPER_NAME, bus.HELPER_PATH, bus.HELPER_IFACE, method_name),
            )
            self.assertEqual(flat(params.get_type_string()), signatures[0])
            self.assertEqual(flat(reply_type.dup_string()), signatures[1])


if __name__ == "__main__":
    unittest.main()
