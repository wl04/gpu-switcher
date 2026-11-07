"""Unit tests for the unprivileged detection in detect.py.

Covers the gnome-shell journal line matching, the pure parsing cores,
the GNOME/Wayland session classification (XDG_CURRENT_DESKTOP
normalization, fallback to shell presence), the pure process-tree scan
for the uid-scoped gnome-shell pids, the uid-scoped journal attribution
of the running primary, and the journal error/absence distinction.
External binaries (lspci, journalctl) are replaced by stubs in the unit
tests; the real commands are exercised by the manual end-to-end
verification.
"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale. LANGUAGE is cleared so it cannot override the LC_ALL pin.
os.environ["LC_ALL"] = "en"
os.environ.pop("LANGUAGE", None)

from gpu_switcher import detect


class PrimaryCardFromLine(unittest.TestCase):
    def test_primary_card_from_line_extracts_card_from_rule_mode_journal_line(self):
        line = "GPU /dev/dri/card2 selected primary given udev rule"
        self.assertEqual(detect.primary_card_from_line(line), "card2")

    def test_primary_card_from_line_extracts_card_from_auto_mode_journal_line(self):
        line = "Boot VGA GPU /dev/dri/card1 selected as primary"
        self.assertEqual(detect.primary_card_from_line(line), "card1")

    def test_primary_card_from_line_returns_none_when_line_has_no_primary_selection(
        self,
    ):
        line = "Added device '/dev/dri/card1' (amdgpu) using atomic mode setting."
        self.assertIsNone(detect.primary_card_from_line(line))


class LastPrimaryCard(unittest.TestCase):
    def test_last_primary_card_returns_card_from_last_matching_line(self):
        lines = [
            "GPU /dev/dri/card1 selected primary given udev rule",
            "noise",
            "Boot VGA GPU /dev/dri/card2 selected as primary",
        ]
        self.assertEqual(detect.last_primary_card(lines), "card2")

    def test_last_primary_card_returns_none_when_no_line_matches(self):
        lines = ["noise", "more noise"]
        self.assertIsNone(detect.last_primary_card(lines))


class ManagedRulePciFromText(unittest.TestCase):
    def test_managed_rule_pci_from_text_extracts_pci_from_rule_text(self):
        text = 'KERNELS=="0000:12:00.0", ATTRS{vendor}=="0x1002"\n'
        self.assertEqual(detect.managed_rule_pci_from_text(text), "0000:12:00.0")

    def test_managed_rule_pci_from_text_returns_none_when_rule_text_absent(self):
        self.assertIsNone(detect.managed_rule_pci_from_text(""))


class ParseLspciMm(unittest.TestCase):
    """parse_lspci_mm reads the `lspci -Dmmnn` machine format: it keeps only
    display-controller classes and strips the trailing [id] from the vendor
    and device fields so the name matches the `lspci -nn` text."""

    def test_parse_lspci_mm_parses_machine_format(self):
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
    def test_is_gnome_wayland_returns_true_when_wayland_session_and_gnome_desktop(self):
        self.assertTrue(detect.is_gnome_wayland("wayland", "GNOME", True))

    def test_is_gnome_wayland_returns_true_when_wayland_case_is_mixed(self):
        self.assertTrue(detect.is_gnome_wayland("WAYLAND", "GNOME", True))

    def test_is_gnome_wayland_returns_false_when_x11_session(self):
        self.assertFalse(detect.is_gnome_wayland("x11", "GNOME", True))

    def test_is_gnome_wayland_returns_false_when_shell_missing(self):
        # XDG_CURRENT_DESKTOP unset: gnome-shell presence is the fallback.
        self.assertFalse(detect.is_gnome_wayland("wayland", "", False))

    def test_is_gnome_wayland_returns_false_for_kde_wayland_with_gnome_installed(self):
        self.assertFalse(detect.is_gnome_wayland("wayland", "KDE", True))

    def test_is_gnome_wayland_falls_back_to_shell_presence_when_desktop_unset(self):
        self.assertTrue(detect.is_gnome_wayland("wayland", "", True))


class GnomeShellPidsForUid(unittest.TestCase):
    """Pure process-tree scan: {pid: (ppid, comm, uid)}.

    Two users: uid 1000 owns shells 200 and 300; uid 999 owns shell 400.
    The scoping key is the uid (not a logind session leader).
    """

    def _tree(self):
        return {
            1: (0, "systemd", 0),
            200: (1, "gnome-shell", 1000),
            300: (1, "gnome-shell", 1000),
            400: (1, "gnome-shell", 999),
        }

    def test_gnome_shell_pids_for_uid_collects_only_that_users_shells(self):
        self.assertEqual(
            detect.gnome_shell_pids_for_uid(self._tree(), 1000), [200, 300]
        )

    def test_gnome_shell_pids_for_uid_excludes_pids_with_unknown_uid(self):
        tree = {200: (1, "gnome-shell", None), 400: (1, "gnome-shell", 1000)}
        self.assertEqual(detect.gnome_shell_pids_for_uid(tree, 1000), [400])


class ProcessTree(unittest.TestCase):
    """The live /proc scan must not die on non-UTF-8 process names: a
    binary that wrote an invalid byte to its comm must be skipped, not
    kill the detection thread."""

    def test_process_tree_skips_non_utf8_comm(self):
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

    def _stub(self, **kwargs):
        return mock.patch("gpu_switcher.detect.subprocess.run", **kwargs)

    def test_running_primary_returns_card_when_journal_has_selection_line(self):
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

    def test_running_primary_scopes_by_uid_without_logind(self):
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

    def test_running_primary_passes_grep_and_cat_to_journalctl(self):
        # The filter (grep) and plain output (cat) are pushed to journalctl
        # instead of the -q silence flag: the parser runs on bare message
        # lines.
        tree = {1: (0, "systemd", 0), 200: (1, "gnome-shell", 1000)}
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU /dev/dri/card2 selected primary given udev rule\n",
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
        self.assertIn("-o", argv)
        self.assertIn("cat", argv)
        self.assertIn("--grep", argv)
        self.assertIn(detect.PRIMARY_GREP, argv)
        self.assertNotIn("-q", argv)

    def test_running_primary_returns_no_error_when_journal_has_no_selection_line(self):
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

    def test_running_primary_returns_error_when_journal_read_fails(self):
        with (
            self._stub(side_effect=FileNotFoundError("journalctl")),
            mock.patch.object(detect, "_process_tree", return_value=self._TREE),
            mock.patch("os.getuid", return_value=1000),
        ):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_running_primary_returns_error_when_journal_exit_code_is_non_zero(self):
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

    def test_running_primary_returns_unknown_when_no_shell_owned_by_our_uid(self):
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

    def test_running_primary_returns_unknown_when_multiple_shells_of_our_uid(self):
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

    def _run_detect(self, pci_class, driver_link=True):
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
            mock.patch("gpu_switcher.detect.managed_rule_pci", return_value=None),
            mock.patch("gpu_switcher.detect.rule_conflicts", return_value=[]),
            mock.patch("gpu_switcher.detect.gnome_wayland_session", return_value=True),
        ):
            return detect.detect()

    def test_detect_accepts_vga_compatible_class(self):
        result = self._run_detect("0x030000")
        self.assertEqual([g.pci for g in result.gpus], ["0000:12:00.0"])

    def test_detect_accepts_3d_controller_class(self):
        result = self._run_detect("0x030200")
        self.assertEqual([g.pci for g in result.gpus], ["0000:12:00.0"])

    def test_detect_skips_pci_class_outside_display_controllers(self):
        result = self._run_detect("0x080000")  # system peripheral
        self.assertEqual(result.gpus, [])

    def test_detect_excludes_card_without_bound_driver(self):
        # Display-class PCI, but the driver symlink is absent (unbound):
        # the shared is_display_gpu contract excludes it.
        result = self._run_detect("0x030000", driver_link=False)
        self.assertEqual(result.gpus, [])


if __name__ == "__main__":
    unittest.main()
