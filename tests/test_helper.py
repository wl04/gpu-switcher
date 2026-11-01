"""Unit tests for the root D-Bus helper (helper/gpu_switcher_helper.py).

Covers the pure PCI-format validation, the /sys resolution failure paths
(stubbed filesystem), the managed rule line format (golden string matching
the reference script mutter-primary-gpu.sh), the missing-catalog translator
fallback, and the IPC contract agreement between the app side
(gpu_switcher/bus.py) and the helper side. External services (D-Bus, polkit)
are exercised by the manual end-to-end verification, not here.
"""
import unittest
from unittest import mock

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


class BusContract(unittest.TestCase):
    def test_BusContract_MatchesHelperServiceNameAndPathAndInterface(self):
        self.assertEqual(bus.HELPER_NAME, h.SERVICE_NAME)
        self.assertEqual(bus.HELPER_PATH, h.OBJECT_PATH)
        self.assertEqual(bus.HELPER_IFACE, h.INTERFACE)


if __name__ == "__main__":
    unittest.main()
