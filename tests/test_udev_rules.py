"""Unit tests for the shared udev rule scanning in udev_rules.py.

The scan is shared by the unprivileged app (unreadable rules are skipped;
the authoritative check runs in the root helper) and the helper (unreadable
rules must block), so both unreadable modes and the udev load-order
semantics (earlier directory shadows the later one) are tested here.
"""

import os
import tempfile
import unittest
from typing import cast

from gpu_switcher.core.udev import (
    MUTTER_ENV,
    MUTTER_TAG,
    OnUnreadable,
    read_text,
    rule_conflicts,
)

TAG_LINE = f'SUBSYSTEM=="drm", TAG+="{MUTTER_TAG}"\n'
ENV_LINE = f'ENV{{{MUTTER_ENV}}}="1"\n'


class RuleConflicts(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.dir_a = os.path.join(self.root, "a")
        self.dir_b = os.path.join(self.root, "b")
        os.makedirs(self.dir_a)
        os.makedirs(self.dir_b)

    def _write_rule(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _make_unreadable(self, path: str) -> None:
        # Broken symlink: open() fails with FileNotFoundError even when
        # running as root, so the "unreadable" path is exercised reliably.
        os.symlink(os.path.join(self.root, "missing-target"), path)

    def test_rule_conflicts_finds_rule_setting_mutter_tag(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, TAG_LINE)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), [path]
        )

    def test_rule_conflicts_finds_rule_setting_legacy_env_variable(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, ENV_LINE)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), [path]
        )

    def test_rule_conflicts_ignores_unassign_and_match_lines(self) -> None:
        # TAG-= (unset) and TAG== (match expression) are not assignments
        # of the preferred GPU and must not block the switch.
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(
            path,
            f'SUBSYSTEM=="drm", TAG-="{MUTTER_TAG}"\n'
            f'SUBSYSTEM=="drm", TAG=="{MUTTER_TAG}"\n'
            f'SUBSYSTEM=="drm", ENV{{{MUTTER_ENV}}}!=""\n',
        )
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), []
        )

    def test_rule_conflicts_finds_plain_assignment_lines(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(
            path,
            f'SUBSYSTEM=="drm", TAG="{MUTTER_TAG}"\nENV{{{MUTTER_ENV}}}="1"\n',
        )
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), [path]
        )

    def test_rule_conflicts_ignores_comment_lines(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f"# {MUTTER_TAG}\n# {ENV_LINE}")
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), []
        )

    def test_rule_conflicts_skips_managed_rule_file(self) -> None:
        managed = os.path.join(self.dir_a, "61-mutter-primary-gpu.rules")
        self._write_rule(managed, TAG_LINE)
        clean = os.path.join(self.dir_a, "10-clean.rules")
        self._write_rule(clean, 'SUBSYSTEM=="drm"\n')
        self.assertEqual(
            rule_conflicts(
                (self.dir_a,), managed_rule=managed, on_unreadable=OnUnreadable.SKIP
            ),
            [],
        )

    def test_rule_conflicts_earlier_directory_shadows_later_same_basename(self) -> None:
        earlier = os.path.join(self.dir_a, "50-same.rules")
        later = os.path.join(self.dir_b, "50-same.rules")
        cases = [
            ("earlier tagged, later clean", TAG_LINE, "# clean\n", [earlier]),
            ("earlier clean, later tagged", "# clean\n", TAG_LINE, []),
        ]
        for label, earlier_text, later_text, expected in cases:
            with self.subTest(label):
                self._write_rule(earlier, earlier_text)
                self._write_rule(later, later_text)
                self.assertEqual(
                    rule_conflicts(
                        (self.dir_a, self.dir_b), on_unreadable=OnUnreadable.SKIP
                    ),
                    expected,
                )

    def test_rule_conflicts_skips_unreadable_file_in_app_mode(self) -> None:
        path = os.path.join(self.dir_a, "10-broken.rules")
        self._make_unreadable(path)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.SKIP), []
        )

    def test_rule_conflicts_flags_unreadable_file_in_helper_mode(self) -> None:
        path = os.path.join(self.dir_a, "10-broken.rules")
        self._make_unreadable(path)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable=OnUnreadable.FLAG), [path]
        )

    def test_rule_conflicts_raises_on_unknown_on_unreadable(self) -> None:
        # An unknown mode must not silently behave like "skip".
        with self.assertRaises(ValueError):
            rule_conflicts((self.dir_a,), on_unreadable=cast(OnUnreadable, "bogus"))


class ReadText(unittest.TestCase):
    def test_read_text_returns_stripped_content_when_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "file")
            with open(path, "w", encoding="utf-8") as f:
                f.write("  line\n")
            self.assertEqual(read_text(path), "line")

    def test_read_text_returns_empty_string_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(read_text(os.path.join(root, "no-such-file")), "")

    def test_read_text_keeps_invalid_utf8_as_replacement_characters(self) -> None:
        # A /sys or /proc value may hold bytes that are not valid UTF-8;
        # reading must not raise (the detector would otherwise die).
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "file")
            with open(path, "wb") as f:
                f.write(b"ab\xff\xfe\n")
            self.assertEqual(read_text(path), "ab\ufffd\ufffd")


if __name__ == "__main__":
    unittest.main()
