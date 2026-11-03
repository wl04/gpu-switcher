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
import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

import helper.gpu_switcher_helper as h
from gpu_switcher import bus


class IsValidPciFormat(unittest.TestCase):
    def test_IsValidPciFormat_AcceptsFullPciAddress(self):
        self.assertTrue(h.is_valid_pci_format("0000:12:00.0"))

    def test_IsValidPciFormat_RejectsDomainOmittedAddress(self):
        self.assertFalse(h.is_valid_pci_format("12:00.0"))

    def test_IsValidPciFormat_RejectsNonHexAddress(self):
        self.assertFalse(h.is_valid_pci_format("0000:GG:00.0"))


class RuleText(unittest.TestCase):
    def test_RuleText_ProducesReferenceRuleLine_ForValidPci(self):
        self.assertEqual(
            h._rule_text("0000:12:00.0", "0x1002", "0x7550"),
            "# Managed by gpu-switcher; selected PCI 0000:12:00.0\n"
            "# Mutter uses TAG; ENV is retained for compatibility "
            "with the previous local rule.\n"
            "SUBSYSTEM==\"drm\", ENV{DEVTYPE}==\"drm_minor\", "
            "KERNEL==\"card[0-9]*\", KERNEL!=\"card*-*\", "
            "SUBSYSTEMS==\"pci\", KERNELS==\"0000:12:00.0\", "
            "ATTRS{vendor}==\"0x1002\", ATTRS{device}==\"0x7550\", "
            "ENV{MUTTER_DEVICE_PREFERRED_PRIMARY}=\"1\", "
            "TAG+=\"mutter-device-preferred-primary\"\n")


class ResolveGpu(unittest.TestCase):
    def test_ResolveGpu_ReturnsNone_ForMalformedPci(self):
        self.assertIsNone(h._resolve_gpu("12:00.0"))
        self.assertIsNone(h._resolve_gpu("0000:12:00"))
        self.assertIsNone(h._resolve_gpu("not-a-pci"))

    def test_ResolveGpu_ReturnsNone_WhenPciNotADisplayController(self):
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch("helper.gpu_switcher_helper.read_text",
                        return_value="0x080000"):
            self.assertIsNone(h._resolve_gpu("0000:12:00.0"))

    def test_ResolveGpu_Accepts3DControllerClass(self):
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.path.islink", return_value=True), \
             mock.patch("os.path.realpath",
                        return_value="/dev/pci/0000:12:00.0"), \
             mock.patch("os.listdir", return_value=["card1"]), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("helper.gpu_switcher_helper.read_text",
                        side_effect=lambda path: (
                            "0x030200" if path.endswith("/class") else
                            "0x1002" if path.endswith("/vendor") else
                            "0x7550" if path.endswith("/device") else "")):
            self.assertEqual(
                h._resolve_gpu("0000:12:00.0"),
                ("card1", "0x1002", "0x7550"))


class Translator(unittest.TestCase):
    def test_Translator_ReturnsIdentity_WhenCatalogMissing(self):
        with mock.patch("helper.gpu_switcher_helper.LOCALE_BASE",
                        "/nonexistent-gpu-switcher-locale"):
            tr = h._translator("ru")
        self.assertEqual(tr("No catalog here"), "No catalog here")


class NormalizeLocale(unittest.TestCase):
    """The locale arrives from an unauthenticated D-Bus caller, so it is
    mapped to the allowlisted catalog languages before gettext sees it.
    Path-shaped values would let gettext open (or block on) files outside
    LOCALE_BASE."""

    def test_NormalizeLocale_KeepsSupportedLanguages(self):
        self.assertEqual(h._normalize_locale("en"), "en")
        self.assertEqual(h._normalize_locale("ru"), "ru")
        self.assertEqual(h._normalize_locale("ru_RU.UTF-8"), "ru")
        self.assertEqual(h._normalize_locale("en_US"), "en")

    def test_NormalizeLocale_FallsBackToEnglish_ForUnsupportedOrPathLike(self):
        for locale in ("fr", "", None, "ru/ru", "..",
                       "/tmp/evil/LC_MESSAGES/gpu-switcher.mo",
                       "C:\\evil", "x" * 33):
            self.assertEqual(h._normalize_locale(locale), "en", locale)


