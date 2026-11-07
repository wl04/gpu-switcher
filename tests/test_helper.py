"""Unit tests for the root D-Bus helper (helper/gpu_switcher_helper.py).

Covers the pure PCI-format validation, the /sys resolution failure paths
(stubbed filesystem), the managed rule line format (golden string matching
the reference script mutter-primary-gpu.sh), the polkit authorization
(subject binding, cancellation id, result statuses — mocked D-Bus), the
distinct authorization-failure messages of SetPrimary, the
SetPrimary/SetAsync transaction failure paths (verify, backup, rename,
unlink) and the idempotent no-write path, the missing-catalog translator
fallback, the in-progress lock (Busy D-Bus error), the backup retention,
and the IPC contract agreement between the app side (gpu_switcher/bus.py),
the helper side and the INTROSPECTION_XML method signatures.

The async design is tested with two fakes: a connection whose
CheckAuthorization reply is delivered synchronously, and a Gio.Subprocess
whose communicate_async callback fires synchronously, so the whole
operation chain runs inline and assertions stay deterministic.
"""

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

import helper.gpu_switcher_helper as h
import udev_rules
from gpu_switcher import bus

_IO_QUARK = GLib.quark_from_string("g-io-error-quark")


def _polkit_conn(reply=None, call_error=None):
    """A fake connection delivering the CheckAuthorization reply inline."""
    conn = mock.Mock()

    def call_finish(_task):
        if call_error is not None:
            raise call_error
        return reply

    conn.call_finish.side_effect = call_finish

    def call(*args):
        # (dest, path, iface, method, params, type, flags, timeout,
        #  cancellable, callback, user_data)
        callback = args[9]
        callback(conn, None)

    conn.call.side_effect = call
    return conn


def _run_authorize(conn, sender=":1.29", locale="en", cancellable=None):
    result = []

    def capture(*args):
        result.append(args)

    h._authorize(conn, sender, locale, capture, cancellable)
    assert result, "authorize did not call on_done"
    return result[0]


def _fake_udev(results, started):
    """Patch Gio.Subprocess.new: `results` maps argv-tuple to rc/out/err or
    a GLib.Error that communicate_finish raises (step timeout/cancel)."""

    def factory(argv, _flags):
        key = tuple(argv)
        spec = None
        for k, v in results.items():  # "*" matches any element (e.g. temp path)
            if len(k) == len(key) and all(a == b or a == "*" for a, b in zip(k, key)):
                spec = v
                break
        if spec is None:
            raise AssertionError(f"unexpected udev command: {key}")
        proc = mock.Mock()

        def communicate_async(_stdin, _cancellable, callback, ud):
            callback(proc, None, ud)

        proc.communicate_async.side_effect = communicate_async
        if spec.get("error") is not None:
            proc.communicate_finish.side_effect = spec["error"]
        else:
            # communicate_finish returns Python bytes (matching the real API).
            proc.communicate_finish.return_value = (
                True,
                spec.get("out", "").encode("utf-8"),
                spec.get("err", "").encode("utf-8"),
            )
        proc.get_exit_status.return_value = spec.get("rc", 0)
        started.append(list(argv))
        return proc

    return mock.patch.object(Gio.Subprocess, "new", factory)


def _fake_authorize(status, detail):
    def fake(_connection, _sender, _locale, on_done, _cancellable=None):
        on_done(status, detail)

    return fake


def _invocation():
    inv = mock.Mock()
    inv.get_cancellable.return_value = Gio.Cancellable.new()
    return inv


RELOAD_ARGV = (h.UDEVDADM, "control", "--reload-rules")
TRIGGER_ARGV = (h.UDEVDADM, "trigger", "--subsystem-match=drm", "--settle")
TAGGED_INFO = "CURRENT_TAGS=:seat:mutter-device-preferred-primary:\nTAGS=:seat:\n"


