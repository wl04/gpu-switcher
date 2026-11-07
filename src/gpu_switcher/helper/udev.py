"""udevadm verification / refresh steps for the helper.

Every step runs as a Gio.Subprocess with a per-step timeout (the shared
budget from core.udev) under a per-step cancellable.

The verification / refresh states. The distinction matters: a refresh that
could not be verified (udevadm missing) must be reported as such and must
never enable the fast-path logout as though the tags were confirmed fresh.
"""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.udev import MUTTER_TAG, UDEVDADM_STEP_TIMEOUT_S

REFRESH_VERIFIED = "verified"  # every step succeeded; tags confirmed
REFRESH_UNSUPPORTED = "unsupported"  # udevadm unavailable; cannot verify
REFRESH_FAILED = "failed"  # a step errored, timed out, or was rejected

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
            _ok, out, err = proc.communicate_finish(result)
            rc = proc.get_exit_status()
        except GLib.Error as error:
            print(f"{argv[0]}: {error}", file=sys.stderr)
            on_done(1, "", str(error))
            return
        print(
            f"{argv[0]} rc={rc} output="
            f"{out.decode('utf-8', 'replace').strip() or err.decode('utf-8', 'replace').strip() or '-'}",
            file=sys.stderr,
        )
        on_done(rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    proc.communicate_async(None, step_cancellable, on_communicate, None)


def verify_rule_async(path, on_done, cancellable=None):
    """Run udevadm verify on the given path and report the verification
    state: REFRESH_VERIFIED when it ran and accepted the rule,
    REFRESH_UNSUPPORTED when udevadm cannot run (not a rejection, but the
    rule is also not confirmed), REFRESH_FAILED when it errored, timed
    out, or rejected the rule."""
    if not check_udevadm():
        print("udev verify: udevadm not available; rule not verified", file=sys.stderr)
        on_done(REFRESH_UNSUPPORTED)
        return

    def on_run(rc, _out, _err):
        if rc != 0:
            print(f"udev verify: rejected the rule (rc={rc})", file=sys.stderr)
            on_done(REFRESH_FAILED)
        else:
            on_done(REFRESH_VERIFIED)

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
    reports REFRESH_VERIFIED only when every step succeeded,
    REFRESH_UNSUPPORTED when udevadm is unavailable (the refresh could not
    be verified, so it must not be treated as fresh), and REFRESH_FAILED
    when a step failed.
    """
    if not check_udevadm():
        print(
            "udev refresh: udevadm not available; refresh not verified",
            file=sys.stderr,
        )
        on_done(REFRESH_UNSUPPORTED)
        return

    def after_reload(ok):
        if not ok:
            on_done(REFRESH_FAILED)
            return

        def after_trigger(ok):
            if not ok:
                on_done(REFRESH_FAILED)
                return
            if card is None:
                print(
                    "udev refresh: reload-rules and DRM trigger succeeded",
                    file=sys.stderr,
                )
                on_done(REFRESH_VERIFIED)
                return

            def after_tag(has):
                if not has:
                    print(
                        f"udev refresh: tag not present (or unverifiable) "
                        f"on {card} after refresh",
                        file=sys.stderr,
                    )
                    on_done(REFRESH_FAILED)
                    return
                print(
                    "udev refresh: reload-rules, DRM trigger and tag check succeeded",
                    file=sys.stderr,
                )
                on_done(REFRESH_VERIFIED)

            card_has_tag_async(card, after_tag, cancellable)

        trigger_drm_async(after_trigger, cancellable)

    reload_rules_async(after_reload, cancellable)
