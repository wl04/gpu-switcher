"""Managed-rule file operations and reply composition for the helper.

All changes to the managed rule go through here: the atomic write with
fsync (temp file -> udevadm verify -> backup -> rename -> directory
fsync -> udev refresh), backup + retention, removal, and the symlink
refusal. A failure here is either a HelperFailure (for the user) or a
callback with ok=False (mid-transaction).
"""

import contextlib
import os
import shutil
import sys
import tempfile
import time

from gpu_switcher.core.udev import MANAGED_RULE
from gpu_switcher.helper import udev as hud
from gpu_switcher.helper.errors import HelperFailure

# Keep only the newest backups of the managed rule.
BACKUP_KEEP = 5


def _discard(path: str) -> None:
    """Remove a file, ignoring failure (best-effort cleanup)."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def check_not_symlink(refusal_message: str) -> None:
    """Raise HelperFailure(refusal_message) when the managed rule is a
    symlink; a symlink would let a write or a removal act on a different
    file than MANAGED_RULE."""
    if os.path.islink(MANAGED_RULE):
        print("managed rule is a symlink; refusing", file=sys.stderr)
        raise HelperFailure(refusal_message)


def backup_rule() -> str:
    """Copy the existing managed rule to <RULE>.bak.<UTC-ts>.<suffix>;
    '' on failure."""
    suffix = os.urandom(3).hex()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = f"{MANAGED_RULE}.bak.{stamp}.{suffix}"
    try:
        shutil.copy2(MANAGED_RULE, dest)
        os.chown(dest, 0, 0)
        os.chmod(dest, 0o644)
        return dest
    except OSError:
        return ""


def retain_backups(keep: int = BACKUP_KEEP) -> None:
    """Keep only the newest `keep` <RULE>.bak.* backups; unlink the rest.

    Backups are named with a UTC timestamp plus a random suffix, so
    lexicographic order is chronological. Best effort: a failure to remove
    an old backup must never fail the switch.
    """
    base = os.path.basename(MANAGED_RULE) + ".bak."
    directory = os.path.dirname(MANAGED_RULE) or "."
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return
    backups = [e for e in entries if e.startswith(base)]
    for stale in backups[: max(len(backups) - keep, 0)]:
        _discard(os.path.join(directory, stale))


def fsync_dir(path) -> None:
    """fsync a directory (makes a rename in it durable); best effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def unlink_managed_rule() -> None:
    """Remove the managed rule; raises OSError on failure."""
    os.unlink(MANAGED_RULE)


def refresh_message(tr, state, *, leading, backup="", verified_sentence):
    """Full localized reply after a udev refresh.

    The reply is composed of the site's leading sentence, the optional
    backup sentence, and the sentence describing the refresh outcome,
    joined by single spaces. The catalog entries (po/) are the sentence
    fragments chosen so the composition reproduces the full localized
    sentence exactly in every shipped language. `state` is a RefreshState
    constant; `backup` is '' when there is no backup.
    """
    prefix = leading
    if backup:
        prefix += " " + tr("A backup was saved to %s.") % backup
    if state == hud.RefreshState.UNSUPPORTED:
        return (
            prefix
            + " "
            + tr(
                "The udev refresh could not be verified "
                "(udevadm unavailable); a reboot is required."
            )
        )
    if state == hud.RefreshState.FAILED:
        return prefix + " " + tr("The udev refresh failed; a reboot is required.")
    return prefix + " " + verified_sentence


def write_rule_atomically(pci, content, card, tr, on_done, cancellable=None):
    """Write the managed rule transactionally, then refresh udev.

    Steps: temp file in the same directory (fsynced before the rename),
    udevadm verify (async), back up the existing rule and retain the
    newest BACKUP_KEEP, rename into place, fsync the directory, then the
    async udev refresh. on_done(ok, message, backup_path, refresh_ok).
    """

    def move_into_place(tmp_path, backup):
        try:
            os.rename(tmp_path, MANAGED_RULE)
        except OSError:
            _discard(tmp_path)
            print("SetPrimary: failed to move the rule into place", file=sys.stderr)
            on_done(
                False,
                tr("Failed to write the managed rule; nothing changed."),
                "",
                False,
            )
            return
        fsync_dir(os.path.dirname(MANAGED_RULE))
        print(
            f"SetPrimary: rule written for {pci} (backup: {backup or 'none'})",
            file=sys.stderr,
        )

        def on_refresh(state):
            refresh_ok = state == hud.RefreshState.VERIFIED
            message = refresh_message(
                tr,
                state,
                leading=tr("Rule written for %s.") % pci,
                backup=backup,
                verified_sentence=tr(
                    "Udev rules reloaded; the change takes effect after the next "
                    "login (or after a reboot)."
                ),
            )
            on_done(True, message, backup, refresh_ok)

        hud.refresh_udev_async(card, on_refresh, cancellable)

    def after_verify(state, tmp_path):
        if state == hud.RefreshState.FAILED:
            _discard(tmp_path)
            print("SetPrimary: udevadm verify rejected the rule", file=sys.stderr)
            on_done(
                False,
                tr("udevadm verify rejected the rule; nothing changed."),
                "",
                False,
            )
            return
        if os.path.exists(MANAGED_RULE):
            backup = backup_rule()
            if backup == "":
                _discard(tmp_path)
                print("SetPrimary: backup of the existing rule failed", file=sys.stderr)
                on_done(
                    False,
                    tr("Backup of the existing rule failed; nothing changed."),
                    "",
                    False,
                )
                return
            retain_backups()
        else:
            backup = ""
        move_into_place(tmp_path, backup)

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".gpu-switcher-", dir=os.path.dirname(MANAGED_RULE)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # durable before the rename
        os.chown(tmp_path, 0, 0)
        os.chmod(tmp_path, 0o644)
    except OSError:
        _discard(tmp_path)
        print("SetPrimary: failed to write the temporary rule", file=sys.stderr)
        on_done(
            False, tr("Failed to write the managed rule; nothing changed."), "", False
        )
        return
    hud.verify_rule_async(
        tmp_path, lambda state: after_verify(state, tmp_path), cancellable
    )
