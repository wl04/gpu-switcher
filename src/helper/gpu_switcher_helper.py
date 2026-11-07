#!/usr/bin/env python3
"""gpu-switcher helper: systemd/D-Bus-activated system-bus service (root).

dbus-broker starts this process on demand (via SystemdService= or Exec=)
when an unprivileged client (the GUI app) calls one of its methods.

Methods:
  Status(locale)            -> (rule_on_disk, message)
  SetPrimary(pci, locale)   -> (ok, message, backup_path, refresh_ok)
  SetAuto(locale)           -> (ok, message, refresh_ok)

SetPrimary and SetAuto also refresh udev (udevadm control --reload-rules
plus a settled, scoped udevadm trigger for the DRM subsystem) under the
SAME authorization, so activating the rule needs no second prompt; a
failed refresh is reported per call (refresh_ok is false) and leaves
reboot as the fallback. SetPrimary additionally verifies that the tag is
present in the chosen card's current udev tags (the tags its last uevent
computed - what mutter reads when the session starts).

Concurrency: each privileged operation claims an in-progress lock before
it starts; a second mutating call arriving while an operation is in flight
gets the com.github.wl04.gpu_switcher.Error.Busy D-Bus error (not queued).
Status stays responsive at all times. All long-running work is
asynchronous (polkit CheckAuthorization via connection.call, udevadm via
Gio.Subprocess.communicate_async with a per-step timeout), so the main
loop - and with it the D-Bus connection's keep-alive - stays alive for
the whole operation. Unhandled exceptions and internal failures are
reported as the com.github.wl04.gpu_switcher.Error.Internal D-Bus error;
the detail is logged to the journal.

Authorization: polkit CheckAuthorization over raw D-Bus with the INTERACTIVE
flag, so the standard agent dialog authenticates the caller. The subject
is the caller's unique bus name (polkitd maps it to the session and user);
a per-invocation cancellation id lets a timed-out check be cancelled so
no stale dialog lingers. The dialog message is localized from the policy
file's <message> elements and the agent's session locale.

Diagnostics: significant steps are written to stderr. The helper runs as
a systemd unit (see debian/gpu-switcher-helper.service) and its stderr is
captured by the journal, so `journalctl` is the reliable record of what
happened.

NOTE on the GDBus API: g_bus_own_name() takes exactly three callbacks -
bus_acquired, name_acquired, name_lost - each (connection, name). There is
NO method-call parameter; method calls are served by the object registered
via connection.register_object(), whose vtable handler is called with
(connection, sender, path, interface, method, params, invocation).
The object must be registered on the bus connection BEFORE g_bus_own_name():
the broker retries a client's pending call the instant the name is acquired,
so the object must already exist (cold-start activation race).

Privileged work mirrors the reference script mutter-primary-gpu.sh: the same
managed rule file, the same rule line format, the same backup scheme, the
same conflict scan, and udevadm verify before the rule is moved into place.
"""

import gettext
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# The shared udev rule module (src/udev_rules.py in the source tree,
# /usr/lib/gpu-switcher/udev_rules.py installed) lives next to this
# package; add the parent directory to sys.path so it can be imported
# without pulling in the GUI package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from udev_rules import (
    DBUS_ERROR_BUSY,
    DBUS_ERROR_INTERNAL,
    G_IO_ERROR_TIMEOUT,
    MANAGED_RULE,
    MUTTER_ENV,
    MUTTER_TAG,
    POLKIT_TIMEOUT_MS,
    UDEVDADM_STEP_TIMEOUT_S,
    is_display_gpu,
    read_text,
    rule_conflicts,
)

SERVICE_NAME = "com.github.wl04.gpu_switcher.Helper"
OBJECT_PATH = "/com/github/wl04/gpu_switcher/Helper"
INTERFACE = "com.github.wl04.gpu_switcher.Helper"

POLKIT_ACTION = "com.github.wl04.gpu_switcher.set-primary"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_INTERACTIVE = 1  # CheckAuthorizationFlags: AllowUserInteraction

LOCALE_BASE = "/usr/share/gpu-switcher/locale"

# Absolute path by policy: the hardening is explicit about what runs, and a
# PATH change must not substitute another udevadm.
UDEVDADM = "/usr/bin/udevadm"
# Availability is checked once per process (in main) and cached.
_udevadm_available = None

