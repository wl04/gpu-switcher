"""Unit tests for the unprivileged detection in detect.py.

Covers the gnome-shell journal line matching, the pure parsing cores,
the journal error/absence distinction, and the session check. External
binaries (lspci, journalctl) are replaced by stubs in the unit tests;
the real commands are exercised by the manual end-to-end verification.
"""
import subprocess
import unittest
from unittest import mock

from gpu_switcher import detect


class PrimaryCardFromLine(unittest.TestCase):
    def test_PrimaryCardFromLine_ExtractsCard_FromRuleModeJournalLine(self):
        line = "GPU /dev/dri/card2 selected primary given udev rule"
        self.assertEqual(detect.primary_card_from_line(line), "card2")

    def test_PrimaryCardFromLine_ExtractsCard_FromAutoModeJournalLine(self):
        line = "Boot VGA GPU /dev/dri/card1 selected as primary"
        self.assertEqual(detect.primary_card_from_line(line), "card1")

    def test_PrimaryCardFromLine_ReturnsNone_WhenLineHasNoPrimarySelection(self):
        line = ("Added device '/dev/dri/card1' (amdgpu) "
                "using atomic mode setting.")
        self.assertIsNone(detect.primary_card_from_line(line))


class LastPrimaryCard(unittest.TestCase):
    def test_LastPrimaryCard_ReturnsCard_FromLastMatchingLine(self):
        lines = [
            "GPU /dev/dri/card1 selected primary given udev rule",
            "noise",
            "Boot VGA GPU /dev/dri/card2 selected as primary",
        ]
        self.assertEqual(detect.last_primary_card(lines), "card2")

    def test_LastPrimaryCard_ReturnsNone_WhenNoLineMatches(self):
        lines = ["noise", "more noise"]
        self.assertIsNone(detect.last_primary_card(lines))


class ManagedRulePciFromText(unittest.TestCase):
    def test_ManagedRulePciFromText_ExtractsPci_FromRuleText(self):
        text = ('KERNELS=="0000:12:00.0", ATTRS{vendor}=="0x1002"\n')
        self.assertEqual(
            detect.managed_rule_pci_from_text(text), "0000:12:00.0")

    def test_ManagedRulePciFromText_ReturnsNone_WhenRuleTextAbsent(self):
        self.assertIsNone(detect.managed_rule_pci_from_text(""))


class IsGnomeWayland(unittest.TestCase):
    def test_IsGnomeWayland_ReturnsTrue_WhenWaylandSessionAndShellPresent(self):
        self.assertTrue(detect.is_gnome_wayland("wayland", True))

    def test_IsGnomeWayland_ReturnsTrue_WhenWaylandCaseIsMixed(self):
        self.assertTrue(detect.is_gnome_wayland("WAYLAND", True))

    def test_IsGnomeWayland_ReturnsFalse_WhenX11Session(self):
        self.assertFalse(detect.is_gnome_wayland("x11", True))

    def test_IsGnomeWayland_ReturnsFalse_WhenShellMissing(self):
        self.assertFalse(detect.is_gnome_wayland("wayland", False))


class RunningPrimary(unittest.TestCase):
    """A failed journal read must be reported as an error, not masked as
    "no selection line"."""

    def _stub(self, **kwargs):
        return mock.patch("gpu_switcher.detect.subprocess.run", **kwargs)

    def test_RunningPrimary_ReturnsCard_WhenJournalHasSelectionLine(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="noise\nGPU /dev/dri/card2 selected primary given udev rule\n",
            stderr="")
        with self._stub(return_value=result):
            card, error = detect.running_primary()
        self.assertEqual(card, "card2")
        self.assertIsNone(error)

    def test_RunningPrimary_ReturnsNoError_WhenJournalHasNoSelectionLine(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr="")
        with self._stub(return_value=result):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)

    def test_RunningPrimary_ReturnsError_WhenJournalReadFails(self):
        with self._stub(side_effect=FileNotFoundError("journalctl")):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_RunningPrimary_ReturnsError_WhenJournalExitCodeIsNonZero(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="")
        with self._stub(return_value=result):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
