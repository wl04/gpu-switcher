"""Tests for the command-line front-end.

`gpu-switcher` with no arguments stays the window. Given a command, it answers the
two questions a terminal user has - what is here, and what is in effect - without
GTK, without polkit and without asking anything to change. The answers come from
the same detection snapshot and the same decisions the window uses, so the two
front-ends cannot disagree about what the machine is doing.

Exit codes are part of the contract: 0 when the answer was given, 2 when the
command line itself was wrong, 1 when nothing could be read.
"""

import io
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from gpu_switcher import cli, detect, gpu_state, i18n, state
from gpu_switcher.core.connectors import DisplayState
from gpu_switcher.core.udev import MANAGED_RULE

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
    name: str = "Advanced Micro Devices [AMD/ATI] Radeon RX 9070",
    in_use: bool = False,
    preferred: bool = False,
    display_state: DisplayState = DisplayState.UNKNOWN,
    connected_displays: list[str] | None = None,
) -> detect.Gpu:
    return detect.Gpu(
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
    gpus: list[detect.Gpu] | None = None,
    running_card: str | None = CARD1,
    running_card_error: str | None = None,
    preferred_pci: str | None = None,
    conflicts: list[str] | None = None,
    gnome_wayland: bool = True,
    rule_state: detect.RuleState = detect.RuleState.ABSENT,
    rule_mtime: float | None = None,
    session_start: float | None = None,
    tag_applied: bool | None = None,
) -> detect.Detection:
    return detect.Detection(
        gpus=gpus if gpus is not None else [_gpu(), _gpu(CARD2, CARD2_PCI)],
        running_card=running_card,
        running_card_error=running_card_error,
        preferred_pci=preferred_pci,
        conflicts=conflicts or [],
        gnome_wayland=gnome_wayland,
        rule_state=rule_state,
        rule_mtime=rule_mtime,
        session_start=session_start,
        tag_applied=tag_applied,
    )


class ParseRequest(unittest.TestCase):
    """What a command line asks for, before anything is read."""

    def test_parse_args_asks_for_the_window_when_no_command_is_given(self) -> None:
        request = cli.parse_args(["gpu-switcher"])
        self.assertIsNone(request.command)
        self.assertIsNone(request.error)

    def test_parse_args_recognises_each_read_only_command(self) -> None:
        for name in ("list", "status", "version"):
            with self.subTest(command=name):
                self.assertEqual(cli.parse_args(["gpu-switcher", name]).command, name)

    def test_parse_args_recognises_help(self) -> None:
        for name in ("-h", "--help"):
            with self.subTest(argument=name):
                self.assertEqual(cli.parse_args(["gpu-switcher", name]).command, "help")

    def test_parse_args_reports_an_unknown_command_without_running_anything(
        self,
    ) -> None:
        request = cli.parse_args(["gpu-switcher", "apply"])
        self.assertIsNone(request.command)
        if request.error is None:
            self.fail("an unknown command has to be reported as a usage error")
        self.assertIn("apply", request.error)

    def test_parse_args_reports_extra_words_after_a_command(self) -> None:
        # `gpu-switcher list now' is a mistake, not a second argument nobody read.
        request = cli.parse_args(["gpu-switcher", "list", "now"])
        self.assertIsNone(request.command)
        if request.error is None:
            self.fail("extra words have to be reported")
        self.assertIn("now", request.error)

    def test_parse_args_leaves_window_arguments_to_the_window(self) -> None:
        # GTK's own options (--gapplication-app-id and friends) are not CLI
        # commands: they belong to the application.
        request = cli.parse_args(["gpu-switcher", "--gapplication-app-id", "x"])
        self.assertIsNone(request.command)
        self.assertIsNone(request.error)


class ListRows(unittest.TestCase):
    """The rows that answer "what GPUs are here"."""

    def test_list_rows_name_the_card_its_pci_and_its_model(self) -> None:
        rows = cli.gpu_rows(_detection())
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith(CARD1))
        self.assertIn(CARD1_PCI, rows[0])
        self.assertIn("Radeon RX 9070", rows[0])

    def test_list_rows_mark_which_card_is_running_and_which_is_configured(
        self,
    ) -> None:
        detection = _detection(
            gpus=[
                _gpu(CARD1, CARD1_PCI, in_use=True),
                _gpu(CARD2, CARD2_PCI, preferred=True),
            ],
            running_card=CARD1,
            preferred_pci=CARD2_PCI,
        )
        rows = cli.gpu_rows(detection)
        self.assertIn(_text("In use now"), rows[0])
        self.assertNotIn(_text("Preferred (configured)"), rows[0])
        self.assertIn(_text("Preferred (configured)"), rows[1])
        self.assertNotIn(_text("In use now"), rows[1])

    def test_list_rows_name_the_outputs_that_report_a_monitor(self) -> None:
        detection = _detection(
            gpus=[_gpu(CARD1, CARD1_PCI, connected_displays=["HDMI-A-1", "DP-2"])]
        )
        self.assertIn(
            _text("Display: %s") % "HDMI-A-1, DP-2", cli.gpu_rows(detection)[0]
        )

    def test_list_rows_say_nothing_about_displays_when_sysfs_could_not_answer(
        self,
    ) -> None:
        rows = cli.gpu_rows(_detection())
        self.assertNotIn(_text("No display connected"), rows[0])
        self.assertNotIn("Display:", rows[0])

    def test_list_reports_that_nothing_was_detected(self) -> None:
        rows = cli.gpu_rows(_detection(gpus=[]))
        self.assertEqual(rows, [_text("No GPUs detected.")])