INTROSPECTION_XML = f"""<node name="{OBJECT_PATH}">
  <interface name="{INTERFACE}">
    <method name="Status">
      <arg type="s" name="locale" direction="in"/>
      <arg type="s" name="rule_on_disk" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
    <method name="SetPrimary">
      <arg type="s" name="pci" direction="in"/>
      <arg type="s" name="locale" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
      <arg type="s" name="backup_path" direction="out"/>
      <arg type="b" name="refresh_ok" direction="out"/>
    </method>
    <method name="SetAuto">
      <arg type="s" name="locale" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
      <arg type="b" name="refresh_ok" direction="out"/>
    </method>
  </interface>
</node>
"""


# udevadm verification / refresh states. The distinction matters: a
# refresh that could not be verified (udevadm missing) must be reported
# as such and must never enable the fast-path logout as though the tags
# were confirmed fresh.
REFRESH_VERIFIED = "verified"  # every step succeeded; tags confirmed
REFRESH_UNSUPPORTED = "unsupported"  # udevadm unavailable; cannot verify
REFRESH_FAILED = "failed"  # a step errored, timed out, or was rejected

# Keep only the newest backups of the managed rule.
_BACKUP_KEEP = 5

# In-progress operation lock: at most one mutating call (SetPrimary or
# SetAuto) runs at a time; a concurrent one gets the Busy D-Bus error.
_operation_in_progress = False


def _claim_operation() -> bool:
    """Claim the operation lock; False when another operation is in flight."""
    global _operation_in_progress
    if _operation_in_progress:
        return False
    _operation_in_progress = True
    return True


def _release_operation() -> None:
    global _operation_in_progress
    _operation_in_progress = False


def _internal_error(invocation, detail):
    """Reply with the internal-error D-Bus error and log the detail."""
    print("error: " + detail, file=sys.stderr)
    invocation.return_dbus_error(
        DBUS_ERROR_INTERNAL, "Internal error; see the journal."
    )


# The locale arguments arrive from an unauthenticated D-Bus caller. Only
# the catalog languages shipped with the helper are accepted; anything else
# (unknown language, path-shaped value, over-long value) falls back to
# English, so catalog lookups can never leave LOCALE_BASE.
SUPPORTED_LOCALES = ("en", "ru")
_MAX_LOCALE_LEN = 32


def _normalize_locale(locale):
    """Map a caller-supplied locale to a supported catalog language.

    Returns one of SUPPORTED_LOCALES: the language part of a plain
    identifier ('ru_RU.UTF-8' -> 'ru') when it is supported, else 'en'.
    Path-shaped values ('/', '\\', '..') and values over _MAX_LOCALE_LEN
    are rejected: gettext would otherwise use them as path components and
    could open (or block on) files outside LOCALE_BASE.
    """
    if not isinstance(locale, str) or not locale:
        return "en"
    if len(locale) > _MAX_LOCALE_LEN:
        return "en"
    if "/" in locale or "\\" in locale or ".." in locale:
        return "en"
    language = locale.split(".")[0].split("_")[0].split("-")[0]
    return language if language in SUPPORTED_LOCALES else "en"


def _translator(locale):
    """gettext for this helper invocation (locale supplied by the caller).

    The locale is normalized to the allowlisted languages first (see
    _normalize_locale), so catalog loading stays inside LOCALE_BASE.
    """
    language = _normalize_locale(locale)
    langs = [language] if language == "en" else [language, "en"]
    try:
        return gettext.translation(
            "gpu-switcher", LOCALE_BASE, languages=langs, fallback=True
        ).gettext
    except OSError:
        return lambda s: s