def _info_argv(card):
    return (h.UDEVDADM, "info", "--query=property", f"--path=/sys/class/drm/{card}")


def _ok_results(card=None):
    results = {RELOAD_ARGV: {"rc": 0}, TRIGGER_ARGV: {"rc": 0}}
    if card is not None:
        results[_info_argv(card)] = {"rc": 0, "out": TAGGED_INFO}
    return results


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

    def test_resolve_gpu_returns_none_when_pci_not_a_display_gpu(self):
        with mock.patch.object(h, "is_display_gpu", return_value=None):
            self.assertIsNone(h._resolve_gpu("0000:12:00.0"))

    def test_resolve_gpu_reads_vendor_and_device_from_sys(self):
        with (
            mock.patch.object(h, "is_display_gpu", return_value="card1"),
            mock.patch.object(
                h,
                "read_text",
                side_effect=lambda p: {
                    "vendor": "0x1002",
                    "device": "0x7550",
                }.get(os.path.basename(p)),
            ),
        ):
            self.assertEqual(
                h._resolve_gpu("0000:12:00.0"), ("card1", "0x1002", "0x7550")
            )

    def test_resolve_gpu_returns_none_when_two_cards_bound_to_same_pci(self):
        # Ambiguous binding: is_display_gpu already refuses it.
        with mock.patch.object(h, "is_display_gpu", return_value=None):
            self.assertIsNone(h._resolve_gpu("0000:12:00.0"))


class Translator(unittest.TestCase):
    def test_translator_returns_identity_when_catalog_is_missing(self):
        self.assertEqual(h._translator("en")("Some text"), "Some text")


class NormalizeLocale(unittest.TestCase):
    def test_normalize_locale_maps_known_language(self):
        self.assertEqual(h._normalize_locale("ru_RU.UTF-8"), "ru")

    def test_normalize_locale_falls_back_to_english_for_unknown_language(self):
        self.assertEqual(h._normalize_locale("de_DE.UTF-8"), "en")

    def test_normalize_locale_rejects_path_shaped_values(self):
        self.assertEqual(h._normalize_locale("../evil"), "en")
        self.assertEqual(h._normalize_locale("/etc/passwd"), "en")
        self.assertEqual(h._normalize_locale(".."), "en")

    def test_normalize_locale_rejects_overlong_values(self):
        self.assertEqual(h._normalize_locale("a" * 40), "en")


class TranslatorLocale(unittest.TestCase):
    def test_translator_uses_normalized_locale_for_catalog_lookup(self):
        with (
            mock.patch.object(h, "_normalize_locale", return_value="en"),
            mock.patch("gettext.translation") as translation,
        ):
            translation.return_value.gettext = lambda s: s
            h._translator("ru_RU.UTF-8")
            langs = translation.call_args.kwargs["languages"]
            self.assertEqual(langs, ["en"])