class StatusLines(unittest.TestCase):
    """The answer to "what is in effect on this machine"."""

    def test_status_names_the_pci_the_managed_rule_pins(self) -> None:
        lines = cli.status_lines(
            _detection(
                gpus=[_gpu(CARD1, CARD1_PCI, preferred=True)],
                preferred_pci=CARD1_PCI,
                rule_state=detect.RuleState.VALID,
            )
        )
        self.assertIn(_text("Managed rule sets PCI %s") % CARD1_PCI, lines)

    def test_status_says_there_is_no_rule_when_selection_is_automatic(self) -> None:
        lines = cli.status_lines(_detection(rule_state=detect.RuleState.ABSENT))
        self.assertIn(_text("No managed rule (auto selection)."), lines)

    def test_status_names_the_rule_it_cannot_read(self) -> None:
        lines = cli.status_lines(_detection(rule_state=detect.RuleState.UNREADABLE))
        self.assertIn(
            _text(
                "Could not read the udev rule %s. It might set the preferred GPU; "
                "make it readable or remove it."
            )
            % MANAGED_RULE,
            lines,
        )

    def test_status_says_the_rule_on_disk_is_not_understood(self) -> None:
        lines = cli.status_lines(_detection(rule_state=detect.RuleState.UNRECOGNIZED))
        self.assertIn(
            _text(
                "The managed udev rule could not be interpreted, so the "
                "configured GPU is unknown (this is not automatic "
                "selection). Use 'Reset to auto' or Apply to rewrite it."
            ),
            lines,
        )

    def test_status_names_the_other_rule_that_sets_the_tag(self) -> None:
        lines = cli.status_lines(_detection(conflicts=["99-other.rules"]))
        self.assertIn(
            _text(
                "Another udev rule sets the preferred GPU: %s. "
                "Resolve it manually first."
            )
            % "99-other.rules",
            lines,
        )

    def test_status_says_udev_applies_the_tag_on_the_configured_card(self) -> None:
        lines = cli.status_lines(
            _detection(
                gpus=[_gpu(CARD1, CARD1_PCI, preferred=True)],
                preferred_pci=CARD1_PCI,
                rule_state=detect.RuleState.VALID,
                tag_applied=True,
            )
        )
        self.assertIn(_text("udev applies the preferred-GPU tag on %s.") % CARD1, lines)

    def test_status_says_udev_does_not_apply_the_tag(self) -> None:
        lines = cli.status_lines(
            _detection(
                gpus=[_gpu(CARD1, CARD1_PCI, preferred=True)],
                preferred_pci=CARD1_PCI,
                rule_state=detect.RuleState.VALID,
                tag_applied=False,
            )
        )
        self.assertIn(
            _text("udev does not apply the preferred-GPU tag on %s.") % CARD1, lines
        )

    def test_status_says_udev_was_not_asked_when_nothing_is_configured(self) -> None:
        lines = cli.status_lines(_detection(tag_applied=None))
        self.assertFalse(any("preferred-GPU tag" in line for line in lines), lines)

    def test_status_names_the_running_primary_by_model_and_card(self) -> None:
        lines = cli.status_lines(_detection(running_card=CARD2))
        self.assertIn(_text("Running primary: %s") % f"{CARD2_PCI} ({CARD2})", lines)

    def test_status_reports_an_unknown_running_primary_as_unknown(self) -> None:
        lines = cli.status_lines(_detection(running_card=None))
        self.assertIn(
            _text("Running primary: unknown (verify in the journal after restart)."),
            lines,
        )

    def test_status_reports_the_journal_error_that_left_the_primary_unknown(
        self,
    ) -> None:
        lines = cli.status_lines(
            _detection(running_card=None, running_card_error="no journal readable")
        )
        self.assertIn(
            _text("Running primary: could not read the session journal (%s).")
            % "no journal readable",
            lines,
        )

    def test_status_states_the_derived_state_in_words(self) -> None:
        # The state the CLI states is derived with Outcome.idle, the startup
        # state of a session that ran no operation: a terminal read cannot claim
        # an Apply it did not perform.
        detection = _detection(
            gpus=[_gpu(CARD1, CARD1_PCI, preferred=True, in_use=True)],
            running_card=CARD1,
            preferred_pci=CARD1_PCI,
            rule_state=detect.RuleState.VALID,
        )
        self.assertIn(
            _text("GNOME is using the configured GPU."), cli.status_lines(detection)
        )
        self.assertEqual(
            gpu_state.derive_state(detection, state.Outcome.idle),
            gpu_state.GpuState.ACTIVE,
        )

    def test_status_says_a_rule_written_this_session_still_needs_a_reboot(self) -> None:
        # A CLI read has no Apply/Reset behind it, so the state comes from the
        # disk and session evidence alone: a rule newer than this session has not
        # been applied by udev yet.
        lines = cli.status_lines(
            _detection(
                gpus=[_gpu(CARD1, CARD1_PCI, preferred=True)],
                running_card=CARD2,
                preferred_pci=CARD1_PCI,
                rule_state=detect.RuleState.VALID,
                rule_mtime=2000.0,
                session_start=1000.0,
            )
        )
        self.assertIn(_text("The change applies after a reboot."), lines)

    def test_status_says_the_tool_does_not_apply_outside_gnome_wayland(self) -> None:
        lines = cli.status_lines(_detection(gnome_wayland=False))
        self.assertIn(
            _text(
                "Not running in a GNOME Wayland session - this build only "
                "supports GNOME."
            ),
            lines,
        )

    def test_status_does_not_state_a_derived_state_when_a_rule_problem_is_named(
        self,
    ) -> None:
        # "unknown" under a named rule problem repeats the diagnosis without
        # adding to it, and it contradicts the running-primary line the status
        # already gave: decisions.state_status hides it in the window for the
        # same reason.
        lines = cli.status_lines(
            _detection(preferred_pci=CARD2_PCI, conflicts=["99-other.rules"])
        )
        self.assertNotIn(
            _text("Running primary: unknown (verify in the journal after restart)."),
            lines,
        )


