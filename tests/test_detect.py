"""Unit tests for the unprivileged detection in detect.py.

Covers the journal line matching, the pure parsers, the GNOME/Wayland
session check, the uid-scoped gnome-shell pid scan and journal attribution,
and the journal error vs. absence distinction. lspci and journalctl are
stubbed; the real commands are covered by the manual end-to-end check.
"""

import os
import subprocess
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

# i18n binds gettext at import time: pin the locale before importing the app
# so English strings are asserted under any developer locale (LANGUAGE would
# override LC_ALL, so it is cleared).
os.environ["LC_ALL"] = "en"
os.environ.pop("LANGUAGE", None)

from gpu_switcher import detect
from gpu_switcher.core import udev as udev_rules


class PrimaryCardFromLine(unittest.TestCase):
    def test_primary_card_from_line_extracts_card_from_rule_mode_journal_line(
        self,
    ) -> None:
        line = "GPU /dev/dri/card2 selected primary given udev rule"
        self.assertEqual(detect.primary_card_from_line(line), "card2")

    def test_primary_card_from_line_extracts_card_from_auto_mode_journal_line(
        self,
    ) -> None:
        line = "Boot VGA GPU /dev/dri/card1 selected as primary"
        self.assertEqual(detect.primary_card_from_line(line), "card1")

    def test_primary_card_from_line_returns_none_when_line_has_no_primary_selection(
        self,
    ) -> None:
        line = "Added device '/dev/dri/card1' (amdgpu) using atomic mode setting."
        self.assertIsNone(detect.primary_card_from_line(line))


class LastPrimaryCard(unittest.TestCase):
    def test_last_primary_card_returns_card_from_last_matching_line(self) -> None:
        lines = [
            "GPU /dev/dri/card1 selected primary given udev rule",
            "noise",
            "Boot VGA GPU /dev/dri/card2 selected as primary",
        ]
        self.assertEqual(detect.last_primary_card(lines), "card2")

    def test_last_primary_card_returns_none_when_no_line_matches(self) -> None:
        lines = ["noise", "more noise"]
        self.assertIsNone(detect.last_primary_card(lines))


class ManagedRulePciFromText(unittest.TestCase):
    def test_managed_rule_pci_from_text_extracts_pci_from_rule_text(self) -> None:
        text = (
            'KERNELS=="0000:12:00.0", ATTRS{vendor}=="0x1002", '
            'TAG+="mutter-device-preferred-primary"\n'
        )
        self.assertEqual(detect.managed_rule_pci_from_text(text), "0000:12:00.0")

    def test_managed_rule_pci_from_text_returns_none_when_rule_text_absent(
        self,
    ) -> None:
        self.assertIsNone(detect.managed_rule_pci_from_text(""))


class ManagedRuleStatus(unittest.TestCase):
    PCI = "0000:12:00.0"

    def test_managed_rule_pci_from_text_ignores_commented_rule(self) -> None:
        text = f'# KERNELS=="{self.PCI}"\n'
        self.assertIsNone(detect.managed_rule_pci_from_text(text))

    def test_managed_rule_pci_from_text_ignores_kernels_without_tag(self) -> None:
        text = f'SUBSYSTEMS=="pci", KERNELS=="{self.PCI}"\n'
        self.assertIsNone(detect.managed_rule_pci_from_text(text))

    def test_managed_rule_pci_from_text_reads_generated_rule(self) -> None:
        from gpu_switcher.core.gpu import rule_text

        text = rule_text(self.PCI, "1002", "13c0")
        self.assertEqual(detect.managed_rule_pci_from_text(text), self.PCI)

    def test_managed_rule_pci_from_text_skips_commented_then_reads_active(self) -> None:
        active = f'KERNELS=="{self.PCI}", TAG+="mutter-device-preferred-primary"\n'
        text = '# KERNELS=="0000:99:00.0"\n' + active
        self.assertEqual(detect.managed_rule_pci_from_text(text), self.PCI)

    def _status(self, text: str | None) -> tuple[detect.RuleState, str | None]:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rule")
            if text is not None:
                with open(path, "w") as f:
                    f.write(text)
            return detect.managed_rule_status(path)

    def test_managed_rule_status_absent_when_file_missing(self) -> None:
        self.assertEqual(self._status(None), (detect.RuleState.ABSENT, None))

    def test_managed_rule_status_unrecognized_when_only_comments(self) -> None:
        state, pci = self._status(f'# KERNELS=="{self.PCI}"\n')
        self.assertEqual((state, pci), (detect.RuleState.UNRECOGNIZED, None))

    def test_managed_rule_status_unreadable_when_open_fails(self) -> None:
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            state, pci = detect.managed_rule_status("/x")
        self.assertEqual((state, pci), (detect.RuleState.UNREADABLE, None))


