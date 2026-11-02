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
    def test_PrimaryCardFromLine_ExtractsCard_FromRuleModeJournalLine(self):
        line = "GPU /dev/dri/card2 selected primary given udev rule"
        self.assertEqual(detect.primary_card_from_line(line), "card2")

    def test_PrimaryCardFromLine_ExtractsCard_FromAutoModeJournalLine(self):
        line = "Boot VGA GPU /dev/dri/card1 selected as primary"
        self.assertEqual(detect.primary_card_from_line(line), "card1")

    def test_PrimaryCardFromLine_ReturnsNone_WhenLineHasNoPrimarySelection(self):
        line = ("Added device '/dev/dri/card1' (amdgpu) "
                "using atomic mode setting.")
        self.assertIsNone(detect.primary_card_from_line(line))


class LastPrimaryCard(unittest.TestCase):
    def test_LastPrimaryCard_ReturnsCard_FromLastMatchingLine(self):
        lines = [
            "GPU /dev/dri/card1 selected primary given udev rule",
            "noise",
            "Boot VGA GPU /dev/dri/card2 selected as primary",
        ]
        self.assertEqual(detect.last_primary_card(lines), "card2")

    def test_LastPrimaryCard_ReturnsNone_WhenNoLineMatches(self):
        lines = ["noise", "more noise"]
        self.assertIsNone(detect.last_primary_card(lines))


class ManagedRulePciFromText(unittest.TestCase):
    def test_ManagedRulePciFromText_ExtractsPci_FromRuleText(self):
        text = ('KERNELS=="0000:12:00.0", ATTRS{vendor}=="0x1002"\n')
        self.assertEqual(
            detect.managed_rule_pci_from_text(text), "0000:12:00.0")

    def test_ManagedRulePciFromText_ReturnsNone_WhenRuleTextAbsent(self):
        self.assertIsNone(detect.managed_rule_pci_from_text(""))


class IsGnomeWayland(unittest.TestCase):
    def test_IsGnomeWayland_ReturnsTrue_WhenWaylandSessionAndGnomeDesktop(self):
        self.assertTrue(detect.is_gnome_wayland("wayland", "GNOME", True))

    def test_IsGnomeWayland_ReturnsTrue_WhenWaylandCaseIsMixed(self):
        self.assertTrue(detect.is_gnome_wayland("WAYLAND", "GNOME", True))

    def test_IsGnomeWayland_ReturnsFalse_WhenX11Session(self):
        self.assertFalse(detect.is_gnome_wayland("x11", "GNOME", True))

    def test_IsGnomeWayland_ReturnsFalse_WhenShellMissing(self):
        # XDG_CURRENT_DESKTOP unset: gnome-shell presence is the fallback.
        self.assertFalse(detect.is_gnome_wayland("wayland", "", False))

    def test_IsGnomeWayland_ReturnsFalse_ForKdeWaylandWithGnomeInstalled(self):
        self.assertFalse(detect.is_gnome_wayland("wayland", "KDE", True))

    def test_IsGnomeWayland_FallsBackToShellPresence_WhenDesktopUnset(self):
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

    def test_GnomeShellPidsInTree_CollectsOnlyDescendantsOfLeader(self):
        self.assertEqual(
            detect.gnome_shell_pids_in_tree(self._tree(), 100), [200, 300])

    def test_GnomeShellPidsInTree_ReturnsAll_WhenLeaderUnknown(self):
        self.assertEqual(
            detect.gnome_shell_pids_in_tree(self._tree(), None),
            [200, 300, 400])


class RunningPrimary(unittest.TestCase):
    """A failed journal read must be reported as an error, not masked as
    "no selection line". The journal is scoped to the single in-scope
    gnome-shell pid (a descendant of the session leader, or the single
    system-wide shell when the leader is unknown)."""

    # One session: leader 100 -> shell 200.
    _TREE = {
        1: (0, "systemd"),
        100: (1, "gnome-session"),
        200: (100, "gnome-shell"),
    }

    def _stub(self, **kwargs):
        return mock.patch("gpu_switcher.detect.subprocess.run", **kwargs)

    def test_RunningPrimary_ReturnsCard_WhenJournalHasSelectionLine(self):
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

    def test_RunningPrimary_ReturnsNoError_WhenJournalHasNoSelectionLine(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="noise\n", stderr="")
        with self._stub(return_value=result), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNone(error)

    def test_RunningPrimary_ReturnsError_WhenJournalReadFails(self):
        with self._stub(side_effect=FileNotFoundError("journalctl")), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_RunningPrimary_ReturnsError_WhenJournalExitCodeIsNonZero(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="")
        with self._stub(return_value=result), \
             mock.patch.object(detect, "_process_tree",
                              return_value=self._TREE):
            card, error = detect.running_primary()
        self.assertIsNone(card)
        self.assertIsNotNone(error)

    def test_RunningPrimary_FiltersJournalBySessionPid(self):
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

    def test_RunningPrimary_ReturnsUnknown_WhenMultipleShellPidsInScope(self):
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

    def test_RunningPrimary_ReturnsUnknown_WhenNoShellPidsInScope(self):
        tree = {1: (0, "systemd"), 100: (1, "gnome-session")}
        with mock.patch.object(detect, "_process_tree", return_value=tree), \
             self._stub() as run:
            card, error = detect.running_primary(100)
        self.assertIsNone(card)
        self.assertIsNone(error)
        run.assert_not_called()


class SessionLeader(unittest.TestCase):
    def test_SessionLeader_ReturnsLeaderPid_FromLogind(self):
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