def _authorize(connection, sender, locale, on_done, cancellable=None):
    """polkit CheckAuthorization bound to the caller's bus connection.

    The subject is ("system-bus-name", {"name": <unique bus name>}); polkitd
    maps the connection to the session and user. The unique name is stable
    for the life of the connection, so the identity checked is the one that
    made the call (no pid-lookup race). The 5th in-arg is the
    cancellation_id (per the current CheckAuthorization signature:
    in (sa{sv})sa{ss}us), not a locale; the localized dialog text comes
    from the policy file's <message> elements and the agent's session
    locale. Out is one struct (bba{ss}) (authorized, whether_challenge,
    details), so the reply body type is ((bba{ss})).

    Asynchronous: on_done(status, detail) is called from the reply
    callback; status is "authorized", "denied", "cancelled" (dialog
    dismissed), "timeout", or "error"; detail describes a failed check
    and is empty otherwise. The check is issued with the shared
    POLKIT_TIMEOUT_MS budget (the client's budget exceeds it, so this
    timeout always resolves first).
    """
    cancellation_id = uuid.uuid4().hex
    subject = GLib.Variant.new_tuple(
        GLib.Variant("s", "system-bus-name"),
        GLib.Variant("a{sv}", {"name": GLib.Variant("s", sender)}),
    )
    # pygobject's GLib.Variant cannot nest an a{sv} inside a tuple literal
    # (the creator iterates it as an array); build the tuple with
    # GLib.Variant.new_tuple instead.
    params = GLib.Variant.new_tuple(
        subject,
        GLib.Variant("s", POLKIT_ACTION),
        GLib.Variant("a{ss}", []),
        GLib.Variant("u", POLKIT_INTERACTIVE),
        GLib.Variant("s", cancellation_id),
    )
    print(
        f"polkit CheckAuthorization: sender={sender} action={POLKIT_ACTION}",
        file=sys.stderr,
    )

    def on_result(_conn, task):
        try:
            result = _conn.call_finish(task)
        except GLib.Error as exc:
            message = getattr(exc, "message", None) or str(exc)
            if exc.matches(
                GLib.quark_from_string("g-io-error-quark"), G_IO_ERROR_TIMEOUT
            ):
                # G_IO_ERROR_TIMEOUT: the check is still pending in polkitd,
                # cancel it so a stale dialog cannot linger.
                print(f"polkit CheckAuthorization timed out: {exc}", file=sys.stderr)
                if cancellable is not None:
                    cancellable.cancel()
                _cancel_check_authorization(connection, cancellation_id)
                on_done("timeout", "")
                return
            print(f"polkit CheckAuthorization failed: {message}", file=sys.stderr)
            on_done("error", message)
            return
        # Reply body is one struct (b b a{ss}):
        # (authorized, whether_challenge, details).
        authorized, whether_challenge, details = result[0]
        print(
            f"polkit result: authorized={authorized} "
            f"whether_challenge={whether_challenge} details={details!r}",
            file=sys.stderr,
        )
        if not authorized:
            if details.get("polkit.dismissed"):
                on_done("cancelled", "")
                return
            on_done("denied", "")
            return
        on_done("authorized", "")

    connection.call(
        "org.freedesktop.PolicyKit1",
        POLKIT_PATH,
        POLKIT_IFACE,
        "CheckAuthorization",
        params,
        GLib.VariantType("((bba{ss}))"),
        0,
        POLKIT_TIMEOUT_MS,
        cancellable,
        on_result,
        None,
    )


def _cancel_check_authorization(connection, cancellation_id):
    """Best-effort CancelCheckAuthorization after a timeout.

    The result is logged and ignored: the check may already be finished
    (denied/cancelled between our timeout and the call), and the reply
    carries no data (in-arg s only).
    """
    try:
        connection.call_sync(
            "org.freedesktop.PolicyKit1",
            POLKIT_PATH,
            POLKIT_IFACE,
            "CancelCheckAuthorization",
            GLib.Variant("(s)", (cancellation_id,)),
            GLib.VariantType("()"),
            0,
            5000,
            None,
        )
        print(f"CancelCheckAuthorization: {cancellation_id}", file=sys.stderr)
    except GLib.Error as exc:
        print(f"CancelCheckAuthorization failed: {exc}", file=sys.stderr)


_PCI_ADDRESS_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9]")


def is_valid_pci_format(pci: str) -> bool:
    """Full `DDDD:BB:DD.D` PCI address (domain included, lowercase hex).

    The exact form the managed rule and udev KERNELS== matching use;
    domain-omitted or non-hex addresses are rejected before any /sys read.
    """
    return _PCI_ADDRESS_RE.fullmatch(pci) is not None


