"""udevadm verification / refresh steps for the helper.

Every step runs as a Gio.Subprocess with a per-step timeout (the shared
budget from core.udev) under a per-step cancellable. The RefreshState
outcome distinguishes a refresh that could not be verified (udevadm
missing) from one that confirmed the tags: the former must never enable
the fast-path logout as though the tags were fresh.
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

from gpu_switcher.core.udev import MUTTER_TAG, UDEVDADM_STEP_TIMEOUT_S


class RefreshState(Enum):
    """Verification / refresh state of the udevadm steps.

    The distinction matters: a refresh that could not be verified (udevadm
    missing) must be reported as such and must never enable the fast-path
    logout as though the tags were confirmed fresh.
    """

    VERIFIED = "verified"  # every step succeeded; tags confirmed
    UNSUPPORTED = "unsupported"  # udevadm unavailable; cannot verify
    FAILED = "failed"  # a step errored, timed out, or was rejected


# Absolute path by policy: the hardening is explicit about what runs, and a
# PATH change must not substitute another udevadm.
UDEVDADM = "/usr/bin/udevadm"
# Availability is checked once per process (in main) and cached.
_udevadm_available = None


def check_udevadm() -> bool:
    """Whether udevadm is available; checked once per process."""
    global _udevadm_available
    if _udevadm_available is None:
        _udevadm_available = os.path.exists(UDEVDADM)
        print(f"udevadm at {UDEVDADM}: available={_udevadm_available}", file=sys.stderr)
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
    """Decode a subprocess output stream to text.

    communicate_finish hands back GLib.Bytes (or None for an empty stream),
    never Python bytes; normalize both to a str before decoding.
    """
    if stream is None:
        return ""
    raw = stream if isinstance(stream, bytes) else stream.get_data()
    return raw.decode("utf-8", "replace")


def run_udev_async(argv, on_done, cancellable=None):
    """Run one udevadm step asynchronously under the per-step timeout.

    on_done(rc, stdout, stderr) is called when the step finishes; rc is
    the exit status, or 1 (with an explanatory stderr) when the step could
    not run or timed out (the per-step cancellable is then cancelled and
    the child killed). The output is logged to the journal (the helper's
    stderr) for diagnostics.
    """
    if cancellable is not None and cancellable.is_cancelled():
        on_done(1, "", "cancelled before start")
        return
    step_cancellable = Gio.Cancellable.new()
    timeout_source = GLib.timeout_add(
        UDEVDADM_STEP_TIMEOUT_S * 1000, step_cancellable.cancel
    )
    try:
        proc = Gio.Subprocess.new(
            list(argv),
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as error:
        GLib.source_remove(timeout_source)
        print(f"{argv[0]}: failed to start: {error}", file=sys.stderr)
        on_done(1, "", str(error))
        return

    def on_communicate(_source, result, _ud):
        GLib.source_remove(timeout_source)
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
    """Run udevadm verify on the given path and report the verification
    state: RefreshState.VERIFIED when it ran and accepted the rule,
    RefreshState.UNSUPPORTED when udevadm cannot run (not a rejection, but the
    rule is also not confirmed), RefreshState.FAILED when it errored, timed
    out, or rejected the rule."""
    if not check_udevadm():
        print("udev verify: udevadm not available; rule not verified", file=sys.stderr)
        on_done(RefreshState.UNSUPPORTED)
        return

    def on_run(rc, _out, _err):
        if rc != 0:
            print(f"udev verify: rejected the rule (rc={rc})", file=sys.stderr)
            on_done(RefreshState.FAILED)
        else:
            on_done(RefreshState.VERIFIED)

    run_udev_async([UDEVDADM, "verify", path], on_run, cancellable)


def reload_rules_async(on_done, cancellable=None):
    """udevadm control --reload-rules; on_done(ok)."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(
                f"udev refresh: udevadm control --reload-rules failed (rc={rc})",
                file=sys.stderr,
            )
        on_done(rc == 0)

    run_udev_async([UDEVDADM, "control", "--reload-rules"], on_run, cancellable)


def trigger_drm_async(on_done, cancellable=None):
    """Re-emit DRM kernel events so udev re-runs the rules and refreshes
    the persistent device database with the new tags. Scoped to the DRM
    subsystem: only display devices are touched, nothing else is
    re-probed. --settle waits for the triggered events to be processed
    before returning, so the tag is queryable right after the trigger.
    on_done(ok)."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(
                f"udevadm trigger --subsystem-match=drm --settle failed (rc={rc})",
                file=sys.stderr,
            )
        on_done(rc == 0)

    run_udev_async(
        [UDEVDADM, "trigger", "--subsystem-match=drm", "--settle"],
        on_run,
        cancellable,
    )


def parse_udev_current_tags(output: str) -> list[str]:
    """The CURRENT_TAGS of a udevadm info --query=property dump as a list.

    The dump is KEY=VALUE lines; CURRENT_TAGS is colon-separated with a
    leading and trailing colon (e.g. CURRENT_TAGS=:seat:uaccess:) and empty
    when the device's last uevent set no tags. These are the tags mutter
    reads when the session starts; the cumulative TAGS line may still carry
    a tag no rule currently matches and is ignored here. Lines other than
    CURRENT_TAGS are ignored.
    """
    tags = []
    for line in output.splitlines():
        if line.startswith("CURRENT_TAGS="):
            tags = [t for t in line[len("CURRENT_TAGS=") :].split(":") if t]
            break
    return tags


def card_has_tag_async(card, on_done, cancellable=None):
    """Whether the card's current udev tags carry the mutter tag.

    Runs udevadm info --query=property on /sys/class/drm/<card> and checks
    CURRENT_TAGS - the tags the last uevent computed, which is what mutter
    reads when the session starts. (The cumulative TAGS line may still show
    a stale tag from a previous rule; it is irrelevant to mutter.) This is
    the check that makes "refresh ok" true: without the tag, mutter will
    not select the primary even after a relogin. on_done(has) reports
    False when the tag is absent OR when it cannot be verified (the query
    fails) - an unverifiable tag must not be treated as present.
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
            UDEVDADM,
            "info",
            "--query=property",
            f"--path=/sys/class/drm/{card}",
        ],
        on_run,
        cancellable,
    )


def refresh_udev_async(card, on_done, cancellable=None):
    """Reload the udev rules, re-trigger (settled) the DRM devices, and
    verify the new rule's tag is present in the card's current udev tags
    (what mutter reads after a re-login).

    Scoped to the DRM subsystem: only display devices are touched, nothing
    else is re-probed. When `card` is given (SetPrimary), the tag is
    checked on that card after the settled trigger; SetAuto passes no
    card, since other rules may legitimately set the tag. on_done(state)
    reports RefreshState.VERIFIED only when every step succeeded,
    RefreshState.UNSUPPORTED when udevadm is unavailable (the refresh could not
    be verified, so it must not be treated as fresh), and RefreshState.FAILED
    when a step failed.
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
