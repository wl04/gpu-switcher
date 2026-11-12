"""Tests for the decisions the window is built from.

These are the answers a front-end needs before it draws anything: what a GPU row
advertises, what Apply is about to do and what it has to warn about, which
warning a snapshot deserves first, and whether the state row has anything to say.
The GTK window and a command line front-end both ask the same questions, so the
answers live here rather than inside a widget builder.

Expected strings go through `i18n._`: these tests are about which strings are
chosen and in what order, not about which language the catalog answered in.
"""

import unittest

from gpu_switcher import decisions, i18n
from gpu_switcher.core.connectors import DisplayState
from gpu_switcher.detect import Detection, Gpu, RuleState
from gpu_switcher.gpu_state import GpuState, presentation
from gpu_switcher.selection import SELECTION_AUTO, SELECTION_UNAVAILABLE
from gpu_switcher.state import BannerKind

CARD1 = "card1"
CARD2 = "card2"
CARD1_PCI = "0000:03:00.0"
CARD2_PCI = "0000:04:00.0"
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
    running_card_error: str | None = None,
    preferred_pci: str | None = None,
    conflicts: list[str] | None = None,
    gnome_wayland: bool = True,
    rule_state: RuleState = RuleState.ABSENT,
    tag_applied: bool | None = None,
) -> Detection:
    return Detection(
        gpus=gpus if gpus is not None else [_gpu(), _gpu(CARD2, CARD2_PCI)],
        running_card=running_card,
        running_card_error=running_card_error,
        preferred_pci=preferred_pci,
        conflicts=conflicts or [],
        gnome_wayland=gnome_wayland,
        rule_state=rule_state,
        rule_mtime=None,
        session_start=None,
        tag_applied=tag_applied,
    )