class TranslatorLocale(unittest.TestCase):
    def test_Translator_PassesOnlyAllowlistedLanguages_ToGettext(self):
        """Contract: only allowlisted language identifiers reach
        gettext.translation, so catalog loading cannot leave LOCALE_BASE.
        """
        fake = mock.Mock(gettext=lambda s: s)
        with mock.patch("helper.gpu_switcher_helper.gettext.translation",
                        return_value=fake) as translation:
            for locale in ("ru", "ru_RU.UTF-8", "fr",
                           "/tmp/evil/LC_MESSAGES/gpu-switcher.mo", ".."):
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

    def test_Log_TruncatesOverlongCallerText(self):
        with mock.patch.object(h, "LOG_PATH", self.log_path):
            h._log("pci=" + "a" * 5000 + " locale=x")
        with open(self.log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        # prefix "gpu-switcher-helper " + UTC timestamp + one space,
        # then the capped message (cap plus the truncation marker).
        self.assertLessEqual(
            len(lines[0]),
            len("gpu-switcher-helper ") + 20 + 1
            + h._LOG_MAX_CHARS + len("…[truncated]"))
        self.assertTrue(lines[0].endswith("…[truncated]"))

    def test_Log_FlattensNewlinesAndDropsControlCharacters(self):
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

    def test_MethodCallAllowed_SuppressesBeyondLimitOncePerWindow(self):
        with mock.patch.object(h, "_method_call_counts", {}), \
             mock.patch.object(h, "LOG_PATH", self.log_path):
            for _ in range(h._METHOD_CALL_LIMIT):
                self.assertTrue(h._method_call_allowed(":1.1", "Status"))
            for _ in range(3):
                self.assertFalse(h._method_call_allowed(":1.1", "Status"))
            # A different sender keeps its own budget.
            self.assertTrue(h._method_call_allowed(":1.2", "Status"))
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content.count("rate limit"), 1)