def _resolve_gpu(pci):
    """(card, vendor, device) for a valid display controller, else None.

    Thin wrapper over the shared is_display_gpu contract (display class,
    bound driver, exactly one /dev/dri card for this PCI); the PCI address
    itself is validated first so no /sys read happens on bad input.
    """
    if not is_valid_pci_format(pci):
        return None
    card = is_display_gpu(pci)
    if card is None:
        return None
    pdir = f"/sys/bus/pci/devices/{pci}"
    vendor = read_text(f"{pdir}/vendor")
    device = read_text(f"{pdir}/device")
    return card, vendor, device


def _rule_text(pci, vendor, device):
    # Same rule line format as the reference script mutter-primary-gpu.sh.
    return (
        f"# Managed by gpu-switcher; selected PCI {pci}\n"
        "# Mutter uses TAG; ENV is retained for compatibility with the previous local rule.\n"
        'SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", KERNEL=="card[0-9]*", '
        'KERNEL!="card*-*", SUBSYSTEMS=="pci", KERNELS=="' + pci + '",'
        ' ATTRS{vendor}=="' + vendor + '",'
        ' ATTRS{device}=="' + device + '",'
        f' ENV{{{MUTTER_ENV}}}="1",'
        f' TAG+="{MUTTER_TAG}"\n'
    )


def _check_udevadm() -> bool:
    """Whether udevadm is available; checked once per process."""
    global _udevadm_available
    if _udevadm_available is None:
        _udevadm_available = os.path.exists(UDEVDADM)
        print(f"udevadm at {UDEVDADM}: available={_udevadm_available}", file=sys.stderr)
    return _udevadm_available


def _run_udev_async(argv, on_done, cancellable=None):
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


def _verify_rule_async(path, on_done, cancellable=None):
    """Run udevadm verify on the given path and report the verification
    state: REFRESH_VERIFIED when it ran and accepted the rule,
    REFRESH_UNSUPPORTED when udevadm cannot run (not a rejection, but the
    rule is also not confirmed), REFRESH_FAILED when it errored, timed
    out, or rejected the rule."""
    if not _check_udevadm():
        print("udev verify: udevadm not available; rule not verified", file=sys.stderr)
        on_done(REFRESH_UNSUPPORTED)
        return

    def on_run(rc, _out, _err):
        if rc != 0:
            print(f"udev verify: rejected the rule (rc={rc})", file=sys.stderr)
            on_done(REFRESH_FAILED)
        else:
            on_done(REFRESH_VERIFIED)

    _run_udev_async([UDEVDADM, "verify", path], on_run, cancellable)


def _reload_rules_async(on_done, cancellable=None):
    """udevadm control --reload-rules; on_done(ok)."""

    def on_run(rc, _out, _err):
        if rc != 0:
            print(
                f"udev refresh: udevadm control --reload-rules failed (rc={rc})",
                file=sys.stderr,
            )
        on_done(rc == 0)

    _run_udev_async([UDEVDADM, "control", "--reload-rules"], on_run, cancellable)


