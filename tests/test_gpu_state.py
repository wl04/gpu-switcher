"""Unit tests for the derived GPU state (gpu_state.py)."""

import os
import unittest

# gpu_switcher.i18n binds gettext at import time; pin the locale first.
os.environ["LC_ALL"] = "en"

from gpu_switcher import detect
from gpu_switcher.gpu_state import GpuState, derive_state, describe, presentation
from gpu_switcher.state import BannerKind, Outcome

CARD1_PCI = "0000:01:00.0"
CARD2_PCI = "0000:12:00.0"
SESSION_START = 1000.0
RULE_BEFORE_SESSION = 500.0
RULE_AFTER_SESSION = 1500.0


def _gpu(card: str, pci: str) -> detect.Gpu:
    return detect.Gpu(
        card=card,
        node=f"/dev/dri/{card}",
        pci=pci,
        vendor="1002",
        device="13c0",
        name=f"GPU {card}",
        in_use=False,
        preferred=False,
    )


def _detection(
    preferred_pci: str | None = CARD2_PCI,
    running_card: str | None = "card2",
    running_card_error: str | None = None,
    rule_state: detect.RuleState | None = None,
    rule_mtime: float | None = RULE_BEFORE_SESSION,
    conflicts: list[str] | None = None,
    gnome_wayland: bool = True,
) -> detect.Detection:
    if rule_state is None:
        rule_state = (
            detect.RuleState.VALID if preferred_pci else detect.RuleState.ABSENT
        )
    return detect.Detection(
        gpus=[_gpu("card1", CARD1_PCI), _gpu("card2", CARD2_PCI)],
        running_card=running_card,
        running_card_error=running_card_error,
        preferred_pci=preferred_pci,
        conflicts=conflicts or [],
        gnome_wayland=gnome_wayland,
        rule_state=rule_state,
        rule_mtime=rule_mtime,
        session_start=SESSION_START,
    )


class DeriveState(unittest.TestCase):
    def test_derive_state_returns_not_applicable_outside_gnome_wayland(self) -> None:
        detection = _detection(gnome_wayland=False)
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.NOT_APPLICABLE)

    def test_derive_state_returns_auto_when_no_rule_and_running_known(self) -> None:
        detection = _detection(preferred_pci=None, rule_mtime=None)
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.AUTO)

    def test_derive_state_returns_unknown_when_no_rule_and_running_unknown(
        self,
    ) -> None:
        detection = _detection(preferred_pci=None, rule_mtime=None, running_card=None)
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.UNKNOWN)

    def test_derive_state_returns_active_when_running_card_matches_rule(self) -> None:
        detection = _detection(running_card="card2")
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.ACTIVE)

    def test_derive_state_returns_ignored_when_old_rule_not_honored(self) -> None:
        detection = _detection(running_card="card1")
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.IGNORED)

    def test_derive_state_returns_pending_login_after_refreshed_apply(self) -> None:
        detection = _detection(running_card="card1", rule_mtime=RULE_AFTER_SESSION)
        self.assertIs(
            derive_state(detection, Outcome.success_refreshed),
            GpuState.PENDING_LOGIN,
        )

    def test_derive_state_returns_pending_reboot_after_failed_refresh(self) -> None:
        detection = _detection(running_card="card1", rule_mtime=RULE_AFTER_SESSION)
        self.assertIs(
            derive_state(detection, Outcome.success_refresh_failed),
            GpuState.PENDING_REBOOT,
        )

    def test_derive_state_returns_pending_reboot_when_rule_newer_after_app_restart(
        self,
    ) -> None:
        # The refresh result is not persisted: without it, advise a reboot.
        detection = _detection(running_card="card1", rule_mtime=RULE_AFTER_SESSION)
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.PENDING_REBOOT)

    def test_derive_state_returns_pending_after_reset_to_auto(self) -> None:
        detection = _detection(preferred_pci=None, rule_mtime=None)
        self.assertIs(
            derive_state(detection, Outcome.success_refreshed),
            GpuState.PENDING_LOGIN,
        )

    def test_derive_state_returns_active_when_new_rule_matches_running_card(
        self,
    ) -> None:
        detection = _detection(running_card="card2", rule_mtime=RULE_AFTER_SESSION)
        self.assertIs(
            derive_state(detection, Outcome.success_refreshed), GpuState.ACTIVE
        )

    def test_derive_state_returns_unknown_when_running_card_unreadable(self) -> None:
        detection = _detection(running_card=None, running_card_error="boom")
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.UNKNOWN)

    def test_derive_state_returns_pending_when_running_unknown_but_rule_is_new(
        self,
    ) -> None:
        detection = _detection(running_card=None, rule_mtime=RULE_AFTER_SESSION)
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.PENDING_REBOOT)

    def test_derive_state_returns_unknown_when_configured_gpu_not_detected(
        self,
    ) -> None:
        detection = _detection(preferred_pci="0000:99:00.0")
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.UNKNOWN)

    def test_derive_state_returns_unknown_when_rule_unrecognized(self) -> None:
        detection = _detection(
            preferred_pci=None, rule_state=detect.RuleState.UNRECOGNIZED
        )
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.UNKNOWN)

    def test_derive_state_returns_unknown_when_conflicting_rule_exists(self) -> None:
        detection = _detection(conflicts=["/etc/udev/rules.d/99-x.rules"])
        self.assertIs(derive_state(detection, Outcome.idle), GpuState.UNKNOWN)

    def test_derive_state_ignores_unknown_outcome_and_uses_disk_state(self) -> None:
        detection = _detection(running_card="card2")
        self.assertIs(derive_state(detection, Outcome.unknown), GpuState.ACTIVE)


class Presentation(unittest.TestCase):
    def test_presentation_defined_for_every_state(self) -> None:
        for state in GpuState:
            kind, icon = presentation(state)
            self.assertIsInstance(kind, BannerKind)
            self.assertTrue(icon.endswith("-symbolic"), state)

    def test_presentation_distinguishes_ignored_from_pending_reboot(self) -> None:
        self.assertNotEqual(
            presentation(GpuState.IGNORED), presentation(GpuState.PENDING_REBOOT)
        )


class Describe(unittest.TestCase):
    def test_describe_names_running_and_configured_gpu_when_ignored(self) -> None:
        detection = _detection(running_card="card1")
        text = describe(GpuState.IGNORED, detection)
        self.assertIn("GPU card1", text)
        self.assertIn("GPU card2", text)

    def test_describe_omits_gpu_names_when_active(self) -> None:
        text = describe(GpuState.ACTIVE, _detection())
        self.assertNotIn("GPU card", text)

    def test_describe_mentions_automatic_when_pending_reset(self) -> None:
        detection = _detection(preferred_pci=None, rule_mtime=None)
        text = describe(GpuState.PENDING_LOGIN, detection)
        self.assertIn("automatic", text.lower())

    def test_describe_reports_journal_error_when_unknown(self) -> None:
        detection = _detection(running_card=None, running_card_error="boom")
        self.assertIn("boom", describe(GpuState.UNKNOWN, detection))

    def test_describe_returns_empty_when_not_applicable(self) -> None:
        self.assertEqual(describe(GpuState.NOT_APPLICABLE, _detection()), "")


if __name__ == "__main__":
    unittest.main()
