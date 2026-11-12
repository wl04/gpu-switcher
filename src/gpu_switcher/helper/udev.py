"""udevadm verification / refresh steps for the helper.

Every step runs as a Gio.Subprocess under a per-step timeout (from
core.udev) and cancellable, and its output goes to the journal.
"""

import os
import sys
import traceback
from collections.abc import Callable
from enum import Enum

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.udev import MUTTER_TAG, UDEVADM_STEP_TIMEOUT_S


class RefreshState(Enum):
    """Outcome of a verify / refresh run.

    UNSUPPORTED is kept apart from VERIFIED so an unverified refresh never
    enables the fast-path logout as though the tags were fresh.
    """

    VERIFIED = "verified"  # every step succeeded; tags confirmed
    UNSUPPORTED = "unsupported"  # udevadm cannot verify: missing or too old
    FAILED = "failed"  # a step errored, timed out, or was rejected


# Absolute path, so a changed PATH cannot substitute another udevadm.
UDEVADM = "/usr/bin/udevadm"
# Checked once per process by check_udevadm().
_udevadm_available = None
# Whether this udevadm understands the `verify` verb (it exists since systemd
# 253); None until the first probe. An older udevadm exits 1 on an unknown
# verb, exactly like it does on a rule it rejects, so the verb is probed once
# before its exit code is allowed to mean "rejected".
_udev_verify_supported: bool | None = None


def check_udevadm() -> bool:
    """Whether udevadm is available; checked once per process."""
    global _udevadm_available
    if _udevadm_available is None:
        _udevadm_available = os.path.exists(UDEVADM)
        print(f"udevadm at {UDEVADM}: available={_udevadm_available}", file=sys.stderr)
    return _udevadm_available


# Exceptions raised inside a GLib callback would otherwise be swallowed by
# PyGObject and leave the D-Bus call unanswered; the service installs a
# handler that releases the operation lock and replies with an error.
callback_error_handler: Callable[[str], None] | None = None


def report_callback_error(detail: str) -> None:
    """Hand an unexpected callback failure to the installed handler (or just
    log it when none is installed)."""
    print("error: " + detail, file=sys.stderr)
    if callback_error_handler is not None:
        callback_error_handler(detail)


def _decode(stream) -> str:
    """Decode a subprocess stream to text.

    communicate_finish returns GLib.Bytes (None for an empty stream), not
    Python bytes.
    """
    if stream is None:
        return ""
    raw = stream if isinstance(stream, bytes) else stream.get_data()
    return raw.decode("utf-8", "replace")


def run_udev_async(argv, on_done, cancellable=None):
    """Run one udevadm step asynchronously under the per-step timeout.

    on_done(rc, stdout, stderr) runs when the step finishes. rc is the exit
    status, or 1 (with an explanation in stderr) when the step could not run
    or timed out; a timed-out child is killed.
    """
    if cancellable is not None and cancellable.is_cancelled():
        on_done(1, "", "cancelled before start")
        return
    step_cancellable = Gio.Cancellable.new()
    timer_fired = False

    def on_deadline() -> bool:
        nonlocal timer_fired
        timer_fired = True
        step_cancellable.cancel()
        return GLib.SOURCE_REMOVE

    def stop_timer() -> None:
        # A fired one-shot source is already gone; removing it again warns.
        if not timer_fired:
            GLib.source_remove(timeout_source)

    timeout_source = GLib.timeout_add(UDEVADM_STEP_TIMEOUT_S * 1000, on_deadline)
    try:
        proc = Gio.Subprocess.new(
            list(argv),
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as error:
        stop_timer()
        print(f"{argv[0]}: failed to start: {error}", file=sys.stderr)
        on_done(1, "", str(error))
        return

    def on_communicate(_source, result, _ud):
        stop_timer()
        try:
            try:
                _ok, out, err = proc.communicate_finish(result)
                rc = proc.get_exit_status()
            except GLib.Error as error:
                print(f"{argv[0]}: {error}", file=sys.stderr)
                on_done(1, "", str(error))
                return
            out_text = _decode(out)
            err_text = _decode(err)
            print(
                f"{argv[0]} rc={rc} output="
                f"{out_text.strip() or err_text.strip() or '-'}",
                file=sys.stderr,
            )
            on_done(rc, out_text, err_text)
        except Exception:
            report_callback_error(
                f"unhandled exception in {argv[0]} callback:\n{traceback.format_exc()}"
            )

    # Cancelling communicate_async only abandons the read; the child keeps
    # running (udevadm trigger --settle can block for minutes), so kill it.
    step_cancellable.connect(lambda *_args: proc.force_exit())
    if cancellable is not None:
        cancellable.connect(lambda *_args: step_cancellable.cancel())
    proc.communicate_async(None, step_cancellable, on_communicate, None)


def verify_rule_async(path, on_done, cancellable=None):
    """Run `udevadm verify` on path; on_done(RefreshState).

    VERIFIED: accepted. UNSUPPORTED: udevadm is missing or predates the
    `verify` verb — nothing was checked, which is not a rejection. FAILED:
    errored, timed out, or rejected the rule.
    """
    if not check_udevadm():
        print("udev verify: udevadm not available; rule not verified", file=sys.stderr)
        on_done(RefreshState.UNSUPPORTED)
        return
    if _udev_verify_supported is False:
        print(
            "udev verify: this udevadm has no 'verify' verb; rule not verified",
            file=sys.stderr,
        )
        on_done(RefreshState.UNSUPPORTED)
        return
    if _udev_verify_supported is True:
        _verify_rule_run(path, on_done, cancellable)
        return

    _probe_verify_verb(path, on_done, cancellable)


def _verify_rule_run(path, on_done, cancellable=None) -> None:
    """`udevadm verify <path>`. The step already logs the command's output to
    the journal, which is where the verifier names the broken rule."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(f"udev verify: rejected the rule (rc={rc})", file=sys.stderr)
            on_done(RefreshState.FAILED)
        else:
            on_done(RefreshState.VERIFIED)

    run_udev_async([UDEVADM, "verify", path], on_run, cancellable)


def _probe_verify_verb(path, on_done, cancellable=None) -> None:
    """Ask udevadm once per process whether it knows the `verify` verb.

    `udevadm verify --help` exits 0 when the verb exists; an older udevadm
    answers "Unknown command verb" and exits 1. Without this probe the two
    look identical to the caller.
    """

    def on_probe(rc, _out, err):
        global _udev_verify_supported
        _udev_verify_supported = rc == 0
        reason = err.strip()
        print(
            f"udev verify: 'verify' verb supported={_udev_verify_supported}"
            + (f" ({reason})" if reason else ""),
            file=sys.stderr,
        )
        if not _udev_verify_supported:
            on_done(RefreshState.UNSUPPORTED)
            return
        _verify_rule_run(path, on_done, cancellable)

    run_udev_async([UDEVADM, "verify", "--help"], on_probe, cancellable)


def reload_rules_async(on_done, cancellable=None):
    """udevadm control --reload-rules; on_done(ok)."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(
                f"udev refresh: udevadm control --reload-rules failed (rc={rc})",
                file=sys.stderr,
            )
        on_done(rc == 0)

    run_udev_async([UDEVADM, "control", "--reload-rules"], on_run, cancellable)