def _trigger_drm_async(on_done, cancellable=None):
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

    _run_udev_async(
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


def _card_has_tag_async(card, on_done, cancellable=None):
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

    _run_udev_async(
        [
            UDEVDADM,
            "info",
            "--query=property",
            f"--path=/sys/class/drm/{card}",
        ],
        on_run,
        cancellable,
    )


def _refresh_udev_async(card, on_done, cancellable=None):
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
    if not _check_udevadm():
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

            _card_has_tag_async(card, after_tag, cancellable)

        _trigger_drm_async(after_trigger, cancellable)

    _reload_rules_async(after_reload, cancellable)


def _backup_rule():
    """Copy the existing managed rule to <RULE>.bak.<UTC-ts>.<suffix>; '' on failure."""
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


def _retain_backups(keep: int = _BACKUP_KEEP) -> None:
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
    for stale in backups[:-keep] if len(backups) > keep else []:
        try:
            os.unlink(os.path.join(directory, stale))
        except OSError:
            pass


def _fsync_dir(path):
    """fsync a directory (makes a rename in it durable); best effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _authorization_message(tr, status, detail):
    """Localized reply for a call that was not authorized."""
    if status == "denied":
        return tr("Authorization was denied. Nothing changed.")
    if status == "cancelled":
        return tr("Authorization was cancelled. Nothing changed.")
    if status == "timeout":
        return tr("The authorization check timed out. Nothing changed.")
    return tr("Could not check authorization (%s). Nothing changed.") % detail


def _refresh_message(tr, state, *, leading, backup="", verified_sentence):
    """Full localized reply after a udev refresh.

    The reply is composed of the site's leading sentence, the optional
    backup sentence, and the sentence describing the refresh outcome,
    joined by single spaces. The catalog entries (po/) are the sentence
    fragments chosen so the composition reproduces the full localized
    sentence exactly in every shipped language. `state` is a REFRESH_*
    constant; `backup` is '' when there is no backup.
    """
    prefix = leading
    if backup:
        prefix += " " + tr("A backup was saved to %s.") % backup
    if state == REFRESH_UNSUPPORTED:
        return (
            prefix
            + " "
            + tr(
                "The udev refresh could not be verified "
                "(udevadm unavailable); a reboot is required."
            )
        )
    if state == REFRESH_FAILED:
        return prefix + " " + tr("The udev refresh failed; a reboot is required.")
    return prefix + " " + verified_sentence


def handle_status(connection, sender, params, invocation) -> None:
    locale = params[0]
    tr = _translator(locale)
    rule = read_text(MANAGED_RULE)
    if rule:
        message = tr("Managed rule present.")
    else:
        message = tr("No managed rule (auto selection).")
    invocation.return_value(GLib.Variant("(ss)", (rule, message)))


def _write_rule_async(pci, content, card, tr, on_done, cancellable=None):
    """Write the managed rule transactionally, then refresh udev.

    Steps: temp file in the same directory (fsynced before the rename),
    udevadm verify (async), back up the existing rule and retain the
    newest _BACKUP_KEEP, rename into place, fsync the directory, then the
    async udev refresh. on_done(ok, message, backup_path, refresh_ok).
    """

    def move_into_place(tmp_path, backup):
        try:
            os.rename(tmp_path, MANAGED_RULE)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            print("SetPrimary: failed to move the rule into place", file=sys.stderr)
            on_done(
                False,
                tr("Failed to write the managed rule; nothing changed."),
                "",
                False,
            )
            return
        _fsync_dir(os.path.dirname(MANAGED_RULE))
        print(
            f"SetPrimary: rule written for {pci} (backup: {backup or 'none'})",
            file=sys.stderr,
        )

        def on_refresh(state):
            refresh_ok = state == REFRESH_VERIFIED
            message = _refresh_message(
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

        _refresh_udev_async(card, on_refresh, cancellable)

    def after_verify(state, tmp_path):
        if state == REFRESH_FAILED:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            print("SetPrimary: udevadm verify rejected the rule", file=sys.stderr)
            on_done(
                False,
                tr("udevadm verify rejected the rule; nothing changed."),
                "",
                False,
            )
            return
        if os.path.exists(MANAGED_RULE):
            backup = _backup_rule()
            if backup == "":
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                print("SetPrimary: backup of the existing rule failed", file=sys.stderr)
                on_done(
                    False,
                    tr("Backup of the existing rule failed; nothing changed."),
                    "",
                    False,
                )
                return
            _retain_backups()
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
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print("SetPrimary: failed to write the temporary rule", file=sys.stderr)
        on_done(
            False, tr("Failed to write the managed rule; nothing changed."), "", False
        )
        return
    _verify_rule_async(
        tmp_path, lambda state: after_verify(state, tmp_path), cancellable
    )


def handle_set_primary(connection, sender, params, invocation) -> None:
    pci, locale = params
    tr = _translator(locale)
    print(f"SetPrimary: pci={pci} locale={locale} sender={sender}", file=sys.stderr)
    if not _claim_operation():
        print("SetPrimary: operation already in progress; rejecting", file=sys.stderr)
        invocation.return_dbus_error(
            DBUS_ERROR_BUSY, "Another operation is in progress."
        )
        return
    cancellable = invocation.get_cancellable()

    def finish(ok, message, backup, refresh_ok):
        _release_operation()
        _reset_idle_timer()
        invocation.return_value(
            GLib.Variant("(bssb)", (ok, message, backup, refresh_ok))
        )

    def on_error(message):
        finish(False, message, "", False)

    def on_auth(status, detail):
        if status != "authorized":
            print(
                f"SetPrimary: authorization {status}; nothing changed", file=sys.stderr
            )
            on_error(_authorization_message(tr, status, detail))
            return
        print("SetPrimary: authorized", file=sys.stderr)
        # Root cannot skip unreadable rules: they may set the tag.
        conflicts = rule_conflicts(on_unreadable="flag")
        if conflicts:
            print(f"SetPrimary: conflicts found: {conflicts}", file=sys.stderr)
            on_error(
                tr(
                    "Another udev rule sets the preferred GPU: %s. Resolve it manually first."
                )
                % conflicts[0]
            )
            return
        resolved = _resolve_gpu(pci)
        if resolved is None:
            print(f"SetPrimary: invalid PCI {pci}", file=sys.stderr)
            on_error(tr("Target PCI %s is not a valid display controller.") % pci)
            return
        _card, vendor, device = resolved
        print(
            f"SetPrimary: resolved {pci} -> card={_card} vendor={vendor} device={device}",
            file=sys.stderr,
        )
        if os.path.islink(MANAGED_RULE):
            print("SetPrimary: managed rule is a symlink; refusing", file=sys.stderr)
            on_error(tr("The managed rule is a symlink; refusing to modify it."))
            return
        content = _rule_text(pci, vendor, device)
        # Read as bytes: the existing rule may hold invalid UTF-8 (a broken
        # hand edit); comparing bytes keeps the overwrite path working.
        try:
            with open(MANAGED_RULE, "rb") as f:
                existing = f.read()
        except OSError:
            existing = b""
        if existing == content.encode():
            print(
                f"SetPrimary: rule already set for {pci}; nothing to do",
                file=sys.stderr,
            )

            def on_refresh_idempotent(state):
                finish(
                    True,
                    _refresh_message(
                        tr,
                        state,
                        leading=tr("The rule is already set for %s.") % pci,
                        verified_sentence=tr("Udev rules reloaded."),
                    ),
                    "",
                    state == REFRESH_VERIFIED,
                )

            _refresh_udev_async(_card, on_refresh_idempotent, cancellable)
            return
        _write_rule_async(pci, content, _card, tr, finish, cancellable)

    sender_bus = invocation.get_message().get_sender()
    _authorize(connection, sender_bus, locale, on_auth, cancellable)


def handle_set_auto(connection, sender, params, invocation) -> None:
    locale = params[0]
    tr = _translator(locale)
    print(f"SetAuto: locale={locale} sender={sender}", file=sys.stderr)
    if not _claim_operation():
        print("SetAuto: operation already in progress; rejecting", file=sys.stderr)
        invocation.return_dbus_error(
            DBUS_ERROR_BUSY, "Another operation is in progress."
        )
        return
    cancellable = invocation.get_cancellable()

    def finish(ok, message, refresh_ok):
        _release_operation()
        _reset_idle_timer()
        invocation.return_value(GLib.Variant("(bsb)", (ok, message, refresh_ok)))

    def on_error(message):
        finish(False, message, False)

    def on_auth(status, detail):
        if status != "authorized":
            print(f"SetAuto: authorization {status}; nothing changed", file=sys.stderr)
            on_error(_authorization_message(tr, status, detail))
            return
        print("SetAuto: authorized", file=sys.stderr)
        if os.path.islink(MANAGED_RULE):
            print("SetAuto: managed rule is a symlink; refusing", file=sys.stderr)
            on_error(tr("The managed rule is a symlink; refusing to remove it."))
            return
        if not os.path.exists(MANAGED_RULE):
            print("SetAuto: no managed rule; already auto", file=sys.stderr)
            # A previous Reset may have removed the rule while its udev
            # refresh failed, so the stale tags persist; refresh udev on
            # the already-absent path too (as SetPrimary does for an
            # unchanged rule).

            def on_refresh_already_auto(state):
                finish(
                    True,
                    _refresh_message(
                        tr,
                        state,
                        leading=tr(
                            "No managed rule (auto selection already in effect)."
                        ),
                        verified_sentence=tr("Udev rules reloaded."),
                    ),
                    state == REFRESH_VERIFIED,
                )

            _refresh_udev_async(None, on_refresh_already_auto, cancellable)
            return
        backup = _backup_rule()
        if backup == "":
            print("SetAuto: backup of the existing rule failed", file=sys.stderr)
            on_error(tr("Backup of the existing rule failed; nothing removed."))
            return
        try:
            os.unlink(MANAGED_RULE)
        except OSError:
            print("SetAuto: failed to remove the managed rule", file=sys.stderr)
            on_error(tr("Failed to remove the managed rule; nothing changed."))
            return
        print(f"SetAuto: rule removed (backup: {backup})", file=sys.stderr)

        def on_refresh_removed(state):
            finish(
                True,
                _refresh_message(
                    tr,
                    state,
                    leading=tr("Managed rule removed."),
                    backup=backup,
                    verified_sentence=tr(
                        "Udev rules reloaded; auto selection applies after the next "
                        "login (or after a reboot)."
                    ),
                ),
                state == REFRESH_VERIFIED,
            )

        _refresh_udev_async(None, on_refresh_removed, cancellable)

    sender_bus = invocation.get_message().get_sender()
    _authorize(connection, sender_bus, locale, on_auth, cancellable)


# A D-Bus-activated helper stays alive as the name owner between calls; if it
# never exits, it keeps serving stale code after a package upgrade. Quit
# shortly after the last call: the broker re-activates a fresh process from
# the on-disk code on the next call.
IDLE_TIMEOUT_MS = 60000
_IDLE_SOURCE = None


def _reset_idle_timer():
    global _IDLE_SOURCE
    if _IDLE_SOURCE is not None:
        GLib.source_remove(_IDLE_SOURCE)

    def on_idle():
        print(
            "idle timeout; exiting so the next call re-activates fresh code",
            file=sys.stderr,
        )
        if _LOOP is not None:
            _LOOP.quit()
        return False

    _IDLE_SOURCE = GLib.timeout_add(IDLE_TIMEOUT_MS, on_idle)


def handle_method_call(
    connection, sender, object_path, interface_name, method_name, params, invocation
) -> None:
    print(f"method call: {method_name} from {sender}", file=sys.stderr)
    try:
        if method_name == "Status":
            handle_status(connection, sender, params, invocation)
        elif method_name == "SetPrimary":
            handle_set_primary(connection, sender, params, invocation)
        elif method_name == "SetAuto":
            handle_set_auto(connection, sender, params, invocation)
        # GDBus dispatches only the methods declared in the introspection
        # node, so there is no "unknown method" case here.
    except Exception:
        _internal_error(
            invocation,
            f"unhandled exception in {method_name}:\n{traceback.format_exc()}",
        )
    _reset_idle_timer()


_LOOP = None


def on_bus_acquired(connection, name) -> None:
    print(f"connected to system bus (name: {name})", file=sys.stderr)


def on_name_acquired(connection, name) -> None:
    print(f"name acquired: {name}; ready to serve calls", file=sys.stderr)


def on_name_lost(connection, name) -> None:
    where = (
        "on the owning connection" if connection is not None else "connection closed"
    )
    print(f"name lost: {name} {where}; exiting", file=sys.stderr)
    if _LOOP is not None:
        _LOOP.quit()


def main() -> None:
    global _LOOP
    print(f"helper starting (uid={os.getuid()}, argv={sys.argv})", file=sys.stderr)
    _check_udevadm()  # single availability check, cached for the process
    _LOOP = GLib.MainLoop.new(None, False)
    # Register the object on the bus connection BEFORE owning the name:
    # the broker retries a client's pending call the instant the name is
    # acquired, so the object must already exist (cold-start race).
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    connection.register_object(
        OBJECT_PATH, node.interfaces[0], handle_method_call, None, None
    )
    print(
        f"object {OBJECT_PATH} registered; requesting name {SERVICE_NAME}",
        file=sys.stderr,
    )
    # g_bus_own_name(bus_type, name, flags, bus_acquired, name_acquired,
    # name_lost): three (connection, name) callbacks, no method handler.
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        SERVICE_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )
    # Arm the idle timer right away: a D-Bus-answered call (gdbus
    # introspect) is not a handled call, and the helper must still exit
    # after IDLE_TIMEOUT_MS instead of owning the name forever.
    _reset_idle_timer()
    _LOOP.run()
    print("main loop ended", file=sys.stderr)


if __name__ == "__main__":
    main()
