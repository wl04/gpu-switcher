"""Unit tests for the helper's managed-rule file layer
(gpu_switcher/helper/rule_store.py).

Covers the symlink refusal, the backup-name contract the retention order
relies on, backup retention (which files are removed, which are never
touched, best-effort cleanup when a removal fails) and the composition of
the localized refresh reply. The SetPrimary/SetAuto transactions built on
top of this layer are covered in test_helper.py.
"""

import contextlib
import io
import os
import re
import tempfile
import unittest
from typing import Any
from unittest import mock

from gpu_switcher.core.udev import MANAGED_RULE
from gpu_switcher.helper import rule_store
from gpu_switcher.helper import udev as hud
from gpu_switcher.helper.errors import HelperFailure

RULE_BASE = os.path.basename(MANAGED_RULE)
# The retention order is lexicographic, so a backup name that does not sort
# by its own creation time makes retain_backups delete the wrong file.
BACKUP_NAME = re.compile(
    rf"^{re.escape(RULE_BASE)}\.bak\.\d{{8}}T\d{{6}}Z\.[0-9a-f]{{6}}$"
)


class ManagedRuleInTempDir(unittest.TestCase):
    """A throwaway rules directory with MANAGED_RULE pointing into it: real
    files and real removals, without root and without touching /etc."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.rule = os.path.join(self.directory, RULE_BASE)
        patcher = mock.patch.object(rule_store, "MANAGED_RULE", self.rule)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rule(self, content: str = 'ACTION=="add", TAG+="x"\n') -> None:
        with open(self.rule, "w", encoding="utf-8") as handle:
            handle.write(content)

    def make_backup(self, stamp: str) -> str:
        """Create a backup named exactly like backup_rule() would name it."""
        name = f"{RULE_BASE}.bak.{stamp}.abcdef"
        with open(os.path.join(self.directory, name), "w", encoding="utf-8") as handle:
            handle.write("previous rule\n")
        return name

    def make_backups(self, stamps: list[str]) -> list[str]:
        return [self.make_backup(stamp) for stamp in stamps]

    def names(self) -> list[str]:
        return sorted(os.listdir(self.directory))

    def backups(self) -> list[str]:
        return [name for name in self.names() if name.startswith(RULE_BASE + ".bak.")]


class CheckNotSymlink(ManagedRuleInTempDir):
    """A symlinked managed rule would make a write or a removal land on a
    different file than the one the app and the helper both name."""

    def make_symlink(self) -> str:
        target = os.path.join(self.directory, "hand-written.rules")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("someone else's rule\n")
        os.symlink(target, self.rule)
        return target

    def test_check_not_symlink_refuses_when_managed_rule_is_symlink(self) -> None:
        self.make_symlink()

        with self.assertRaises(HelperFailure) as raised:
            rule_store.check_not_symlink("Refusing to modify it.")

        self.assertEqual(raised.exception.args[0], "Refusing to modify it.")

    def test_check_not_symlink_leaves_symlink_and_target_in_place(self) -> None:
        target = self.make_symlink()
        log = io.StringIO()

        with self.assertRaises(HelperFailure), contextlib.redirect_stderr(log):
            rule_store.check_not_symlink("Refusing to modify it.")

        self.assertTrue(os.path.islink(self.rule))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "someone else's rule\n")
        self.assertIn("symlink", log.getvalue())

    def test_check_not_symlink_accepts_a_regular_file(self) -> None:
        self.write_rule()

        rule_store.check_not_symlink("Refusing to modify it.")

        self.assertTrue(os.path.isfile(self.rule))

    def test_check_not_symlink_accepts_a_missing_rule(self) -> None:
        rule_store.check_not_symlink("Refusing to remove it.")

        self.assertFalse(os.path.exists(self.rule))


class BackupRule(ManagedRuleInTempDir):
    """The backup the transaction keeps before replacing the rule.

    os.chown is mocked: chowning the backup to root is a privileged step the
    helper performs as uid 0, mocked the same way in test_helper.py.
    """

    @contextlib.contextmanager
    def unprivileged(self) -> Any:
        with mock.patch("os.chown"):
            yield

    def test_backup_rule_name_sorts_by_its_own_creation_time(self) -> None:
        self.write_rule()

        with self.unprivileged():
            name = rule_store.backup_rule()

        self.assertRegex(os.path.basename(name), BACKUP_NAME)
        self.assertIn(os.path.basename(name), self.backups())

    def test_backup_rule_copies_the_rule_it_replaces(self) -> None:
        self.write_rule("primary 0000:01:00.0\n")

        with self.unprivileged():
            name = rule_store.backup_rule()

        backup = os.path.join(self.directory, os.path.basename(name))
        with open(backup, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "primary 0000:01:00.0\n")

    def test_backup_rule_is_readable_by_everyone(self) -> None:
        # The package installs the managed rule 0644; a backup the helper
        # writes must stay readable for diagnostics and for `udevadm verify`.
        self.write_rule()

        with self.unprivileged():
            name = rule_store.backup_rule()

        mode = os.stat(os.path.join(self.directory, os.path.basename(name))).st_mode
        self.assertEqual(mode & 0o777, 0o644)

    def test_backup_rule_reports_empty_name_when_the_rule_cannot_be_copied(
        self,
    ) -> None:
        # No rule yet: the switch still proceeds, so the caller must be able
        # to tell "no backup was made" from a backup path.
        with self.unprivileged():
            self.assertEqual(rule_store.backup_rule(), "")


class RetainBackups(ManagedRuleInTempDir):
    """Backups accumulate on a machine where the primary GPU is switched
    often; retention must remove exactly the oldest ones and nothing else."""

    STAMPS = [f"20260101T00000{i}Z" for i in range(1, 8)]

    def test_retain_backups_removes_the_oldest_and_keeps_the_newest(self) -> None:
        self.make_backups(self.STAMPS)

        rule_store.retain_backups(keep=3)

        self.assertEqual(
            self.backups(),
            [
                f"{RULE_BASE}.bak.{stamp}.abcdef"
                for stamp in (
                    "20260101T000005Z",
                    "20260101T000006Z",
                    "20260101T000007Z",
                )
            ],
        )

    def test_retain_backups_keeps_every_backup_within_the_limit(self) -> None:
        created = self.make_backups(self.STAMPS[: rule_store.BACKUP_KEEP])

        rule_store.retain_backups()

        self.assertEqual(self.backups(), created)

    def test_retain_backups_touches_nothing_but_managed_rule_backups(self) -> None:
        self.write_rule()
        self.make_backups(self.STAMPS)
        foreign_backup = "99-other-package.rules.bak.20260101T000001Z.abcdef"
        similar = RULE_BASE + ".baknotabackup"
        for name in (foreign_backup, similar):
            with open(
                os.path.join(self.directory, name), "w", encoding="utf-8"
            ) as handle:
                handle.write("not a managed-rule backup\n")

        rule_store.retain_backups(keep=2)

        self.assertIn(foreign_backup, self.names())
        self.assertIn(similar, self.names())
        self.assertIn(RULE_BASE, self.names())

    def test_retain_backups_continues_after_a_backup_cannot_be_removed(self) -> None:
        self.make_backups(self.STAMPS)
        doomed = os.path.join(
            self.directory, f"{RULE_BASE}.bak.{self.STAMPS[0]}.abcdef"
        )
        real_unlink = os.unlink

        def unlink(path: str) -> None:
            if path == doomed:
                raise OSError("Cannot remove an in-use backup")
            real_unlink(path)

        with mock.patch.object(rule_store.os, "unlink", unlink):
            rule_store.retain_backups(keep=2)

        self.assertTrue(os.path.exists(doomed))
        # The failure of one removal must not strand the other old backups.
        self.assertEqual(len(self.backups()), 3)

    def test_retain_backups_ignores_an_unreadable_rules_directory(self) -> None:
        created = self.make_backups(self.STAMPS)

        with mock.patch.object(
            rule_store.os, "listdir", side_effect=OSError("Permission denied")
        ):
            rule_store.retain_backups(keep=1)

        self.assertEqual(self.backups(), created)


class RefreshMessage(unittest.TestCase):
    """The reply the user reads must say what happened to the udev refresh,
    in the user's language, and must name reboot when it is required.

    `leading` and `verified_sentence` arrive already translated: they are the
    outcome of the switch, which only the caller can phrase. Only the fixed
    outcome sentences are translated here."""

    @staticmethod
    def _tr(text: str) -> str:
        return f"[{text}]"

    def test_refresh_message_joins_leading_and_verified_sentences(self) -> None:
        message = rule_store.refresh_message(
            self._tr,
            hud.RefreshState.VERIFIED,
            leading="Primary GPU is now 0000:01:00.0.",
            verified_sentence="A reboot is needed to make it take effect.",
        )

        self.assertEqual(
            message,
            "Primary GPU is now 0000:01:00.0. A reboot is needed to make it take effect.",
        )

    def test_refresh_message_keeps_the_callers_sentences_untranslated(self) -> None:
        # Translating them again would break the locale: the caller's text is
        # already in the user's language.
        message = rule_store.refresh_message(
            self._tr,
            hud.RefreshState.VERIFIED,
            leading="Первичный GPU — 0000:01:00.0.",
            verified_sentence="Перезапустите компьютер.",
        )

        self.assertNotIn("[", message)
        self.assertEqual(
            message, "Первичный GPU — 0000:01:00.0. Перезапустите компьютер."
        )

    def test_refresh_message_reports_failed_refresh_as_reboot_required(self) -> None:
        message = rule_store.refresh_message(
            self._tr,
            hud.RefreshState.FAILED,
            leading="Primary GPU is now 0000:01:00.0.",
            verified_sentence="It will be applied on the next login.",
        )

        self.assertIn("[The udev refresh failed; a reboot is required.]", message)
        self.assertIn("Primary GPU is now 0000:01:00.0.", message)
        self.assertNotIn("It will be applied on the next login.", message)

    def test_refresh_message_reports_unsupported_refresh_as_reboot_required(
        self,
    ) -> None:
        message = rule_store.refresh_message(
            self._tr,
            hud.RefreshState.UNSUPPORTED,
            leading="Primary GPU is back to automatic.",
            verified_sentence="It will be applied on the next login.",
        )

        self.assertIn(
            "[The udev refresh could not be verified "
            "(udevadm unavailable); a reboot is required.]",
            message,
        )
        self.assertNotIn("It will be applied on the next login.", message)

    def test_refresh_message_never_doubles_the_joining_space(self) -> None:
        for state in hud.RefreshState:
            message = rule_store.refresh_message(
                self._tr,
                state,
                leading="Leading sentence.",
                verified_sentence="Verified sentence.",
            )

            self.assertNotIn("  ", message)
