"""Unit tests for the root D-Bus helper (helper/gpu_switcher_helper.py).

Covers the pure PCI-format validation, the /sys resolution failure paths
(stubbed filesystem), the managed rule line format (golden string matching
the reference script mutter-primary-gpu.sh), the polkit authorization
(subject binding, cancellation id, result statuses — mocked D-Bus), the
distinct authorization-failure messages of SetPrimary, the missing-catalog
translator fallback, and the IPC contract agreement between the app side
(gpu_switcher/bus.py) and the helper side.
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


class Translator(unittest.TestCase):
    def test_Translator_ReturnsIdentity_WhenCatalogMissing(self):
        with mock.patch("helper.gpu_switcher_helper.LOCALE_BASE",
                        "/nonexistent-gpu-switcher-locale"):
            tr = h._translator("ru")
        self.assertEqual(tr("No catalog here"), "No catalog here")


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


class ParseUdevTags(unittest.TestCase):
    def test_ParseUdevTags_ExtractsTagList_FromPropertyOutput(self):
        # udevadm info prints KEY=VALUE lines; TAGS is comma-separated.
        output = ("DEVPATH=/devices/pci/0000:12:00.0\n"
                  "TAGS=mutter-device-preferred-primary,seat,uaccess\n"
                  "ID_PATH=pci-0000:12:00.0\n")
        self.assertEqual(
            h.parse_udev_tags(output),
            ["mutter-device-preferred-primary", "seat", "uaccess"])

    def test_ParseUdevTags_ReturnsEmptyList_ForEmptyTagsLine(self):
        self.assertEqual(h.parse_udev_tags("DEVPATH=/x\nTAGS=\n"), [])
        self.assertEqual(h.parse_udev_tags("DEVPATH=/x\n"), [])


class CardHasTag(unittest.TestCase):
    def test_CardHasTag_ReturnsTrue_WhenMutterTagInTags(self):
        with mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="DEVPATH=/x\nTAGS=mutter-device-preferred-primary\n")
            self.assertTrue(h._card_has_tag("card1"))
        argv = run.call_args[0][0]
        self.assertIn("--path=/sys/class/drm/card1", argv)

    def test_CardHasTag_ReturnsFalse_WhenTagMissing(self):
        with mock.patch("shutil.which", return_value="/usr/bin/udevadm"), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="DEVPATH=/x\nTAGS=seat\n")
            self.assertFalse(h._card_has_tag("card1"))


class SetPrimaryRefresh(unittest.TestCase):
    """SetPrimary verifies the tag is present in the card's udev tags
    after the settled refresh."""

    def test_SetPrimary_ReturnsRefreshFailed_WhenTagMissingAfterRefresh(self):
        tmp = tempfile.mkdtemp(prefix="gpu-switcher-wp5-")
        managed = os.path.join(tmp, "61-mutter-primary-gpu.rules")

        def run(argv, **kwargs):
            # udevadm info (the tag query) reports no tag; everything
            # else (verify, reload-rules, settled trigger) succeeds.
            if argv[:2] == ["udevadm", "info"]:
                return mock.Mock(returncode=0, stderr="",
                                 stdout="DEVPATH=/x\nTAGS=\n")
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


class BusContract(unittest.TestCase):
    def test_BusContract_MatchesHelperServiceNameAndPathAndInterface(self):
        self.assertEqual(bus.HELPER_NAME, h.SERVICE_NAME)
        self.assertEqual(bus.HELPER_PATH, h.OBJECT_PATH)
        self.assertEqual(bus.HELPER_IFACE, h.INTERFACE)


if __name__ == "__main__":
    unittest.main()