class ParseLspciMm(unittest.TestCase):
    """parse_lspci_mm reads the `lspci -Dmmnn` machine format: it keeps only
    display-controller classes and strips the trailing [id] from the vendor
    and device fields so the name matches the `lspci -nn` text."""

    def test_parse_lspci_mm_parses_machine_format(self) -> None:
        output = (
            '0000:03:00.0 "3D controller [0302]" '
            '"Advanced Micro Devices, Inc. [AMD/ATI] [1002]" '
            '"Czinger [Navi 32] [7550]" rev 0x1c\n'
            '0000:00:02.0 "VGA compatible controller [0300]" '
            '"Advanced Micro Devices, Inc. [AMD/ATI] [1002]" '
            '"Radeon Graphics [13c0]" rev 0x00\n'
            '0000:00:00.0 "Host bridge [0600]" "AMD [1022]" "Root [14d8]"\n'
        )
        names = detect.parse_lspci_mm(output)
        self.assertEqual(
            names,
            {
                "0000:03:00.0": (
                    "Advanced Micro Devices, Inc. [AMD/ATI] Czinger [Navi 32]"
                ),
                "0000:00:02.0": "Advanced Micro Devices, Inc. [AMD/ATI] Radeon Graphics",
            },
        )


class IsGnomeWayland(unittest.TestCase):
    def test_is_gnome_wayland_returns_true_when_wayland_session_and_gnome_desktop(
        self,
    ) -> None:
        self.assertTrue(detect.is_gnome_wayland("wayland", "GNOME", True))

    def test_is_gnome_wayland_returns_true_when_wayland_case_is_mixed(self) -> None:
        self.assertTrue(detect.is_gnome_wayland("WAYLAND", "GNOME", True))

    def test_is_gnome_wayland_returns_false_when_x11_session(self) -> None:
        self.assertFalse(detect.is_gnome_wayland("x11", "GNOME", True))

    def test_is_gnome_wayland_returns_false_when_shell_missing(self) -> None:
        # XDG_CURRENT_DESKTOP unset: gnome-shell presence is the fallback.
        self.assertFalse(detect.is_gnome_wayland("wayland", "", False))

    def test_is_gnome_wayland_returns_false_for_kde_wayland_with_gnome_installed(
        self,
    ) -> None:
        self.assertFalse(detect.is_gnome_wayland("wayland", "KDE", True))

    def test_is_gnome_wayland_falls_back_to_shell_presence_when_desktop_unset(
        self,
    ) -> None:
        self.assertTrue(detect.is_gnome_wayland("wayland", "", True))


class GnomeShellPidsForUid(unittest.TestCase):
    """Pure process-tree scan: {pid: (ppid, comm, uid)}.

    Two users: uid 1000 owns shells 200 and 300; uid 999 owns shell 400.
    The scoping key is the uid (not a logind session leader).
    """

    def _tree(self) -> dict[int, tuple[int, str, int | None]]:
        return {
            1: (0, "systemd", 0),
            200: (1, "gnome-shell", 1000),
            300: (1, "gnome-shell", 1000),
            400: (1, "gnome-shell", 999),
        }

    def test_gnome_shell_pids_for_uid_collects_only_that_users_shells(self) -> None:
        self.assertEqual(
            detect.gnome_shell_pids_for_uid(self._tree(), 1000), [200, 300]
        )

    def test_gnome_shell_pids_for_uid_excludes_pids_with_unknown_uid(self) -> None:
        tree = {200: (1, "gnome-shell", None), 400: (1, "gnome-shell", 1000)}
        self.assertEqual(detect.gnome_shell_pids_for_uid(tree, 1000), [400])


class ProcessTree(unittest.TestCase):
    """The live /proc scan must not die on non-UTF-8 process names: a
    binary that wrote an invalid byte to its comm must be skipped, not
    kill the detection thread."""

    def test_process_tree_skips_non_utf8_comm(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "100"))
            with open(os.path.join(root, "100", "stat"), "wb") as f:
                f.write(b"100 (good\xff\xfe) S 1 0")
            os.makedirs(os.path.join(root, "200"))
            with open(os.path.join(root, "200", "stat"), "wb") as f:
                f.write(b"200 (shell) S 100 0")
            with open(os.path.join(root, "200", "status"), "wb") as f:
                f.write(b"Uid:\t1000\t1000\n")
            tree = detect._process_tree(root)
        self.assertEqual(set(tree), {100, 200})
        # No status file for pid 100's valid comm? It has comm replaced, uid None.
        self.assertEqual(tree[200], (100, "shell", 1000))
        self.assertNotEqual(tree[100][1], "shell")
        self.assertIsNone(tree[100][2])


