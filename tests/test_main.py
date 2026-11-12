"""Tests for the GTK UI's row builder and the decisions behind a GPU row.

Each GPU row is two lines: the name (the radio's child, so it is the accessible
name and is clickable) and below it the PCI address plus badges, which keeps the
window narrow.

Expected strings go through `i18n._` too: these tests are about which strings a
row contains and in what order, not about which language the catalog answered in.
"""

import unittest

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from gpu_switcher import i18n, main
from gpu_switcher.core.connectors import DisplayState
from gpu_switcher.detect import Gpu


def _text(msgid: str) -> str:
    return i18n._(msgid)


def _gpu(
    *,
    card: str = "card1",
    in_use: bool = False,
    preferred: bool = False,
    display_state: DisplayState = DisplayState.UNKNOWN,
    connected_displays: list[str] | None = None,
) -> Gpu:
    return Gpu(
        card=card,
        node=f"/dev/dri/{card}",
        pci="0000:03:00.0",
        vendor="1002",
        device="7550",
        name="Radeon RX 9070",
        in_use=in_use,
        preferred=preferred,
        display_state=display_state,
        connected_displays=list(connected_displays or []),
    )


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


class GpuRowBadges(unittest.TestCase):
    """What a GPU row advertises: the adapter GNOME runs on, the one that is
    configured, and what is plugged into this card's outputs.
    """

    def test_gpu_badges_report_no_display_when_the_outputs_of_a_card_are_all_free(
        self,
    ) -> None:
        self.assertIn(
            _text("No display connected"),
            main._gpu_badges(_gpu(display_state=DisplayState.NONE)),
        )

    def test_gpu_badges_name_the_outputs_that_report_a_monitor(self) -> None:
        # A port name is what a cable goes into, so the user can plug the monitor
        # into the card being switched to.
        badges = main._gpu_badges(
            _gpu(
                display_state=DisplayState.CONNECTED,
                connected_displays=["HDMI-A-1", "DP-2"],
            )
        )
        self.assertIn(_text("Display: %s") % "HDMI-A-1, DP-2", badges)

    def test_gpu_badges_say_nothing_about_displays_when_sysfs_could_not_say(
        self,
    ) -> None:
        # "No display" is something a user acts on; it is not printed when the
        # kernel did not answer.
        self.assertEqual(main._gpu_badges(_gpu()), [])

    def test_gpu_badges_keep_the_existing_marks_and_add_the_display_last(self) -> None:
        badges = main._gpu_badges(
            _gpu(in_use=True, preferred=True, display_state=DisplayState.NONE)
        )
        self.assertEqual(
            badges,
            [
                _text("In use now"),
                _text("Preferred (configured)"),
                _text("No display connected"),
            ],
        )


class ApplyWarning(unittest.TestCase):
    """The risk the confirmation dialog has to add before switching to a GPU."""

    def test_apply_warning_warns_when_the_target_has_no_display_connected(self) -> None:
        warning = main._apply_warning(_gpu(display_state=DisplayState.NONE))
        if warning is None:
            self.fail("a card whose outputs are all free must be warned about")
        # The card the user is about to switch to is named, so the warning cannot
        # be read as a general remark about displays.
        self.assertIn("Radeon RX 9070", warning)

    def test_apply_warning_says_nothing_when_the_target_has_a_display(self) -> None:
        self.assertIsNone(
            main._apply_warning(
                _gpu(
                    display_state=DisplayState.CONNECTED,
                    connected_displays=["HDMI-A-1"],
                )
            )
        )

    def test_apply_warning_says_nothing_when_the_display_answer_is_unknown(
        self,
    ) -> None:
        self.assertIsNone(main._apply_warning(_gpu()))


if __name__ == "__main__":
    unittest.main()
