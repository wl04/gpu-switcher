"""Unit tests for the shared udev rule scanning in udev_rules.py.

The scan is shared by the unprivileged app (unreadable rules are skipped;
the authoritative check runs in the root helper) and the helper (unreadable
rules must block), so both unreadable modes and the udev load-order
semantics (earlier directory shadows the later one) are tested here.
"""
import os
import tempfile
import unittest

from udev_rules import MUTTER_ENV, MUTTER_TAG, read_text, rule_conflicts

TAG_LINE = f'SUBSYSTEM=="drm", TAG+="{MUTTER_TAG}"\n'
ENV_LINE = f'ENV{{{MUTTER_ENV}}}="1"\n'


class RuleConflicts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.dir_a = os.path.join(self.root, "a")
        self.dir_b = os.path.join(self.root, "b")
        os.makedirs(self.dir_a)
        os.makedirs(self.dir_b)

    def _write_rule(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _make_unreadable(self, path):
        # Broken symlink: open() fails with FileNotFoundError even when
        # running as root, so the "unreadable" path is exercised reliably.
        os.symlink(os.path.join(self.root, "missing-target"), path)

    def test_RuleConflicts_FindsRule_SettingMutterTag(self):
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, TAG_LINE)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable="skip"), [path])

    def test_RuleConflicts_FindsRule_SettingLegacyEnvVariable(self):
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, ENV_LINE)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable="skip"), [path])

    def test_RuleConflicts_IgnoresCommentLines(self):
        path = os.path.join(self.dir_a, "10-other.rules")
        self._write_rule(path, f"# {MUTTER_TAG}\n# {ENV_LINE}")
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable="skip"), [])

    def test_RuleConflicts_SkipsManagedRuleFile(self):
        managed = os.path.join(self.dir_a, "61-mutter-primary-gpu.rules")
        self._write_rule(managed, TAG_LINE)
        clean = os.path.join(self.dir_a, "10-clean.rules")
        self._write_rule(clean, 'SUBSYSTEM=="drm"\n')
        self.assertEqual(
            rule_conflicts((self.dir_a,), managed_rule=managed,
                           on_unreadable="skip"), [])

    def test_RuleConflicts_EarlierDirectoryShadowsLaterSameBasename(self):
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
                    rule_conflicts((self.dir_a, self.dir_b),
                                  on_unreadable="skip"), expected)

    def test_RuleConflicts_SkipsUnreadableFile_InAppMode(self):
        path = os.path.join(self.dir_a, "10-broken.rules")
        self._make_unreadable(path)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable="skip"), [])

    def test_RuleConflicts_FlagsUnreadableFile_InHelperMode(self):
        path = os.path.join(self.dir_a, "10-broken.rules")
        self._make_unreadable(path)
        self.assertEqual(
            rule_conflicts((self.dir_a,), on_unreadable="flag"), [path])


class ReadText(unittest.TestCase):
    def test_ReadText_ReturnsStrippedContent_WhenFileExists(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "file")
            with open(path, "w", encoding="utf-8") as f:
                f.write("  line\n")
            self.assertEqual(read_text(path), "line")

    def test_ReadText_ReturnsEmptyString_WhenFileMissing(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(read_text(os.path.join(root, "no-such-file")),
                             "")


if __name__ == "__main__":
    unittest.main()