class Authorize(unittest.TestCase):
    """polkit authorization: subject bound to the caller's bus name, a
    unique cancellation id, the result statuses, and timeout cancellation."""

    def test_Authorize_BuildsSystemBusNameSubject_FromMessageSender(self):
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

    def test_Authorize_PassesUniqueCancellationId_PerInvocation(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((True, False, {}),)
        h._authorize(conn, "sender", "en")
        first_id = conn.call_sync.call_args[0][4].unpack()[4]
        h._authorize(conn, "sender", "en")
        second_id = conn.call_sync.call_args[0][4].unpack()[4]
        self.assertIsInstance(first_id, str)
        self.assertIsInstance(second_id, str)
        self.assertNotEqual(first_id, second_id)

    def test_Authorize_ReturnsAuthorizedStatus_WhenPolkitSaysAuthorized(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((True, False, {}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "authorized")
        self.assertEqual(detail, "")

    def test_Authorize_ReturnsDeniedStatus_WhenPolkitSaysNotAuthorized(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((False, True, {}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "denied")
        self.assertEqual(detail, "")

    def test_Authorize_ReturnsCancelledStatus_WhenDialogWasDismissed(self):
        conn = mock.Mock()
        conn.call_sync.return_value = ((False, True, {"polkit.dismissed": "1"}),)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "cancelled")
        self.assertEqual(detail, "")

    def test_Authorize_ReturnsErrorStatus_WhenPolkitCallFails(self):
        conn = mock.Mock()
        conn.call_sync.side_effect = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"), "name has no owner", 8)
        status, detail = h._authorize(conn, "sender", "en")
        self.assertEqual(status, "error")
        self.assertIn("name has no owner", detail)
        # No cancel attempt for a non-timeout failure.
        self.assertEqual(conn.call_sync.call_count, 1)

    def test_Authorize_CancelsPendingCheck_WhenPolkitTimesOut(self):
        def call_sync(*args):
            if args[3] == "CheckAuthorization":
                raise GLib.Error.new_literal(
                    GLib.quark_from_string("g-io-error-quark"), "timed out", 5)
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
    def test_SetPrimary_ReturnsDistinctMessages_ForDenialVsServiceError(self):
        def run(status, detail):
            invocation = mock.Mock()
            with mock.patch.object(h, "_authorized",
                                   return_value=(status, detail)):
                h.handle_set_primary(mock.Mock(), "sender",
                                     ("0000:12:00.0", "en"), invocation)
            return invocation.return_value.call_args[0][0].unpack()

        denied = run("denied", "")
        error = run("error", "polkitd is not running")

        self.assertEqual(denied,
                         (False, "Authorization was denied. Nothing changed.",
                          "", False))
        self.assertEqual(
            error,
            (False, "Could not check authorization (polkitd is not running). "
                    "Nothing changed.", "", False))


class TriggerDrm(unittest.TestCase):
    def test_TriggerDrm_PassesSettleFlag(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h._trigger_drm()
        argv = run.call_args[0][0]
        self.assertEqual(
            argv,
            ["udevadm", "trigger", "--subsystem-match=drm", "--settle"])

    def test_TriggerDrm_ReturnsFalse_WhenTriggerTimesOut(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("udevadm", 30)):
            self.assertFalse(h._trigger_drm())


class ParseUdevCurrentTags(unittest.TestCase):
    def test_ParseUdevCurrentTags_ExtractsCurrentTagList_FromPropertyOutput(self):
        # udev prints CURRENT_TAGS colon-separated with a leading and
        # trailing colon, alongside the cumulative TAGS line.
        output = ("DEVPATH=/devices/pci/0000:12:00.0\n"
                  "TAGS=:mutter-device-preferred-primary:seat:uaccess:\n"
                  "CURRENT_TAGS=:mutter-device-preferred-primary:seat:uaccess:\n"
                  "ID_PATH=pci-0000:12:00.0\n")
        self.assertEqual(
            h.parse_udev_current_tags(output),
            ["mutter-device-preferred-primary", "seat", "uaccess"])

    def test_ParseUdevCurrentTags_ReturnsEmptyList_ForEmptyCurrentTagsLine(self):
        self.assertEqual(
            h.parse_udev_current_tags("DEVPATH=/x\nCURRENT_TAGS=\n"), [])
        self.assertEqual(h.parse_udev_current_tags("DEVPATH=/x\n"), [])

    def test_ParseUdevCurrentTags_IgnoresStaleTagInCumulativeTagsLine(self):
        # The cumulative TAGS line still carries the tag from a previous
        # rule, but the last uevent (rule no longer matching) set none.
        output = ("DEVPATH=/x\n"
                  "TAGS=:mutter-device-preferred-primary:seat:\n"
                  "CURRENT_TAGS=:seat:\n")
        self.assertEqual(
            h.parse_udev_current_tags(output), ["seat"])


class CardHasTag(unittest.TestCase):
    def test_CardHasTag_ReturnsTrue_WhenMutterTagInCurrentTags(self):
        with mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="DEVPATH=/x\n"
                       "TAGS=:mutter-device-preferred-primary:\n"
                       "CURRENT_TAGS=:mutter-device-preferred-primary:\n")
            self.assertTrue(h._card_has_tag("card1"))
        argv = run.call_args[0][0]
        self.assertIn("--path=/sys/class/drm/card1", argv)

    def test_CardHasTag_ReturnsFalse_WhenTagOnlyInStaleCumulativeTags(self):
        with mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="DEVPATH=/x\n"
                       "TAGS=:mutter-device-preferred-primary:\n"
                       "CURRENT_TAGS=:seat:\n")
            self.assertFalse(h._card_has_tag("card1"))


class SetPrimaryRefresh(unittest.TestCase):
    """SetPrimary verifies the tag is present in the card's current udev
    tags (what mutter reads at session start) after the settled refresh."""

    def test_SetPrimary_ReturnsRefreshFailed_WhenTagMissingAfterRefresh(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp5-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        def run(argv, **kwargs):
            # udevadm info (the tag query) reports no current tag; everything
            # else (verify, reload-rules, settled trigger) succeeds.
            if argv[:2] == ["udevadm", "info"]:
                return mock.Mock(returncode=0, stderr="",
                                 stdout="DEVPATH=/x\nCURRENT_TAGS=\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "rule_conflicts", return_value=[]), \
             mock.patch.object(h, "_resolve_gpu",
                               return_value=("card1", "0x1002", "0x7550")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("os.chown"), \
             mock.patch("subprocess.run", side_effect=run):
            h.handle_set_primary(mock.Mock(), "sender",
                                 ("0000:12:00.0", "en"), invocation)
        shutil.rmtree(tmp)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(
            result,
            (True, "Rule written for 0000:12:00.0. The udev refresh failed; "
                   "a reboot is required.", "", False))


class RefreshUdev(unittest.TestCase):
    def test_Refresh_SkipsGracefully_WhenUdevadmMissing(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch("subprocess.run") as run:
            self.assertTrue(h._refresh_udev("card1"))
        run.assert_not_called()


class SetPrimaryTransaction(unittest.TestCase):
    """SetPrimary write-transaction failure paths and the idempotent
    no-write path."""

    PCI = "0000:12:00.0"

    def test_SetPrimary_ReturnsVerifyRejected_WhenUdevadmVerifyFails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        def run(argv, **kwargs):
            # udevadm verify rejects the new rule; nothing else runs.
            if argv[:2] == ["udevadm", "verify"]:
                return mock.Mock(returncode=1, stdout="", stderr="bad rule")
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "rule_conflicts", return_value=[]), \
             mock.patch.object(h, "_resolve_gpu",
                               return_value=("card1", "0x1002", "0x7550")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("os.chown"), \
             mock.patch("subprocess.run", side_effect=run):
            h.handle_set_primary(mock.Mock(), "sender",
                                 (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(result, (False,
                                 "udevadm verify rejected the rule; "
                                 "nothing changed.",
                                 "", False))
        self.assertFalse(os.path.exists(managed))
        shutil.rmtree(tmp)

    def test_SetPrimary_ReturnsBackupFailure_WhenCopyFails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        old = 'SUBSYSTEMS=="pci", KERNELS=="0000:00:00.0"\n'
        with open(managed, "w", encoding="utf-8") as f:
            f.write(old)

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "rule_conflicts", return_value=[]), \
             mock.patch.object(h, "_resolve_gpu",
                               return_value=("card1", "0x1002", "0x7550")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("os.chown"), \
             mock.patch("shutil.copy2", side_effect=OSError("no space")), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h.handle_set_primary(mock.Mock(), "sender",
                                 (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(result, (False,
                                 "Backup of the existing rule failed; "
                                 "nothing changed.",
                                 "", False))
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)
        shutil.rmtree(tmp)

    def test_SetPrimary_ReturnsNothingChanged_WhenRenameFails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        old = 'SUBSYSTEMS=="pci", KERNELS=="0000:00:00.0"\n'
        with open(managed, "w", encoding="utf-8") as f:
            f.write(old)

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "rule_conflicts", return_value=[]), \
             mock.patch.object(h, "_resolve_gpu",
                               return_value=("card1", "0x1002", "0x7550")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("os.chown"), \
             mock.patch("subprocess.run") as run, \
             mock.patch("os.rename",
                        side_effect=OSError("read-only filesystem")):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            h.handle_set_primary(mock.Mock(), "sender",
                                 (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(result, (False,
                                 "Failed to write the managed rule; "
                                 "nothing changed.",
                                 "", False))
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)
        shutil.rmtree(tmp)

    def test_SetPrimary_SkipsWrite_WhenRuleAlreadyCurrent(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        content = h._rule_text(self.PCI, "0x1002", "0x7550")
        with open(managed, "w", encoding="utf-8") as f:
            f.write(content)

        def run(argv, **kwargs):
            # reload-rules and the settled trigger succeed; the tag query
            # reports the mutter tag in the card's current tags.
            if argv[:2] == ["udevadm", "info"]:
                return mock.Mock(returncode=0, stderr="",
                                 stdout="DEVPATH=/x\n"
                                        "CURRENT_TAGS=:mutter-device-preferred-primary:\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "rule_conflicts", return_value=[]), \
             mock.patch.object(h, "_resolve_gpu",
                               return_value=("card1", "0x1002", "0x7550")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("subprocess.run", side_effect=run):
            h.handle_set_primary(mock.Mock(), "sender",
                                 (self.PCI, "en"), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(result, (True,
                                 "The rule is already set for "
                                 "0000:12:00.0. Udev rules reloaded.",
                                 "", True))
        with open(managed, encoding="utf-8") as f:
            self.assertEqual(f.read(), content)
        # The write path was skipped entirely: no backup, no temp file.
        self.assertEqual(os.listdir(tmp), ["61-mutter-primary-gpu.rules"])
        shutil.rmtree(tmp)


class SetAutoTransaction(unittest.TestCase):
    def test_SetAuto_ReturnsNothingRemoved_WhenUnlinkFails(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp10-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")
        with open(managed, "w", encoding="utf-8") as f:
            f.write("old rule\n")

        invocation = mock.Mock()
        with mock.patch.object(h, "_authorized",
                               return_value=("authorized", "")), \
             mock.patch.object(h, "MANAGED_RULE", managed), \
             mock.patch("os.chown"), \
             mock.patch("os.unlink",
                        side_effect=OSError("read-only filesystem")):
            h.handle_set_auto(mock.Mock(), "sender", ("en",), invocation)
        result = invocation.return_value.call_args[0][0].unpack()
        self.assertEqual(result, (False,
                                 "Failed to remove the managed rule; "
                                 "nothing changed.",
                                 False))
        self.assertTrue(os.path.exists(managed))
        shutil.rmtree(tmp)


class BusContract(unittest.TestCase):
    def test_BusContract_MatchesHelperServiceNameAndPathAndInterface(self):
        self.assertEqual(bus.HELPER_NAME, h.SERVICE_NAME)
        self.assertEqual(bus.HELPER_PATH, h.OBJECT_PATH)
        self.assertEqual(bus.HELPER_IFACE, h.INTERFACE)

    def test_BusContract_MatchesIntrospection_MethodsAndSignatures(self):
        """INTROSPECTION_XML must declare the signatures the app side
        (bus.py) actually sends and expects, method for method."""

        def flat(sig: str) -> str:
            # A variant's format string is parenthesized for tuples, while
            # the introspection's per-arg concatenation is not.
            if sig.startswith("(") and sig.endswith(")"):
                return sig[1:-1]
            return sig

        expected = {"Status": ("s", "ss"),
                    "SetPrimary": ("ss", "bssb"),
                    "SetAuto": ("s", "bsb")}
        node = Gio.DBusNodeInfo.new_for_xml(h.INTROSPECTION_XML)
        iface = node.interfaces[0]
        self.assertEqual(iface.name, h.INTERFACE)
        self.assertEqual(iface.name, bus.HELPER_IFACE)
        for name, signatures in expected.items():
            method = iface.lookup_method(name)
            declared = ("".join(a.signature for a in method.in_args),
                        "".join(a.signature for a in method.out_args))
            self.assertEqual(declared, signatures, name)

        sent = []
        connection = mock.Mock()
        connection.call.side_effect = lambda *args: sent.append(args)
        with mock.patch.object(Gio, "bus_get_sync",
                              return_value=connection):
            bus.set_primary("0000:12:00.0", "en", lambda *a: None)
            bus.set_auto("en", lambda *a: None)
        self.assertEqual(len(sent), 2)
        for args, (method_name, signatures) in zip(
                sent, (("SetPrimary", expected["SetPrimary"]),
                       ("SetAuto", expected["SetAuto"]))):
            bus_name, bus_path, iface_name, method, params, reply_type = args[:6]
            self.assertEqual((bus_name, bus_path, iface_name, method),
                             (bus.HELPER_NAME, bus.HELPER_PATH,
                              bus.HELPER_IFACE, method_name))
            self.assertEqual(flat(params.get_type_string()), signatures[0])
            self.assertEqual(flat(reply_type.dup_string()), signatures[1])


if __name__ == "__main__":
    unittest.main()
