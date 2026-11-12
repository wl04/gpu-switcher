"""Tests for the GTK row builder and the window's own re-detection rule.

A GPU row is two lines: the name (the radio's child, so it is the accessible name
and is clickable) and below it the PCI address plus badges, which keeps the window
narrow. What the row *says* is decided in decisions.py and tested there; this file
covers how the window assembles it, and the one decision that belongs to a window
rather than to a situation: when coming back to it should read the world again.
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


class RecheckOnReturn(unittest.TestCase):
    """Deciding whether returning to the window should read the world again.

    Detection is a snapshot: a monitor gets plugged in, another tool changes the
    rule, a helper call from elsewhere lands. Re-reading on every focus change
    would mean sysfs traffic while the user merely moves the mouse, and re-reading
    in the middle of an operation would rebuild the window under their hands.
    """

    def test_should_recheck_reads_again_after_a_stale_snapshot(self) -> None:
        self.assertTrue(main.should_recheck(60.0, False))

    def test_should_recheck_keeps_a_snapshot_that_is_still_fresh(self) -> None:
        self.assertFalse(main.should_recheck(2.0, False))

    def test_should_recheck_waits_while_an_operation_is_in_flight(self) -> None:
        # Re-detection rebuilds the content: it must not happen while a
        # confirmation is open or a helper call is waiting for its reply.
        self.assertFalse(main.should_recheck(60.0, True))

    def test_should_recheck_detects_when_nothing_has_been_read_yet(self) -> None:
        self.assertTrue(main.should_recheck(None, False))


if __name__ == "__main__":
    unittest.main()
