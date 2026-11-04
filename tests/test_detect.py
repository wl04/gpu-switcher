"""Unit tests for the unprivileged detection in detect.py.

Covers the gnome-shell journal line matching, the pure parsing cores,
the GNOME/Wayland session classification (XDG_CURRENT_DESKTOP
normalization, fallback to shell presence), the pure process-tree scan
for session-scoped gnome-shell pids, the session-scoped journal
attribution of the running primary, the journal error/absence
distinction, and the logind session-leader lookup (bus.session_leader).
External binaries (lspci, journalctl) are replaced by stubs in the unit
tests; the real commands are exercised by the manual end-to-end
verification.
"""
import os
import subprocess
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale.
os.environ["LC_ALL"] = "en"

from gpu_switcher import bus, detect


class PrimaryCardFromLine(unittest.TestCase):
    def test_primary_card_from_line_extracts_card_from_rule_mode_journal_line(self):
        line = "GPU /dev/dri/card2 selected primary given udev rule"
        self.assertEqual(detect.primary_card_from_line(line), "card2")

    def test_primary_card_from_line_extracts_card_from_auto_mode_journal_line(self):
        line = "Boot VGA GPU /dev/dri/card1 selected as primary"
        self.assertEqual(detect.primary_card_from_line(line), "card1")

    def test_primary_card_from_line_returns_none_when_line_has_no_primary_selection(self):
        line = ("Added device '/dev/dri/card1' (amdgpu) "
                "using atomic mode setting.")
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
        text = ('KERNELS=="0000:12:00.0", ATTRS{vendor}=="0x1002"\n')
        self.assertEqual(
            detect.managed_rule_pci_from_text(text), "0000:12:00.0")

    def test_managed_rule_pci_from_text_returns_none_when_rule_text_absent(self):
        self.assertIsNone(detect.managed_rule_pci_from_text(""))


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


class GnomeShellPidsInTree(unittest.TestCase):
    """Pure process-tree scan: {pid: (ppid, comm)}.

    Session 1: leader 100 (gnome-session) -> shell 200 -> shell 300.
    Session 2 (other user): shell 400 directly under init.
    """

    def _tree(self):
        return {
            1: (0, "systemd"),
            100: (1, "gnome-session"),
            200: (100, "gnome-shell"),
            300: (200, "gnome-shell"),
            400: (1, "gnome-shell"),
        }

    def test_gnome_shell_pids_in_tree_collects_only_descendants_of_leader(self):
        self.assertEqual(
            detect.gnome_shell_pids_in_tree(self._tree(), 100), [200, 300])

    def test_gnome_shell_pids_in_tree_returns_all_when_leader_unknown(self):
        self.assertEqual(
            detect.gnome_shell_pids_in_tree(self._tree(), None),
            [200, 300, 400])