class Authorize(unittest.TestCase):
    """polkit authorization: subject bound to the caller's bus name, a
    unique cancellation id, the result statuses, and timeout cancellation."""

    def test_authorize_builds_system_bus_name_subject_from_message_sender(self):
        conn = _polkit_conn(reply=((True, False, {}),))
        _run_authorize(conn, sender=":1.29")
        args = conn.call.call_args[0]
        self.assertEqual(args[0], "org.freedesktop.PolicyKit1")
        self.assertEqual(args[2], h.POLKIT_IFACE)
        self.assertEqual(args[3], "CheckAuthorization")
        subject, action, _details, flags, _cancellation_id = args[4]
        self.assertEqual(subject[0], "system-bus-name")
        self.assertEqual(subject[1]["name"], ":1.29")
        self.assertEqual(action, h.POLKIT_ACTION)
        self.assertEqual(flags, h.POLKIT_INTERACTIVE)

    def test_authorize_passes_unique_cancellation_id_per_invocation(self):
        ids = []
        for _ in range(2):
            conn = _polkit_conn(reply=((True, False, {}),))
            _run_authorize(conn)
            ids.append(conn.call.call_args[0][4][4])
        self.assertEqual(len(set(ids)), 2)

    def test_authorize_reports_authorized(self):
        conn = _polkit_conn(reply=((True, False, {}),))
        self.assertEqual(_run_authorize(conn), ("authorized", ""))

    def test_authorize_reports_denied(self):
        conn = _polkit_conn(reply=((False, False, {}),))
        self.assertEqual(_run_authorize(conn), ("denied", ""))

    def test_authorize_reports_cancelled_when_dialog_dismissed(self):
        conn = _polkit_conn(reply=((False, False, {"polkit.dismissed": True}),))
        self.assertEqual(_run_authorize(conn), ("cancelled", ""))

    def test_authorize_reports_error_on_check_authorization_failure(self):
        conn = _polkit_conn(
            call_error=GLib.Error.new_literal(_IO_QUARK, "polkitd is not running", 8)
        )
        self.assertEqual(_run_authorize(conn), ("error", "polkitd is not running"))

    def test_authorize_returns_timeout_cancels_check_when_polkit_times_out(self):
        conn = _polkit_conn(
            call_error=GLib.Error.new_literal(_IO_QUARK, "timed out", 7)
        )
        cancellable = Gio.Cancellable.new()
        status, detail = _run_authorize(conn, cancellable=cancellable)
        self.assertEqual((status, detail), ("timeout", ""))
        self.assertTrue(cancellable.is_cancelled())
        self.assertTrue(conn.call_sync.called)
        self.assertEqual(conn.call_sync.call_args[0][3], "CancelCheckAuthorization")

    def test_authorize_does_not_treat_io_error_code_5_as_timeout(self):
        conn = _polkit_conn(call_error=GLib.Error.new_literal(_IO_QUARK, "io", 5))
        status, detail = _run_authorize(conn)
        self.assertEqual(status, "error")
        self.assertEqual(detail, "io")
        self.assertFalse(conn.call_sync.called)