def trigger_drm_async(on_done, cancellable=None):
    """Re-emit the DRM uevents so udev recomputes every card's tags under the
    current rules. --settle waits for the events to be processed, so the tags
    can be queried right after. Scoped to the drm subsystem so nothing else is
    re-probed. on_done(ok)."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(
                f"udevadm trigger --subsystem-match=drm --settle failed (rc={rc})",
                file=sys.stderr,
            )
        on_done(rc == 0)

    run_udev_async(
        [UDEVADM, "trigger", "--subsystem-match=drm", "--settle"],
        on_run,
        cancellable,
    )


def parse_udev_current_tags(output: str) -> list[str]:
    """The CURRENT_TAGS of a udevadm info --query=property dump as a list.

    CURRENT_TAGS is colon-separated with leading and trailing colons
    (CURRENT_TAGS=:seat:uaccess:), empty when the last uevent set no tags.
    These are the tags mutter reads at session start; the cumulative TAGS line
    can keep a stale tag and is ignored, as is every other line.
    """
    tags = []
    for line in output.splitlines():
        if line.startswith("CURRENT_TAGS="):
            tags = [t for t in line[len("CURRENT_TAGS=") :].split(":") if t]
            break
    return tags


def card_has_tag_async(card, on_done, cancellable=None):
    """Whether the card's current udev tags carry the mutter tag.

    Reads CURRENT_TAGS (see parse_udev_current_tags) of /sys/class/drm/<card>.
    Without the tag mutter will not pick this card even after a re-login, so
    this decides "refresh ok". on_done(has) is False when the tag is absent or
    the query fails: an unverifiable tag is not treated as present.
    """

    def on_run(rc, out, _err):
        if rc != 0:
            print(f"tag check: udevadm info rc={rc} on {card}", file=sys.stderr)
            on_done(False)
            return
        tags = parse_udev_current_tags(out)
        has = MUTTER_TAG in tags
        print(f"tag check: {card} current_tags={tags} has={has}", file=sys.stderr)
        on_done(has)

    run_udev_async(
        [
            UDEVADM,
            "info",
            "--query=property",
            f"--path=/sys/class/drm/{card}",
        ],
        on_run,
        cancellable,
    )


def refresh_udev_async(card, on_done, cancellable=None):
    """Reload the udev rules, re-trigger the DRM devices (settled) and, for
    SetPrimary, check that `card` carries the mutter tag.

    SetAuto passes card=None: other rules may legitimately set the tag.
    on_done(RefreshState): VERIFIED only when every step succeeded;
    UNSUPPORTED when udevadm is unavailable; FAILED when a step failed.
    """
    if not check_udevadm():
        print(
            "udev refresh: udevadm not available; refresh not verified",
            file=sys.stderr,
        )
        on_done(RefreshState.UNSUPPORTED)
        return

    def after_reload(ok):
        if not ok:
            on_done(RefreshState.FAILED)
            return

        def after_trigger(ok):
            if not ok:
                on_done(RefreshState.FAILED)
                return
            if card is None:
                print(
                    "udev refresh: reload-rules and DRM trigger succeeded",
                    file=sys.stderr,
                )
                on_done(RefreshState.VERIFIED)
                return

            def after_tag(has):
                if not has:
                    print(
                        f"udev refresh: tag not present (or unverifiable) "
                        f"on {card} after refresh",
                        file=sys.stderr,
                    )
                    on_done(RefreshState.FAILED)
                    return
                print(
                    "udev refresh: reload-rules, DRM trigger and tag check succeeded",
                    file=sys.stderr,
                )
                on_done(RefreshState.VERIFIED)

            card_has_tag_async(card, after_tag, cancellable)

        trigger_drm_async(after_trigger, cancellable)

    reload_rules_async(after_reload, cancellable)