class ProcessStartTime(unittest.TestCase):
    # `pid (comm) state ppid ...`: starttime is field 22, i.e. index 19 after
    # the closing parenthesis; comm may itself contain spaces and ")".
    @staticmethod
    def _stat(comm: str, starttime: int) -> str:
        fields = ["S", "1"] + ["0"] * 17 + [str(starttime)] + ["0"] * 5
        return f"200 ({comm}) " + " ".join(fields)

    def _proc(self, root: str, stat: str | None, btime: str | None) -> None:
        if stat is not None:
            os.makedirs(os.path.join(root, "200"))
            with open(os.path.join(root, "200", "stat"), "w") as f:
                f.write(stat)
        if btime is not None:
            with open(os.path.join(root, "stat"), "w") as f:
                f.write(f"cpu 1 2 3\nbtime {btime}\nprocesses 5\n")

    def test_process_start_time_adds_boot_time_and_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, self._stat("gnome-shell", 5000), "1000")
            result = detect.process_start_time(200, root, clock_ticks=100)
        self.assertEqual(result, 1050.0)

    def test_process_start_time_handles_parenthesis_in_comm(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, self._stat("a) b (c", 200), "1000")
            result = detect.process_start_time(200, root, clock_ticks=100)
        self.assertEqual(result, 1002.0)

    def test_process_start_time_returns_none_when_process_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, None, "1000")
            result = detect.process_start_time(200, root, clock_ticks=100)
        self.assertIsNone(result)

    def test_process_start_time_returns_none_without_boot_time(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._proc(root, self._stat("gnome-shell", 5000), None)
            result = detect.process_start_time(200, root, clock_ticks=100)
        self.assertIsNone(result)


class RuleMtime(unittest.TestCase):
    def test_rule_mtime_returns_modification_time(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            os.utime(f.name, (1, 1234.0))
            self.assertEqual(detect.rule_mtime(f.name), 1234.0)

    def test_rule_mtime_returns_none_when_file_missing(self) -> None:
        self.assertIsNone(detect.rule_mtime("/nonexistent/rule"))


class RunningPrimary(unittest.TestCase):
    """A failed journal read must be reported as an error, not masked as
    "no selection line". The journal is scoped to the single gnome-shell pid
    owned by our uid (scoping by /proc/<pid>/status Uid:, no logind): with
    zero or multiple in-scope pids the attribution is ambiguous and the
    journal is never queried."""

    # One user (uid 1000) with a single gnome-shell (pid 200).
    _TREE = {
        1: (0, "systemd", 0),
        100: (1, "gnome-session", 1000),
        200: (100, "gnome-shell", 1000),
    }

    def _stub(self, **kwargs: Any) -> Any:
        return mock.patch("gpu_switcher.detect.subprocess.run", **kwargs)

    def test_running_primary_returns_card_when_journal_has_selection_line(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="noise\nGPU /dev/dri/card2 selected primary given udev rule\n",
            stderr="",
        )
        with (
            self._stub(return_value=result),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual(card, "card2")
        self.assertIsNone(error)

    def test_running_primary_scopes_by_uid_without_logind(self) -> None:
        # Two gnome-shell pids from two users; only ours (uid 1000) may
        # reach the journal. No logind session-leader round trip involved.
        tree = {
            1: (0, "systemd", 0),
            200: (1, "gnome-shell", 1000),
            600: (1, "gnome-shell", 999),
        }
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU /dev/dri/card2 selected primary\n",
            stderr="",
        )
        with (
            self._stub(return_value=result) as run,
            mock.patch.object(detect, "_process_tree", return_value=tree),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual(card, "card2")
        self.assertIsNone(error)
        argv = run.call_args[0][0]
        self.assertIn("_PID=200", argv)
        self.assertNotIn("_PID=600", argv)

    def test_running_primary_reads_the_user_journal_before_the_system_journal(
        self,
    ) -> None:
        # The system journal is restricted to adm/systemd-journal members on
        # many distros; the user journal is this user's own. Ask the readable
        # one first, and do not ask the other once it answered.
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU /dev/dri/card2 selected primary\n",
            stderr="",
        )
        with (
            self._stub(return_value=result) as run,
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual((card, error), ("card2", None))
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertIn("--user", argv)
        self.assertIn("_PID=200", argv)

    def test_running_primary_falls_back_to_the_system_journal_without_a_user_journal(
        self,
    ) -> None:
        # journalctl --user fails where no user journal exists, and a session
        # logged to the system journal is then the only source left.
        denied = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="No user journals are available"
        )
        found = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU /dev/dri/card1 selected as primary\n",
            stderr="",
        )
        with (
            self._stub(side_effect=[denied, found]) as run,
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual((card, error), ("card1", None))
        argvs = [call[0][0] for call in run.call_args_list]
        self.assertIn("--user", argvs[0])
        self.assertNotIn("--user", argvs[1])

    def test_running_primary_falls_back_when_the_user_journal_has_no_selection_line(
        self,
    ) -> None:
        # A readable user journal that says nothing is not an answer: this
        # distro may keep the session's messages in the system journal.
        quiet = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr=""
        )
        found = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU /dev/dri/card1 selected as primary\n",
            stderr="",
        )
        with (
            self._stub(side_effect=[quiet, found]),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual((card, error), ("card1", None))

    def test_running_primary_bounds_the_query_to_this_shell_start(self) -> None:
        start = 1_700_000_000  # a fixed epoch: the bound is asserted, not recomputed
        quiet = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr=""
        )
        with (
            self._stub(return_value=quiet) as run,
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
            mock.patch.object(detect, "process_start_time", return_value=float(start)),
        ):
            detect.running_primary()
        argv = run.call_args_list[0][0][0]
        self.assertIn("--since", argv)
        since = argv[argv.index("--since") + 1]
        self.assertRegex(since, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # Round-tripped: the same wall clock, whatever timezone runs the tests.
        self.assertAlmostEqual(
            time.mktime(time.strptime(since, "%Y-%m-%d %H:%M:%S")), float(start)
        )

    def test_running_primary_stays_unknown_when_a_journal_is_simply_empty(
        self,
    ) -> None:
        # One journal refused, the other answered with nothing: the running
        # primary is unknown, not broken.
        denied = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        quiet = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr=""
        )
        with (
            self._stub(side_effect=[denied, quiet]),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual((card, error), (None, None))

    def test_running_primary_matches_selection_lines_without_journalctl_grep(
        self,
    ) -> None:
        # journalctl --grep needs a PCRE2-capable build; where the distro build
        # lacks it the call fails and the running primary used to stay unknown.
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="noise\nGPU /dev/dri/card2 selected primary given udev rule\n",
            stderr="",
        )
        with (
            self._stub(return_value=result) as run,
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertEqual((card, error), ("card2", None))
        argv = run.call_args[0][0]
        self.assertNotIn("--grep", argv)
        self.assertIn("-o", argv)
        self.assertIn("cat", argv)

    def test_running_primary_returns_no_error_when_journal_has_no_selection_line(
        self,
    ) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr=""
        )
        with (
            self._stub(return_value=result),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)

    def test_running_primary_returns_error_when_journal_read_fails(self) -> None:
        with (
            self._stub(side_effect=FileNotFoundError("journalctl")),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_running_primary_returns_error_when_journal_exit_code_is_non_zero(
        self,
    ) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        with (
            self._stub(return_value=result),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_running_primary_returns_unknown_when_no_shell_owned_by_our_uid(
        self,
    ) -> None:
        # A shell of another user only: it never counts, and with no
        # in-scope pid the journal is never queried.
        tree = {
            1: (0, "systemd", 0),
            600: (1, "gnome-shell", 999),
        }
        with (
            mock.patch.object(detect, "_process_tree", return_value=tree),
            self._stub() as run,
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()

    def test_running_primary_returns_unknown_when_multiple_shells_of_our_uid(
        self,
    ) -> None:
        # Two concurrent shells owned by our uid: the attribution is
        # ambiguous, so the result stays unknown and the journal is never
        # queried.
        tree = {
            1: (0, "systemd", 0),
            200: (1, "gnome-shell", 1000),
            300: (1, "gnome-shell", 1000),
        }
        with (
            mock.patch.object(detect, "_process_tree", return_value=tree),
            self._stub() as run,
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()


class DetectClassFilter(unittest.TestCase):
    """detect() uses the shared is_display_gpu contract: display-controller
    class (VGA 0x030000 / 3D 0x030200), a bound driver, and the DRM card
    with a /dev/dri device; every other PCI is skipped."""

    def _run_detect(self, pci_class: str, driver_link: bool = True) -> detect.Detection:
        with (
            mock.patch(
                "os.listdir",
                side_effect=lambda d: ["card1"] if d == "/sys/class/drm" else [],
            ),
            mock.patch("os.path.realpath", return_value="0000:12:00.0"),
            mock.patch("os.path.isdir", return_value=True),
            mock.patch("os.path.islink", return_value=driver_link),
            mock.patch(
                "gpu_switcher.core.udev.read_text",
                side_effect=lambda p: pci_class if p.endswith("/class") else "",
            ),
            mock.patch(
                "gpu_switcher.detect.read_text",
                side_effect=lambda p: pci_class if p.endswith("/class") else "",
            ),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("gpu_switcher.detect.lspci_names", return_value={}),
            mock.patch(
                "gpu_switcher.detect.running_primary", return_value=(None, None)
            ),
            mock.patch(
                "gpu_switcher.detect.managed_rule_status",
                return_value=(detect.RuleState.ABSENT, None),
            ),
            mock.patch(
                "gpu_switcher.detect.scan_rules", return_value=udev_rules.RuleScan()
            ),
            mock.patch("gpu_switcher.detect.gnome_wayland_session", return_value=True),
        ):
            return detect.detect()

    def test_detect_accepts_vga_compatible_class(self) -> None:
        result = self._run_detect("0x030000")
        self.assertEqual([g.pci for g in result.gpus], ["0000:12:00.0"])

    def test_detect_accepts_3d_controller_class(self) -> None:
        result = self._run_detect("0x030200")
        self.assertEqual([g.pci for g in result.gpus], ["0000:12:00.0"])

    def test_detect_skips_pci_class_outside_display_controllers(self) -> None:
        result = self._run_detect("0x080000")  # system peripheral
        self.assertEqual(result.gpus, [])

    def test_detect_excludes_card_without_bound_driver(self) -> None:
        # Display-class PCI, but the driver symlink is absent (unbound):
        # the shared is_display_gpu contract excludes it.
        result = self._run_detect("0x030000", driver_link=False)
        self.assertEqual(result.gpus, [])


class ConfiguredTagApplied(unittest.TestCase):
    """detect asks udev — without privileges — whether the preferred-GPU tag is on
    the configured card now, and asks only when there is a configured card whose
    rule could be applied.
    """

    _CARD1_PCI = "0000:01:00.0"
    _CARD2_PCI = "0000:12:00.0"

    def _gpus(self) -> list[detect.Gpu]:
        return [
            detect.Gpu(
                card=card,
                node=f"/dev/dri/{card}",
                pci=pci,
                vendor="1002",
                device="13c0",
                name=f"GPU {card}",
                in_use=False,
                preferred=False,
            )
            for card, pci in (("card1", self._CARD1_PCI), ("card2", self._CARD2_PCI))
        ]

    def _udev_answers(self, answer: bool | None):
        return mock.patch.object(detect, "tag_applied", return_value=answer)

    def test_configured_tag_applied_asks_udev_about_the_configured_card(self) -> None:
        with self._udev_answers(True) as ask:
            answer = detect.configured_tag_applied(
                self._gpus(), self._CARD2_PCI, detect.RuleState.VALID
            )
        self.assertIs(answer, True)
        ask.assert_called_once_with("card2")

    def test_configured_tag_applied_does_not_ask_when_nothing_is_configured(
        self,
    ) -> None:
        # Automatic selection writes no rule, so there is no tag for udev to hold.
        with self._udev_answers(True) as ask:
            self.assertIsNone(
                detect.configured_tag_applied(
                    self._gpus(), self._CARD2_PCI, detect.RuleState.ABSENT
                )
            )
        ask.assert_not_called()

    def test_configured_tag_applied_does_not_ask_when_the_configured_gpu_is_not_present(
        self,
    ) -> None:
        with self._udev_answers(True) as ask:
            self.assertIsNone(
                detect.configured_tag_applied(
                    self._gpus(), "0000:99:00.0", detect.RuleState.VALID
                )
            )
        ask.assert_not_called()

    def test_configured_tag_applied_passes_udev_unknown_through_as_unknown(
        self,
    ) -> None:
        with self._udev_answers(None):
            self.assertIs(
                detect.configured_tag_applied(
                    self._gpus(), self._CARD1_PCI, detect.RuleState.VALID
                ),
                None,
            )

    def test_configured_tag_applied_reports_udev_answer_of_false_as_false(self) -> None:
        with self._udev_answers(False):
            self.assertIs(
                detect.configured_tag_applied(
                    self._gpus(), self._CARD1_PCI, detect.RuleState.VALID
                ),
                False,
            )


if __name__ == "__main__":
    unittest.main()