class SetPrimaryAuthorization(unittest.TestCase):
    def setUp(self):
        h._operation_in_progress = False

    def tearDown(self):
        h._operation_in_progress = False

    def test_set_primary_returns_distinct_messages_for_denial_vs_service_error(self):
        def run(status, detail):
            invocation = _invocation()
            with mock.patch.object(h, "_authorize", _fake_authorize(status, detail)):
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

    def test_set_primary_reports_conflict_when_other_rule_sets_tag(self):
        invocation = _invocation()
        with (
            mock.patch.object(h, "_authorize", _fake_authorize("authorized", "")),
            mock.patch.object(
                h, "rule_conflicts", return_value=["/etc/udev/rules.d/10-x.rules"]
            ),
        ):
            h.handle_set_primary(
                mock.Mock(), "sender", ("0000:12:00.0", "en"), invocation
            )
        self.assertEqual(
            invocation.return_value.call_args[0][0].unpack(),
            (
                False,
                "Another udev rule sets the preferred GPU: "
                "/etc/udev/rules.d/10-x.rules. Resolve it manually first.",
                "",
                False,
            ),
        )

    def test_set_primary_reports_invalid_pci(self):
        invocation = _invocation()
        with (
            mock.patch.object(h, "_authorize", _fake_authorize("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(h, "_resolve_gpu", return_value=None),
        ):
            h.handle_set_primary(
                mock.Mock(), "sender", ("0000:12:00.0", "en"), invocation
            )
        self.assertEqual(
            invocation.return_value.call_args[0][0].unpack(),
            (
                False,
                "Target PCI 0000:12:00.0 is not a valid display controller.",
                "",
                False,
            ),
        )


class OperationInProgress(unittest.TestCase):
    """The in-progress lock: a second mutating call while an operation is
    in flight gets the Busy D-Bus error, and Status stays responsive."""

    def setUp(self):
        h._operation_in_progress = False

    def tearDown(self):
        h._operation_in_progress = False

    def test_set_primary_rejects_concurrent_second_call_as_busy(self):
        def never_authorize(_connection, _sender, _locale, _on_done, _cancellable=None):
            pass  # the operation stays in flight

        with mock.patch.object(h, "_authorize", never_authorize):
            inv1 = _invocation()
            h.handle_set_primary(mock.Mock(), "sender", ("0000:12:00.0", "en"), inv1)
            inv2 = _invocation()
            h.handle_set_primary(mock.Mock(), "sender", ("0000:12:00.0", "en"), inv2)
            inv2.return_dbus_error.assert_called_once()
            name, message = inv2.return_dbus_error.call_args[0]
            self.assertEqual(name, udev_rules.DBUS_ERROR_BUSY)
            self.assertTrue(message)
            inv1.return_value.assert_not_called()
            inv2.return_value.assert_not_called()

    def test_set_auto_rejects_concurrent_second_call_as_busy(self):
        def never_authorize(_connection, _sender, _locale, _on_done, _cancellable=None):
            pass

        with mock.patch.object(h, "_authorize", never_authorize):
            inv1 = _invocation()
            h.handle_set_auto(mock.Mock(), "sender", ("en",), inv1)
            inv2 = _invocation()
            h.handle_set_auto(mock.Mock(), "sender", ("en",), inv2)
            inv2.return_dbus_error.assert_called_once()
            self.assertEqual(
                inv2.return_dbus_error.call_args[0][0], udev_rules.DBUS_ERROR_BUSY
            )

    def test_status_stays_responsive_while_operation_in_progress(self):
        h._operation_in_progress = True
        try:
            with mock.patch.object(h, "read_text", return_value=""):
                invocation = _invocation()
                h.handle_status(mock.Mock(), "sender", ("en",), invocation)
            rule, message = invocation.return_value.call_args[0][0].unpack()
            self.assertEqual(rule, "")
            self.assertEqual(message, "No managed rule (auto selection).")
        finally:
            h._operation_in_progress = False


class ErrorNames(unittest.TestCase):
    """The D-Bus error names are a stable contract shared by both sides."""

    def test_error_names_are_valid_and_shared(self):
        self.assertIs(h.DBUS_ERROR_BUSY, udev_rules.DBUS_ERROR_BUSY)
        self.assertIs(h.DBUS_ERROR_INTERNAL, udev_rules.DBUS_ERROR_INTERNAL)
        for name in (h.DBUS_ERROR_BUSY, h.DBUS_ERROR_INTERNAL):
            self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class VerifyRule(unittest.TestCase):
    def test_verify_rule_returns_verified_when_udevadm_accepts(self):
        started = []
        with (
            _fake_udev({(h.UDEVDADM, "verify", "/tmp/r.rules"): {"rc": 0}}, started),
            mock.patch.object(h, "_check_udevadm", return_value=True),
        ):
            result = []
            h._verify_rule_async("/tmp/r.rules", result.append)
            self.assertEqual(result, [h.REFRESH_VERIFIED])
        self.assertEqual(started, [[h.UDEVDADM, "verify", "/tmp/r.rules"]])

    def test_verify_rule_returns_failed_when_udevadm_rejects(self):
        with (
            _fake_udev({(h.UDEVDADM, "verify", "/tmp/r.rules"): {"rc": 1}}, []),
            mock.patch.object(h, "_check_udevadm", return_value=True),
        ):
            result = []
            h._verify_rule_async("/tmp/r.rules", result.append)
            self.assertEqual(result, [h.REFRESH_FAILED])

    def test_verify_rule_returns_failed_when_step_times_out(self):
        error = GLib.Error.new_literal(_IO_QUARK, "Operation was cancelled", 8)
        with (
            _fake_udev({(h.UDEVDADM, "verify", "/tmp/r.rules"): {"error": error}}, []),
            mock.patch.object(h, "_check_udevadm", return_value=True),
        ):
            result = []
            h._verify_rule_async("/tmp/r.rules", result.append)
            self.assertEqual(result, [h.REFRESH_FAILED])

    def test_verify_rule_returns_unsupported_when_udevadm_missing(self):
        started = []
        with mock.patch.object(h, "_check_udevadm", return_value=False):
            result = []
            h._verify_rule_async("/tmp/r.rules", result.append)
            self.assertEqual(result, [h.REFRESH_UNSUPPORTED])
        self.assertEqual(started, [])


class RefreshUdev(unittest.TestCase):
    def setUp(self):
        self.started = []

    def _run(self, results, card):
        with (
            _fake_udev(results, self.started),
            mock.patch.object(h, "_check_udevadm", return_value=True),
        ):
            out = []
            h._refresh_udev_async(card, out.append)
            self.assertEqual(len(out), 1)
            return out[0]

    def test_refresh_udev_returns_verified_when_every_step_succeeds_and_tag_present(
        self,
    ):
        self.assertEqual(self._run(_ok_results("card1"), "card1"), h.REFRESH_VERIFIED)
        self.assertEqual(
            self.started,
            [
                [RELOAD_ARGV[0], "control", "--reload-rules"],
                [TRIGGER_ARGV[0], "trigger", "--subsystem-match=drm", "--settle"],
                list(_info_argv("card1")),
            ],
        )

    def test_refresh_udev_returns_failed_when_reload_fails(self):
        results = {RELOAD_ARGV: {"rc": 1}, TRIGGER_ARGV: {"rc": 0}}
        self.assertEqual(self._run(results, None), h.REFRESH_FAILED)
        self.assertEqual(len(self.started), 1)

    def test_refresh_udev_returns_failed_when_trigger_fails(self):
        results = {RELOAD_ARGV: {"rc": 0}, TRIGGER_ARGV: {"rc": 1}}
        self.assertEqual(self._run(results, None), h.REFRESH_FAILED)
        self.assertEqual(len(self.started), 2)

    def test_refresh_udev_returns_failed_when_tag_absent(self):
        results = _ok_results("card1")
        results[_info_argv("card1")] = {"rc": 0, "out": "CURRENT_TAGS=:seat:\n"}
        self.assertEqual(self._run(results, "card1"), h.REFRESH_FAILED)

    def test_refresh_udev_returns_failed_when_tag_query_fails(self):
        results = _ok_results("card1")
        results[_info_argv("card1")] = {"rc": 1}
        self.assertEqual(self._run(results, "card1"), h.REFRESH_FAILED)

    def test_refresh_udev_returns_unsupported_when_udevadm_missing(self):
        with (
            _fake_udev({}, self.started),
            mock.patch.object(h, "_check_udevadm", return_value=False),
        ):
            out = []
            h._refresh_udev_async(None, out.append)
            self.assertEqual(out, [h.REFRESH_UNSUPPORTED])
        self.assertEqual(self.started, [])

    def test_refresh_udev_without_card_skips_tag_check(self):
        self.assertEqual(self._run(_ok_results(None), None), h.REFRESH_VERIFIED)
        self.assertEqual(len(self.started), 2)


class ParseUdevCurrentTags(unittest.TestCase):
    def test_parse_udev_current_tags_extracts_current_tag_list_from_property_output(
        self,
    ):
        # udev prints CURRENT_TAGS colon-separated with a leading and
        # trailing colon, alongside the cumulative TAGS line.
        out = (
            "SUBSYSTEM=drm\n"
            "DEVTYPE=drm_minor\n"
            "CURRENT_TAGS=:seat:mutter-device-preferred-primary:uaccess:\n"
            "TAGS=:seat:uaccess:\n"
        )
        self.assertEqual(
            h.parse_udev_current_tags(out),
            ["seat", "mutter-device-preferred-primary", "uaccess"],
        )

    def test_parse_udev_current_tags_returns_empty_list_when_no_current_tags(self):
        self.assertEqual(h.parse_udev_current_tags("CURRENT_TAGS=:\n"), [])
        self.assertEqual(h.parse_udev_current_tags("SUBSYSTEM=drm\n"), [])


class SetPrimaryTransactionBase(unittest.TestCase):
    """Shared setup for the SetPrimary write-path tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="gpu-switcher-sp-")
        self.addCleanup(self._tmp.cleanup)
        self.rules_dir = self._tmp.name
        self.rule = os.path.join(self.rules_dir, "61-mutter-primary-gpu.rules")
        h._operation_in_progress = False

    def tearDown(self):
        h._operation_in_progress = False

    def _run_set_primary(self, results, invocation=None, udevadm=True):
        inv = invocation or _invocation()
        patches = [
            _fake_udev(results, []),
            mock.patch.object(h, "MANAGED_RULE", self.rule),
            mock.patch.object(h, "_authorize", _fake_authorize("authorized", "")),
            mock.patch.object(h, "rule_conflicts", return_value=[]),
            mock.patch.object(
                h, "_resolve_gpu", return_value=("card1", "0x1002", "0x7550")
            ),
            mock.patch.object(h, "_check_udevadm", return_value=udevadm),
            mock.patch("os.chown"),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            h.handle_set_primary(mock.Mock(), "sender", ("0000:12:00.0", "en"), inv)
        self.assertTrue(inv.return_value.called, "SetPrimary did not reply")
        return inv.return_value.call_args[0][0].unpack()


class SetPrimaryTransaction(SetPrimaryTransactionBase):
    def test_set_primary_writes_rule_backs_up_and_refreshes_udev(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write('SUBSYSTEM=="drm", TAG="something-else"\n')
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 0},
            **_ok_results("card1"),
        }
        with mock.patch.object(h, "_fsync_dir") as fsync_dir:
            ok, message, backup, refresh_ok = self._run_set_primary(results)
        self.assertTrue(ok)
        self.assertTrue(refresh_ok)
        self.assertTrue(message.startswith("Rule written for 0000:12:00.0."))
        self.assertIn("A backup was saved to", message)
        self.assertTrue(backup.startswith(self.rule + ".bak."))
        self.assertTrue(os.path.exists(backup))
        with open(self.rule, encoding="utf-8") as f:
            self.assertIn('KERNELS=="0000:12:00.0"', f.read())
        fsync_dir.assert_called_once_with(self.rules_dir)

    def test_set_primary_does_not_rename_when_verify_rejects(self):
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 1},
            **_ok_results("card1"),
        }
        ok, message, backup, refresh_ok = self._run_set_primary(results)
        self.assertEqual(
            (ok, message, backup, refresh_ok),
            (False, "udevadm verify rejected the rule; nothing changed.", "", False),
        )
        self.assertFalse(os.path.exists(self.rule))

    def test_set_primary_does_not_rename_when_backup_fails(self):
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 0},
            **_ok_results("card1"),
        }
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write("old rule\n")
        with mock.patch.object(h, "_backup_rule", return_value=""):
            ok, message, backup, refresh_ok = self._run_set_primary(results)
        self.assertEqual(
            (ok, message, backup, refresh_ok),
            (False, "Backup of the existing rule failed; nothing changed.", "", False),
        )
        with open(self.rule, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old rule\n")

    def test_set_primary_reports_failure_when_rename_fails(self):
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 0},
            **_ok_results("card1"),
        }
        with mock.patch.object(h.os, "rename", side_effect=OSError("boom")):
            ok, message, backup, refresh_ok = self._run_set_primary(results)
        self.assertEqual(
            (ok, message, backup, refresh_ok),
            (False, "Failed to write the managed rule; nothing changed.", "", False),
        )

    def test_set_primary_idempotent_when_rule_already_matches(self):
        content = h._rule_text("0000:12:00.0", "0x1002", "0x7550")
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write(content)
        results = _ok_results("card1")  # no verify step on the idempotent path
        with mock.patch.object(h, "_fsync_dir") as fsync_dir:
            ok, message, backup, refresh_ok = self._run_set_primary(results)
        self.assertTrue(ok)
        self.assertTrue(refresh_ok)
        self.assertEqual(backup, "")
        self.assertEqual(
            message, "The rule is already set for 0000:12:00.0. Udev rules reloaded."
        )
        fsync_dir.assert_not_called()

    def test_set_primary_retains_only_the_newest_backups(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write("old rule\n")
        for i in range(h._BACKUP_KEEP + 2):
            with open(
                f"{self.rule}.bak.2026010{i}T000000Z.{i:06x}", "w", encoding="utf-8"
            ) as f:
                f.write(f"backup {i}\n")
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 0},
            **_ok_results("card1"),
        }
        ok, _message, backup, _refresh_ok = self._run_set_primary(results)
        self.assertTrue(ok)
        backups = sorted(
            e
            for e in os.listdir(self.rules_dir)
            if e.startswith("61-mutter-primary-gpu.rules.bak.")
        )
        self.assertEqual(len(backups), h._BACKUP_KEEP)
        self.assertIn(os.path.basename(backup), backups)


class SetPrimaryNonUtf8(SetPrimaryTransactionBase):
    def test_set_primary_overwrites_non_utf8_existing_rule(self):
        # A broken hand edit may leave invalid UTF-8 in the rule; the read
        # is byte-based so the comparison and overwrite must not crash.
        with open(self.rule, "wb") as f:
            f.write(b'SUBSYSTEM=="drm", TAG\x80="x"\n')
        results = {
            (h.UDEVDADM, "verify", "*"): {"rc": 0},
            **_ok_results("card1"),
        }
        ok, _message, backup, _refresh_ok = self._run_set_primary(results)
        self.assertTrue(ok)
        with open(self.rule, encoding="utf-8") as f:
            self.assertIn('KERNELS=="0000:12:00.0"', f.read())
        self.assertTrue(backup)


class SetPrimaryUnsupported(SetPrimaryTransactionBase):
    """udevadm missing: verification is impossible, and a refresh cannot
    be reported as fresh - the user must be told a reboot is required."""

    def test_set_primary_reports_unsupported_when_udevadm_missing(self):
        results = _ok_results("card1")
        ok, message, _backup, refresh_ok = self._run_set_primary(results, udevadm=False)
        self.assertTrue(ok)  # the rule was still written
        self.assertFalse(refresh_ok)
        self.assertIn("could not be verified", message)
        self.assertIn("reboot is required", message)
        self.assertTrue(os.path.exists(self.rule))


class SetAutoTransactionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="gpu-switcher-sa-")
        self.addCleanup(self._tmp.cleanup)
        self.rules_dir = self._tmp.name
        self.rule = os.path.join(self.rules_dir, "61-mutter-primary-gpu.rules")
        h._operation_in_progress = False

    def tearDown(self):
        h._operation_in_progress = False

    def _run_set_auto(self, results=None, invocation=None, udevadm=True):
        inv = invocation or _invocation()
        patches = [
            _fake_udev(results or _ok_results(None), []),
            mock.patch.object(h, "MANAGED_RULE", self.rule),
            mock.patch.object(h, "_authorize", _fake_authorize("authorized", "")),
            mock.patch.object(h, "_check_udevadm", return_value=udevadm),
            mock.patch("os.chown"),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            h.handle_set_auto(mock.Mock(), "sender", ("en",), inv)
        self.assertTrue(inv.return_value.called, "SetAuto did not reply")
        return inv.return_value.call_args[0][0].unpack()


class SetAutoTransaction(SetAutoTransactionBase):
    def test_set_auto_removes_rule_backs_up_and_refreshes(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write('SUBSYSTEM=="drm", TAG="x"\n')
        ok, message, refresh_ok = self._run_set_auto()
        self.assertTrue(ok)
        self.assertTrue(refresh_ok)
        self.assertFalse(os.path.exists(self.rule))
        self.assertIn("Managed rule removed.", message)
        self.assertIn("A backup was saved to", message)
        self.assertEqual(
            len(
                [
                    e
                    for e in os.listdir(self.rules_dir)
                    if e.startswith("61-mutter-primary-gpu.rules.bak.")
                ]
            ),
            1,
        )

    def test_set_auto_refuses_symlink(self):
        os.symlink("/nonexistent-target", self.rule)
        ok, message, refresh_ok = self._run_set_auto()
        self.assertEqual(
            (ok, message, refresh_ok),
            (False, "The managed rule is a symlink; refusing to remove it.", False),
        )
        self.assertTrue(os.path.islink(self.rule))

    def test_set_auto_reports_failure_when_backup_fails(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write("old rule\n")
        with mock.patch.object(h, "_backup_rule", return_value=""):
            ok, message, refresh_ok = self._run_set_auto()
        self.assertEqual(
            (ok, message, refresh_ok),
            (False, "Backup of the existing rule failed; nothing removed.", False),
        )
        with open(self.rule, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old rule\n")

    def test_set_auto_reports_failure_when_unlink_fails(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write("old rule\n")
        with (
            mock.patch.object(h, "_backup_rule", return_value=self.rule + ".bak.x"),
            mock.patch.object(h.os, "unlink", side_effect=OSError("boom")),
        ):
            ok, message, refresh_ok = self._run_set_auto()
        self.assertEqual(
            (ok, message, refresh_ok),
            (False, "Failed to remove the managed rule; nothing changed.", False),
        )
        self.assertTrue(os.path.exists(self.rule))


class SetAutoAlreadyAuto(SetAutoTransactionBase):
    def test_set_auto_is_idempotent_when_rule_is_absent(self):
        ok, message, refresh_ok = self._run_set_auto()
        self.assertTrue(ok)
        self.assertTrue(refresh_ok)
        self.assertEqual(
            message,
            "No managed rule (auto selection already in effect). Udev rules reloaded.",
        )

    def test_set_auto_reports_unsupported_when_rule_absent_and_udevadm_missing(self):
        ok, message, refresh_ok = self._run_set_auto(udevadm=False)
        self.assertTrue(ok)
        self.assertFalse(refresh_ok)
        self.assertIn("could not be verified", message)


class SetAutoUnsupported(SetAutoTransactionBase):
    """udevadm missing: a removal refresh must be reported as unverifiable."""

    def test_set_auto_reports_unsupported_when_udevadm_missing(self):
        with open(self.rule, "w", encoding="utf-8") as f:
            f.write("old rule\n")
        ok, message, refresh_ok = self._run_set_auto(udevadm=False)
        self.assertTrue(ok)
        self.assertFalse(refresh_ok)
        self.assertIn("could not be verified", message)
        self.assertIn("reboot is required", message)
        self.assertFalse(os.path.exists(self.rule))


class IdleTimerInMain(unittest.TestCase):
    def test_main_arms_idle_timer_without_any_handled_call(self):
        # A D-Bus-answered call (gdbus introspect) must not keep the
        # helper alive: the idle timer is armed as soon as the name is
        # owned, so 60 s after the last real call the process exits.
        with (
            mock.patch.object(h, "_check_udevadm", return_value=True),
            mock.patch.object(Gio, "bus_get_sync", return_value=mock.Mock()),
            mock.patch.object(Gio, "bus_own_name"),
            mock.patch.object(GLib, "MainLoop") as loop_cls,
            mock.patch.object(h, "_IDLE_SOURCE", None),
        ):
            loop_cls.return_value.run.return_value = None
            h.main()
            self.assertIsNotNone(h._IDLE_SOURCE)


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