class RunCommand(unittest.TestCase):
    """Running a command: what it prints, where, and with which exit code."""

    def _run(
        self, argv: list[str], detection: detect.Detection | None = None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        detect_mock = mock.Mock(return_value=detection if detection else _detection())
        with mock.patch.object(cli, "detect", mock.Mock(detect=detect_mock)):
            code = cli.run(["gpu-switcher", *argv], stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_run_lists_the_detected_gpus_and_exits_zero(self) -> None:
        code, out, err = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertIn(CARD1_PCI, out)
        self.assertIn(CARD2_PCI, out)
        self.assertEqual(err, "")

    def test_run_describes_the_situation_for_status_and_exits_zero(self) -> None:
        code, out, _err = self._run(
            ["status"],
            _detection(
                gpus=[_gpu(CARD1, CARD1_PCI, preferred=True, in_use=True)],
                running_card=CARD1,
                preferred_pci=CARD1_PCI,
                rule_state=detect.RuleState.VALID,
                tag_applied=True,
            ),
        )
        self.assertEqual(code, 0)
        self.assertIn(_text("Managed rule sets PCI %s") % CARD1_PCI, out)
        self.assertIn(_text("udev applies the preferred-GPU tag on %s.") % CARD1, out)
        self.assertIn(_text("GNOME is using the configured GPU."), out)

    def test_run_prints_the_package_version_and_exits_zero(self) -> None:
        import gpu_switcher

        code, out, _err = self._run(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"gpu-switcher {gpu_switcher.__version__}")

    def test_run_prints_the_commands_for_help_and_exits_zero(self) -> None:
        code, out, _err = self._run(["--help"])
        self.assertEqual(code, 0)
        for name in ("list", "status", "version"):
            self.assertIn(name, out)

    def test_run_rejects_an_unknown_command_with_a_usage_error(self) -> None:
        code, out, err = self._run(["apply"])
        self.assertEqual(code, 2)
        self.assertIn("apply", err)
        self.assertIn("list", err)
        self.assertEqual(out, "")

    def test_run_without_a_command_hands_the_arguments_to_the_window(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_launch_window", return_value=7) as launch:
            code = cli.run(["gpu-switcher", "--gapplication-app-id", "x"], out, err)
        self.assertEqual(code, 7)
        launch.assert_called_once_with(["gpu-switcher", "--gapplication-app-id", "x"])
        self.assertEqual(out.getvalue(), "")

    def test_run_reports_a_detection_failure_and_exits_one(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        boom = mock.Mock(side_effect=OSError("lspci is not installed"))
        with mock.patch.object(cli, "detect", mock.Mock(detect=boom)):
            code = cli.run(["gpu-switcher", "status"], out, err)
        self.assertEqual(code, 1)
        self.assertIn(
            _text("Detection failed: %s.") % "lspci is not installed", err.getvalue()
        )


class VersionSource(unittest.TestCase):
    """The version the CLI prints is the version the package carries."""

    def test_version_matches_the_version_in_pyproject(self) -> None:
        import gpu_switcher

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "version"
        ]
        self.assertEqual(gpu_switcher.__version__, declared)


if __name__ == "__main__":
    unittest.main()
