"""Unit tests for the udev rule scan in core/udev.py.

The scan reports two findings that are not the same thing: *.rules that really
assign the preferred-GPU tag, and rules it could not read. udev's load order is
tested too (an earlier directory shadows a later one).
"""

import os
import tempfile
import unittest
from unittest import mock

from gpu_switcher.core.udev import (
    MUTTER_ENV,
    MUTTER_TAG,
    read_text,
    scan_rules,
)

TAG_LINE = f'SUBSYSTEM=="drm", TAG+="{MUTTER_TAG}"\n'
ENV_LINE = f'ENV{{{MUTTER_ENV}}}="1"\n'


class ScanRules(unittest.TestCase):
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

    def test_scan_rules_finds_rule_setting_mutter_tag(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, TAG_LINE)
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_finds_rule_setting_legacy_env_variable(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, ENV_LINE)
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_finds_assignment_after_match_on_same_line(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f'SUBSYSTEM=="drm", TAG=="seat", TAG+="{MUTTER_TAG}"\n')
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_finds_env_assignment_after_env_match(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f'ENV{{A}}=="1", ENV{{{MUTTER_ENV}}}="1"\n')
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_finds_assignment_on_continued_line(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f'SUBSYSTEM=="drm", \\\nTAG+="{MUTTER_TAG}"\n')
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_ignores_tag_name_in_negated_match(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f'TAG!="{MUTTER_TAG}", TAG+="x"\n')
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [])

    def test_scan_rules_ignores_unassign_and_match_lines(self) -> None:
        # TAG-= (unset) and TAG== (match expression) are not assignments
        # of the preferred GPU and must not block the switch.
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(
            path,
            f'SUBSYSTEM=="drm", TAG-="{MUTTER_TAG}"\n'
            f'SUBSYSTEM=="drm", TAG=="{MUTTER_TAG}"\n'
            f'SUBSYSTEM=="drm", ENV{{{MUTTER_ENV}}}!=""\n',
        )
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [])

    def test_scan_rules_finds_plain_assignment_lines(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(
            path,
            f'SUBSYSTEM=="drm", TAG="{MUTTER_TAG}"\nENV{{{MUTTER_ENV}}}="1"\n',
        )
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [path])

    def test_scan_rules_ignores_comment_lines(self) -> None:
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f"# {MUTTER_TAG}\n# {ENV_LINE}")
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [])

    def test_scan_rules_skips_managed_rule_file(self) -> None:
        managed = os.path.join(self.dir_a, "61-mutter-primary-gpu.rules")
        self._write_rule(managed, TAG_LINE)
        clean = os.path.join(self.dir_a, "10-clean.rules")
        self._write_rule(clean, 'SUBSYSTEM=="drm"\n')
        self.assertEqual(
            scan_rules((self.dir_a,), managed_rule=managed).conflicts,
            [],
        )

    def test_scan_rules_earlier_directory_shadows_later_same_basename(self) -> None:
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
                    scan_rules((self.dir_a, self.dir_b)).conflicts,
                    expected,
                )

    def test_scan_rules_records_unreadable_file_as_unreadable_not_conflict(
        self,
    ) -> None:
        # A rule nobody can read is not known to set the tag. Saying so is what
        # lets the helper name the file instead of accusing it.
        path = os.path.join(self.dir_a, "10-broken.rules")
        self._make_unreadable(path)
        scan = scan_rules((self.dir_a,))
        self.assertEqual(scan.conflicts, [])
        self.assertEqual(scan.unreadable, [path])

    def test_scan_rules_records_unlistable_directory_as_unreadable(self) -> None:
        with mock.patch("os.listdir", side_effect=PermissionError("denied")):
            scan = scan_rules((self.dir_a,))
        self.assertEqual(scan.conflicts, [])
        self.assertEqual(scan.unreadable, [self.dir_a])

    def test_scan_rules_reports_conflicts_and_unreadable_files_together(self) -> None:
        conflict = os.path.join(self.dir_a, "10-tagged.rules")
        self._write_rule(conflict, TAG_LINE)
        broken = os.path.join(self.dir_a, "20-broken.rules")
        self._make_unreadable(broken)
        scan = scan_rules((self.dir_a,))
        self.assertEqual(scan.conflicts, [conflict])
        self.assertEqual(scan.unreadable, [broken])

    def test_scan_rules_ignores_missing_directory(self) -> None:
        scan = scan_rules((os.path.join(self.root, "missing"),))
        self.assertEqual(scan.conflicts, [])
        self.assertEqual(scan.unreadable, [])

    def test_scan_rules_ignores_non_rules_files(self) -> None:
        other = os.path.join(self.dir_a, "10-notes.txt")
        self._write_rule(other, TAG_LINE)
        self.assertEqual(scan_rules((self.dir_a,)).conflicts, [])

    def test_scan_rules_matches_each_assignment_with_its_own_key(self) -> None:
        cases = [
            ('TAG=="seat", TAG+="%s"' % MUTTER_TAG, True),
            ('ENV{OTHER}=="x", ENV{%s}="1"' % MUTTER_ENV, True),
            ('TAG+="seat", ENV{NOTE}="%s"' % MUTTER_TAG, False),
            ('TAG+="seat", \\\nTAG+="%s"' % MUTTER_TAG, True),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                with open(os.path.join(self.dir_a, "10-x.rules"), "w") as f:
                    f.write(line + "\n")
                found = scan_rules((self.dir_a,)).conflicts
                self.assertEqual(bool(found), expected)


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