class GpuRowBadges(unittest.TestCase):
    """What a GPU row advertises: the adapter GNOME runs on, the one that is
    configured, and what is plugged into this card's outputs.
    """

    def test_gpu_badges_report_no_display_when_the_outputs_of_a_card_are_all_free(
        self,
    ) -> None:
        self.assertIn(
            _text("No display connected"),
            decisions.gpu_badges(_gpu(display_state=DisplayState.NONE)),
        )

    def test_gpu_badges_name_the_outputs_that_report_a_monitor(self) -> None:
        # A port name is what a cable goes into, so the user can plug the monitor
        # into the card being switched to.
        badges = decisions.gpu_badges(
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
        self.assertEqual(decisions.gpu_badges(_gpu()), [])

    def test_gpu_badges_keep_the_existing_marks_and_add_the_display_last(self) -> None:
        badges = decisions.gpu_badges(
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
    """The risk the confirmation has to add before switching to a GPU."""

    def test_apply_warning_warns_when_the_target_has_no_display_connected(self) -> None:
        warning = decisions.apply_warning(_gpu(display_state=DisplayState.NONE))
        if warning is None:
            self.fail("a card whose outputs are all free must be warned about")
        # The card the user is about to switch to is named, so the warning cannot
        # be read as a general remark about displays.
        self.assertIn("Radeon RX 9070", warning)

    def test_apply_warning_says_nothing_when_the_target_has_a_display(self) -> None:
        self.assertIsNone(
            decisions.apply_warning(
                _gpu(
                    display_state=DisplayState.CONNECTED,
                    connected_displays=["HDMI-A-1"],
                )
            )
        )

    def test_apply_warning_says_nothing_when_the_display_answer_is_unknown(
        self,
    ) -> None:
        self.assertIsNone(decisions.apply_warning(_gpu()))


class ApplyDecision(unittest.TestCase):
    """What Apply means for a selection, before any widget or prompt is involved.

    The answer is always one of three things: ask the user to confirm a target,
    explain that there is nothing to send, or re-read the world because the
    selection no longer matches what was detected.
    """

    def test_apply_decision_confirms_removing_the_rule_when_auto_is_selected(
        self,
    ) -> None:
        decision = decisions.apply_decision(_detection(), SELECTION_AUTO)
        self.assertIs(decision.kind, decisions.ApplyKind.CONFIRM)
        self.assertEqual(decision.target, SELECTION_AUTO)
        self.assertEqual(
            decision.confirm_text,
            _text(
                "Apply removes the managed udev rule, returning to automatic "
                "GPU selection.\n\n"
                "If a rule is present, a backup is saved. The change takes "
                "effect after a reboot, or after logging out and back in."
            ),
        )

    def test_apply_decision_names_the_gpu_and_its_pci_when_a_gpu_is_selected(
        self,
    ) -> None:
        detection = _detection(preferred_pci=CARD2_PCI)
        decision = decisions.apply_decision(detection, CARD2)
        self.assertIs(decision.kind, decisions.ApplyKind.CONFIRM)
        self.assertEqual(decision.target, CARD2_PCI)
        self.assertEqual(
            decision.confirm_text,
            _text(
                "Apply sets %s (PCI %s) as the primary display adapter.\n\n"
                "The managed udev rule is written; if a rule is present, a "
                "backup is saved. The change takes effect after a reboot, or "
                "after logging out and back in."
            )
            % ("Radeon RX 9070", CARD2_PCI),
        )

    def test_apply_decision_warns_in_the_confirmation_when_the_target_has_no_display(
        self,
    ) -> None:
        detection = _detection(
            gpus=[
                _gpu(),
                _gpu(CARD2, CARD2_PCI, display_state=DisplayState.NONE),
            ],
            preferred_pci=CARD2_PCI,
        )
        warning = decisions.apply_warning(
            _gpu(CARD2, CARD2_PCI, display_state=DisplayState.NONE)
        )
        if warning is None:
            self.fail("a card with no display connected must produce a warning")
        decision = decisions.apply_decision(detection, CARD2)
        if decision.confirm_text is None:
            self.fail("a selectable GPU must produce a confirmation")
        self.assertTrue(decision.confirm_text.endswith(warning))

    def test_apply_decision_explains_when_the_configured_gpu_is_not_detected(
        self,
    ) -> None:
        # The rule pins a PCI no detected GPU carries: there is nothing to send,
        # and silently falling back to auto would rewrite the rule.
        detection = _detection(preferred_pci=CARD3_PCI, rule_state=RuleState.VALID)
        decision = decisions.apply_decision(detection, SELECTION_UNAVAILABLE)
        self.assertIs(decision.kind, decisions.ApplyKind.EXPLAIN)
        self.assertIsNone(decision.target)
        self.assertIsNone(decision.confirm_text)
        if decision.banner is None:
            self.fail("a refusal has to say why")
        text, kind = decision.banner
        self.assertIs(kind, BannerKind.WARNING)
        self.assertIn(CARD3_PCI, text)
        self.assertIn("Reset to auto", text)

    def test_apply_decision_rechecks_when_the_selected_card_is_no_longer_detected(
        self,
    ) -> None:
        # The card vanished after the rows were built. Applying a stale selection
        # would act on a GPU the snapshot no longer has.
        detection = _detection(gpus=[_gpu()])
        decision = decisions.apply_decision(detection, CARD2)
        self.assertIs(decision.kind, decisions.ApplyKind.RECHECK)
        self.assertIsNone(decision.target)
        self.assertIsNone(decision.confirm_text)
        if decision.banner is None:
            self.fail("re-checking has to be announced")
        text, kind = decision.banner
        self.assertIs(kind, BannerKind.WARNING)
        self.assertEqual(
            text, _text("The selected GPU is no longer detected; re-checking…")
        )

    def test_apply_decision_carries_no_confirmation_for_a_refusal(self) -> None:
        detection = _detection(preferred_pci=CARD3_PCI, rule_state=RuleState.VALID)
        decision = decisions.apply_decision(detection, SELECTION_UNAVAILABLE)
        self.assertIsNone(decision.confirm_text)


class StartupBanner(unittest.TestCase):
    """Which warning a snapshot deserves first.

    Several things can be wrong at once, and the window has room for one banner.
    The order is what a user can act on: a configured GPU that is not present has
    to be named before anything else, because every other reading of the state
    depends on it.
    """

    def test_startup_banner_names_an_undetected_configured_gpu_before_a_conflict(
        self,
    ) -> None:
        detection = _detection(
            preferred_pci=CARD3_PCI,
            rule_state=RuleState.VALID,
            conflicts=["99-other.rules"],
        )
        banner = decisions.startup_banner(detection)
        if banner is None:
            self.fail("a rule pinning an absent GPU has to be reported")
        text, kind = banner
        self.assertIs(kind, BannerKind.WARNING)
        self.assertIn(CARD3_PCI, text)
        self.assertNotIn("99-other.rules", text)

    def test_startup_banner_names_the_conflicting_rule_when_the_configured_gpu_is_present(
        self,
    ) -> None:
        detection = _detection(
            preferred_pci=CARD2_PCI,
            rule_state=RuleState.VALID,
            conflicts=["99-other.rules"],
        )
        banner = decisions.startup_banner(detection)
        if banner is None:
            self.fail("a foreign rule setting the tag has to be reported")
        text, kind = banner
        self.assertIs(kind, BannerKind.WARNING)
        self.assertEqual(
            text,
            _text(
                "Another udev rule sets the preferred GPU: %s. "
                "Resolve it manually first."
            )
            % "99-other.rules",
        )

    def test_startup_banner_reports_a_rule_that_cannot_be_interpreted(
        self,
    ) -> None:
        for rule_state in (RuleState.UNRECOGNIZED, RuleState.UNREADABLE):
            with self.subTest(rule_state=rule_state):
                banner = decisions.startup_banner(_detection(rule_state=rule_state))
                if banner is None:
                    self.fail(f"{rule_state} has to be reported")
                text, kind = banner
                self.assertIs(kind, BannerKind.WARNING)
                self.assertEqual(
                    text,
                    _text(
                        "The managed udev rule could not be interpreted, so the "
                        "configured GPU is unknown (this is not automatic "
                        "selection). Use 'Reset to auto' or Apply to rewrite it."
                    ),
                )

    def test_startup_banner_says_nothing_when_the_snapshot_is_clean(self) -> None:
        self.assertIsNone(decisions.startup_banner(_detection()))

    def test_startup_banner_says_nothing_while_the_rule_matches_the_selection(
        self,
    ) -> None:
        detection = _detection(preferred_pci=CARD2_PCI, rule_state=RuleState.VALID)
        self.assertIsNone(decisions.startup_banner(detection))


class StateStatus(unittest.TestCase):
    """Whether the state row has something to say, and how it should look."""

    def test_state_status_shows_nothing_when_the_state_has_no_text(self) -> None:
        # Outside GNOME Wayland the state row would only repeat the session warning.
        detection = _detection(gnome_wayland=False)
        self.assertIsNone(decisions.state_status(GpuState.NOT_APPLICABLE, detection))

    def test_state_status_hides_unknown_when_another_warning_already_names_the_rule(
        self,
    ) -> None:
        # The startup banner already explains the rule problem; a second line
        # saying "unknown" would only echo it.
        detection = _detection(preferred_pci=CARD3_PCI, conflicts=["99-other.rules"])
        self.assertIsNone(decisions.state_status(GpuState.UNKNOWN, detection))

    def test_state_status_hides_unknown_for_a_rule_that_cannot_be_read(self) -> None:
        detection = _detection(rule_state=RuleState.UNREADABLE)
        self.assertIsNone(decisions.state_status(GpuState.UNKNOWN, detection))

    def test_state_status_hides_unknown_for_a_rule_that_is_not_understood(self) -> None:
        detection = _detection(rule_state=RuleState.UNRECOGNIZED)
        self.assertIsNone(decisions.state_status(GpuState.UNKNOWN, detection))

    def test_state_status_hides_unknown_when_the_rule_pins_an_absent_gpu(self) -> None:
        detection = _detection(preferred_pci=CARD3_PCI, rule_state=RuleState.VALID)
        self.assertIsNone(decisions.state_status(GpuState.UNKNOWN, detection))

    def test_state_status_shows_unknown_when_nothing_else_explains_it(self) -> None:
        detection = _detection(running_card_error=None)
        status = decisions.state_status(GpuState.UNKNOWN, detection)
        if status is None:
            self.fail("an unexplained unknown state has to be shown")
        text, kind, icon = status
        self.assertIn("unknown", text)
        self.assertEqual(icon, presentation(GpuState.UNKNOWN)[1])
        self.assertIs(kind, BannerKind.INFO)

    def test_state_status_carries_the_style_and_icon_of_the_state(self) -> None:
        status = decisions.state_status(GpuState.PENDING_REBOOT, _detection())
        if status is None:
            self.fail("a pending change has to be shown")
        text, kind, icon = status
        self.assertEqual(kind, presentation(GpuState.PENDING_REBOOT)[0])
        self.assertEqual(icon, presentation(GpuState.PENDING_REBOOT)[1])
        self.assertEqual(text, _text("The change applies after a reboot."))


if __name__ == "__main__":
    unittest.main()
