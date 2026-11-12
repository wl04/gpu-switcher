"""Tests for the GTK row builder, the assembled window, and the window's own rule.

A GPU row is two lines: the name (the radio's child, so it is the accessible name
and is clickable) and below it the PCI address plus badges, which keeps the window
narrow. What a row *says* is decided in decisions.py and tested there; this file
covers what the window does with those answers - the banner it shows, what it does
with a refusal to Apply, which buttons stay live - and the one decision that
belongs to a window rather than to a situation: when coming back to it should read
the world again.

The window tests need a display and skip without one; CI runs the suite under xvfb.
"""

import contextlib
import os
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from gpu_switcher import decisions, i18n, main, selection
from gpu_switcher.core.connectors import DisplayState
from gpu_switcher.detect import Detection, Gpu, RuleState

CARD1 = "card1"
CARD2 = "card2"
CARD1_PCI = "0000:03:00.0"
CARD2_PCI = "0000:04:00.0"
# A PCI the managed rule pins while no detected GPU has it.
CARD3_PCI = "0000:05:00.0"


def _text(msgid: str) -> str:
    return i18n._(msgid)


def _gpu(
    card: str = CARD1,
    pci: str = CARD1_PCI,
    name: str = "Radeon RX 9070",
    in_use: bool = False,
    preferred: bool = False,
    display_state: DisplayState = DisplayState.UNKNOWN,
    connected_displays: list[str] | None = None,
) -> Gpu:
    return Gpu(
        card=card,
        node=f"/dev/dri/{card}",
        pci=pci,
        vendor="1002",
        device="7550",
        name=name,
        in_use=in_use,
        preferred=preferred,
        display_state=display_state,
        connected_displays=list(connected_displays or []),
    )


def _detection(
    gpus: list[Gpu] | None = None,
    running_card: str | None = CARD1,
    preferred_pci: str | None = None,
    conflicts: list[str] | None = None,
    gnome_wayland: bool = True,
    rule_state: RuleState = RuleState.ABSENT,
) -> Detection:
    return Detection(
        gpus=gpus if gpus is not None else [_gpu(), _gpu(CARD2, CARD2_PCI)],
        running_card=running_card,
        running_card_error=None,
        preferred_pci=preferred_pci,
        conflicts=conflicts or [],
        gnome_wayland=gnome_wayland,
        rule_state=rule_state,
        rule_mtime=None,
        session_start=None,
        tag_applied=None,
    )


def _rule_pinning_an_absent_gpu() -> Detection:
    """A valid managed rule whose GPU is not currently detected."""
    return _detection(preferred_pci=CARD3_PCI, rule_state=RuleState.VALID)


def _label_texts(root: Gtk.Widget) -> list[str]:
    """Every label text under a widget, in display order."""
    found: list[str] = []

    def walk(node: Gtk.Widget) -> None:
        if isinstance(node, Gtk.Label):
            found.append(node.get_text())
        child = node.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(root)
    return found


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


