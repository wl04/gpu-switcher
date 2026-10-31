"""Unit tests for the gnome-shell journal line matching in detect.py (M3.2).

gnome-shell logs the primary selection once per session start, in two forms:
rule mode ("selected primary given udev rule") and auto mode
("selected as primary"). Both must yield the card name; the previous
implementation only matched the rule-mode form, so auto mode reported the
running primary as unknown.
"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