class RunningPrimary(unittest.TestCase):
    """A failed journal read must be reported as an error, not masked as
    "no selection line". The journal is scoped to the single in-scope
    gnome-shell pid: a descendant of the session leader, falling back to
    the single system-wide shell when that scan is empty (current GNOME
    runs the shell under a systemd user manager, not under the leader),
    or system-wide when the leader is unknown."""

    # One session: leader 100 -> shell 200.
    _TREE = {
        1: (0, "systemd"),
        100: (1, "gnome-session"),
        200: (100, "gnome-shell"),
    }

    def _stub(self, **kwargs):
        return mock.patch("gpu_switcher.detect.subprocess.run", **kwargs)

    def test_running_primary_returns_card_when_journal_has_selection_line(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="noise\nGPU /dev/dri/card2 selected primary given udev rule\n",
            stderr="")
        with self._stub(return_value=result), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertEqual(card, "card2")
        self.assertIsNone(error)

    def test_running_primary_returns_no_error_when_journal_has_no_selection_line(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr="")
        with self._stub(return_value=result), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)

    def test_running_primary_returns_error_when_journal_read_fails(self):
        with self._stub(side_effect=FileNotFoundError("journalctl")), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_running_primary_returns_error_when_journal_exit_code_is_non_zero(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="")
        with self._stub(return_value=result), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_running_primary_filters_journal_by_session_pid(self):
        # Two interleaved sessions: only our leader's shell pid may
        # reach the journal, so the other session's lines never count.
        tree = {
            1: (0, "systemd"),
            100: (1, "gnome-session"),
            200: (100, "gnome-shell"),
            500: (1, "gnome-session"),
            600: (500, "gnome-shell"),
        }
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="GPU /dev/dri/card2 selected primary "
                       "given udev rule\n")
            card, error = detect.running_primary(100)
        self.assertEqual(card, "card2")
        self.assertIsNone(error)
        argv = run.call_args[0][0]
        self.assertIn("_PID=200", argv)
        self.assertIn(detect.JOURNAL_UNIT, argv)

    def test_running_primary_falls_back_to_system_wide_scan_when_shell_is_not_under_leader(self):
        # Current GNOME runs the session under a systemd user manager:
        # gnome-shell is a descendant of the manager, not of the logind
        # session leader (the greeter worker), so the scoped scan is
        # empty and the fallback must find the single system-wide shell.
        tree = {
            1: (0, "systemd"),
            100: (1, "gdm-session-worker"),
            200: (1, "systemd"),
            300: (200, "gnome-shell"),
        }
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            run.return_value = mock.Mock(
                returncode=0, stderr="",
                stdout="GPU /dev/dri/card2 selected primary "
                       "given udev rule\n")
            card, error = detect.running_primary(100)
        self.assertEqual(card, "card2")
        self.assertIsNone(error)
        argv = run.call_args[0][0]
        self.assertIn("_PID=300", argv)
        self.assertIn(detect.JOURNAL_UNIT, argv)

    def test_running_primary_still_unknown_when_fallback_finds_multiple_shells(self):
        # The scoped scan is empty, but the system-wide fallback finds
        # two shells (two concurrent sessions): the attribution is
        # ambiguous, so the result stays unknown and the journal is
        # never queried.
        tree = {
            1: (0, "systemd"),
            100: (1, "gdm-session-worker"),
            200: (1, "systemd"),
            300: (200, "gnome-shell"),
            400: (200, "gnome-shell"),
        }
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            card, error = detect.running_primary(100)
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()

    def test_running_primary_returns_unknown_when_multiple_shell_pids_in_scope(self):
        tree = {
            1: (0, "systemd"),
            100: (1, "gnome-session"),
            200: (100, "gnome-shell"),
            300: (100, "gnome-shell"),
        }
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            card, error = detect.running_primary(100)
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()

    def test_running_primary_returns_unknown_when_no_shell_pids_in_scope(self):
        tree = {1: (0, "systemd"), 100: (1, "gnome-session")}
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            card, error = detect.running_primary(100)
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()


class DetectClassFilter(unittest.TestCase):
    """detect() accepts both display-controller classes (VGA 0x030000 and
    3D controller 0x030200) and skips every other PCI class; suitability
    beyond the class is established by the DRM card with a /dev/dri
    device."""

    def _run_detect(self, pci_class):
        with mock.patch("os.listdir",
                        side_effect=lambda d: ["card1"]
                        if d == "/sys/class/drm" else []), \
             mock.patch("os.path.realpath", return_value="0000:12:00.0"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("gpu_switcher.detect.read_text",
                        side_effect=lambda p: pci_class
                        if p.endswith("/class") else ""), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("gpu_switcher.detect.lspci_names", return_value={}), \
             mock.patch("gpu_switcher.detect.running_primary",
                        return_value=(None, None)), \
             mock.patch("gpu_switcher.detect.managed_rule_pci",
                        return_value=None), \
             mock.patch("gpu_switcher.detect.rule_conflicts",
                        return_value=[]), \
             mock.patch("gpu_switcher.detect.gnome_wayland_session",
                        return_value=True):
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


class SessionLeader(unittest.TestCase):
    def test_session_leader_returns_leader_pid_from_logind(self):
        conn = mock.Mock()

        def call_sync(dest, path, iface, method, params, rtype,
                      flags, timeout, user_data):
            if method == "ListSessions":
                # out-arg a(susso): one row of (sid, uid, user, seat, path)
                return ((("1", 1000, "user", "seat0",
                          "/org/freedesktop/login1/session/2"),),)
            if method == "Get":
                self.assertEqual(iface, "org.freedesktop.DBus.Properties")
                self.assertEqual(
                    params.unpack(),
                    ("org.freedesktop.login1.Session", "Leader"))
                return (4361,)
            raise AssertionError(f"unexpected method {method}")

        conn.call_sync.side_effect = call_sync
        with mock.patch.object(Gio, "bus_get_sync", return_value=conn), \
             mock.patch.dict(os.environ, {"XDG_SESSION_ID": "1"}):
            self.assertEqual(bus.session_leader(), 4361)


if __name__ == "__main__":
    unittest.main()
