"""Regression tests for the GPU row builder in the GTK UI.

GTK widgets can be constructed headless (no display), so the row builder is
testable here. Each GPU row is a two-line block: the name on its own line (the
radio's child, so it is the row's accessible name and is clickable), and the
PCI address plus status badges on a second line. This keeps the window from
stretching to name + PCI + badges on one line.
"""

import unittest

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from gpu_switcher import main


class GpuRowBuildTest(unittest.TestCase):
    def test_gpu_row_builds_name_and_detail_lines_when_sub_and_badges_present(
        self,
    ) -> None:
        radio = Gtk.CheckButton()
        row = main._gpu_row(
            radio,
            "Radeon RX 9070",
            "PCI 0000:03:00.0 · /dev/dri/card0",
            ["In use now"],
        )
        self.assertIsInstance(row, Gtk.Box)
        # Two lines: the radio (its child is the name) and the detail line.
        self.assertIs(row.get_first_child(), radio)
        self.assertIsNotNone(row.get_last_child())
        self.assertIsNot(row.get_first_child(), row.get_last_child())

    def test_gpu_row_sets_name_label_as_radio_child(self) -> None:
        # The name label is the radio's child: it is the row's accessible
        # name (Orca announces it) and clicking it toggles the selection.
        radio = Gtk.CheckButton()
        main._gpu_row(radio, "Radeon RX 9070", "", [])
        name_label = radio.get_child()
        self.assertIsInstance(name_label, Gtk.Label)
        self.assertEqual(name_label.get_text(), "Radeon RX 9070")

    def test_gpu_row_omits_detail_line_when_no_sub_and_no_badges(self) -> None:
        # With neither a subtitle nor badges only the name line remains.
        radio = Gtk.CheckButton()
        row = main._gpu_row(radio, "Radeon RX 9070", "", [])
        self.assertIs(row.get_first_child(), row.get_last_child())
        self.assertIs(row.get_first_child(), radio)


if __name__ == "__main__":
    unittest.main()