class WindowBuildTest(unittest.TestCase):
    """The window assembled from a snapshot, with the decisions wired in.

    What is checked is the wiring, not the appearance: the banner shows the warning
    decisions.startup_banner chose, a refusal to Apply is presented to the user
    instead of sent to the helper, a selection that no longer exists re-reads the
    world, and the privileged buttons follow the session type.
    """

    app: main.GpuSwitcherApp
    _stack: contextlib.ExitStack

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("GPU_SWITCHER_GUI_TESTS") != "1":
            # A window needs a display, and in a normal shell the only display
            # available is the developer's live session, which is not where a test
            # belongs. These run on a virtual one (CI: xvfb-run with the flag).
            raise unittest.SkipTest(
                "window tests need a virtual display: "
                "GPU_SWITCHER_GUI_TESTS=1 xvfb-run -a make test"
            )
        if not Gtk.init_check():
            raise unittest.SkipTest("GTK could not open the display")
        # One application for the whole class: a second instance would fail to
        # export org.gtk.Application while the first still owns it on the bus.
        # Its detection thread gets a fixture instead of the machine.
        patcher = mock.patch.object(main.detect, "detect", return_value=_detection())
        cls._stack = contextlib.ExitStack()
        cls._stack.enter_context(patcher)
        cls.app = main.GpuSwitcherApp()
        cls.app.register()
        cls.app.activate()
        if cls.app.window is None:
            cls._stack.close()
            raise unittest.SkipTest("the application created no window")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.app.window is not None:
            cls.app.window.destroy()
        cls._stack.close()

    def build(self, detection: Detection) -> None:
        """Assemble the window content from this snapshot.

        The detection thread can only hand its result over in a running main loop,
        so the snapshot is given directly: every assertion below is then about this
        snapshot and not about the machine running the test.
        """
        self.app.detection = detection
        self.app._build_content()

    def banner_text(self) -> str:
        return self.app.banner_label.get_label()

    def test_window_shows_the_warning_about_a_rule_pinning_an_absent_gpu(self) -> None:
        detection = _rule_pinning_an_absent_gpu()
        self.build(detection)
        expected = decisions.startup_banner(detection)
        if expected is None:
            self.fail("a rule pinning an absent GPU has to be reported")
        text, _kind = expected
        self.assertTrue(self.app.banner.get_reveal_child())
        self.assertEqual(self.banner_text(), text)

    def test_window_shows_no_warning_when_the_snapshot_needs_no_resolving(self) -> None:
        self.build(_detection())
        self.assertFalse(self.app.banner.get_reveal_child())
        self.assertEqual(self.banner_text(), "")

    def test_window_names_both_conflicting_rules_of_the_same_snapshot(self) -> None:
        # The banner has room for one warning, but the GPU rows still mark the
        # card the rule pins, so nothing about the situation is lost.
        detection = _detection(
            gpus=[
                _gpu(CARD1, CARD1_PCI),
                _gpu(CARD2, CARD2_PCI, preferred=True),
            ],
            preferred_pci=CARD2_PCI,
            rule_state=RuleState.VALID,
            conflicts=["99-other.rules"],
        )
        self.build(detection)
        self.assertEqual(
            self.banner_text(),
            _text(
                "Another udev rule sets the preferred GPU: %s. "
                "Resolve it manually first."
            )
            % "99-other.rules",
        )
        self.assertIn(_text("Preferred (configured)"), _label_texts(self.app.rows_box))

    def test_window_has_one_selectable_row_per_detected_gpu(self) -> None:
        detection = _detection()
        self.build(detection)
        self.assertEqual(
            sorted(gpu.card for gpu in detection.gpus), sorted(self.app.radios_by_gpu)
        )
        # The auto row always exists: Reset and the revert path never reach for a
        # missing widget.
        self.app._select_radio(selection.SELECTION_AUTO)
        self.assertTrue(self.app.auto_radio.get_active())

    def test_window_shows_a_disabled_row_for_a_configured_gpu_it_cannot_select(
        self,
    ) -> None:
        detection = _rule_pinning_an_absent_gpu()
        self.build(detection)
        unavailable = self.app.unavailable_radio
        if unavailable is None:
            self.fail("the rule's GPU is absent, so its row has to be shown")
        self.assertFalse(unavailable.get_sensitive())
        self.assertIn(CARD3_PCI, " ".join(_label_texts(self.app.rows_box)))

    def test_window_marks_the_detected_card_that_has_no_display_connected(self) -> None:
        detection = _detection(
            gpus=[
                _gpu(CARD1, CARD1_PCI),
                _gpu(CARD2, CARD2_PCI, display_state=DisplayState.NONE),
            ],
            preferred_pci=CARD2_PCI,
            rule_state=RuleState.VALID,
        )
        self.build(detection)
        self.assertIn(_text("No display connected"), _label_texts(self.app.rows_box))

    def test_apply_of_an_undetected_configured_gpu_is_explained_and_never_sent(
        self,
    ) -> None:
        # The user selected the rule's row. There is nothing to write, and falling
        # back to auto silently would remove a rule they did not ask about.
        detection = _rule_pinning_an_absent_gpu()
        self.build(detection)
        self.app.selected_card = selection.SELECTION_UNAVAILABLE
        expected = decisions.apply_decision(detection, selection.SELECTION_UNAVAILABLE)
        if expected.banner is None:
            self.fail("a refusal has to carry the text it shows")
        expected_text, _kind = expected.banner
        with (
            mock.patch.object(main.bus, "set_primary") as set_primary,
            mock.patch.object(main.bus, "set_auto") as set_auto,
        ):
            self.app._on_apply(None)
            set_primary.assert_not_called()
            set_auto.assert_not_called()
        self.assertEqual(self.banner_text(), expected_text)

    def test_apply_of_a_card_that_vanished_rechecks_instead_of_writing(self) -> None:
        detection = _detection(gpus=[_gpu(CARD1, CARD1_PCI)])
        self.build(detection)
        self.app.selected_card = CARD2  # selected while it was detected, now gone
        with (
            mock.patch.object(main.bus, "set_primary") as set_primary,
            mock.patch.object(self.app, "_refresh") as refresh,
        ):
            self.app._on_apply(None)
            set_primary.assert_not_called()
        refresh.assert_called_once_with()
        self.assertEqual(
            self.banner_text(),
            _text("The selected GPU is no longer detected; re-checking…"),
        )

    def test_window_keeps_privileged_actions_off_outside_gnome_wayland(self) -> None:
        self.build(_detection(gnome_wayland=False))
        for button in (self.app.apply_btn, self.app.reset_btn, self.app.reboot_btn):
            self.assertFalse(button.get_sensitive())
        # Reading the world again asks nothing of polkit, so it stays available.
        self.assertTrue(self.app.refresh_btn.get_sensitive())

    def test_window_warns_in_the_confirmation_before_switching_to_a_free_card(
        self,
    ) -> None:
        detection = _detection(
            gpus=[
                _gpu(CARD1, CARD1_PCI),
                _gpu(CARD2, CARD2_PCI, display_state=DisplayState.NONE),
            ],
            preferred_pci=CARD1_PCI,
            rule_state=RuleState.VALID,
        )
        self.build(detection)
        self.app.selected_card = CARD2
        decision = decisions.apply_decision(detection, CARD2)
        if decision.confirm_text is None:
            self.fail("a selectable GPU has to produce a confirmation")
        asked: list[str] = []

        def fake_confirm(text: str, label: str, on_result) -> None:
            asked.append(text)
            on_result(False)  # cancelled: nothing is sent

        with (
            mock.patch.object(main.bus, "set_primary") as set_primary,
            mock.patch.object(self.app, "_confirm", side_effect=fake_confirm),
        ):
            self.app._on_apply(None)
            set_primary.assert_not_called()
        self.assertEqual(asked, [decision.confirm_text])
        warning = decisions.apply_warning(
            _gpu(CARD2, CARD2_PCI, display_state=DisplayState.NONE)
        )
        if warning is None:
            self.fail("a card with no display connected has to be warned about")
        self.assertTrue(asked[0].endswith(warning))


if __name__ == "__main__":
    unittest.main()
