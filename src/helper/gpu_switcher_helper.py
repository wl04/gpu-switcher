#!/usr/bin/env python3
"""gpu-switcher helper: D-Bus-activated system-bus service (runs as root).

dbus-broker starts this process on demand when an unprivileged client (the
GUI app) calls one of its methods.

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

Authorization: polkit CheckAuthorization over raw D-Bus with the INTERACTIVE
flag, so the standard agent dialog authenticates the caller. The subject
is the caller's unique bus name (polkitd maps it to the session and user);
a per-invocation cancellation id lets a timed-out check be cancelled so
no stale dialog lingers. The dialog message is localized from the policy
file's <message> elements and the agent's session locale.

Diagnostics: every significant step is written to stderr AND to
/var/log/gpu-switcher-helper.log (created at runtime; the helper runs as
root and can always write it). The log file is the reliable record of what
happened: the journal does not reliably capture the stderr of D-Bus-
activated services, so on any failure check the log file first.

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
import subprocess
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
    DISPLAY_PCI_CLASSES,
    MANAGED_RULE,
    MUTTER_ENV,
    MUTTER_TAG,
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
LOG_PATH = "/var/log/gpu-switcher-helper.log"

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


# One log line is capped: caller-supplied strings (pci, locale) arrive
# unauthenticated and may be arbitrarily long; the line is also flattened
# so embedded newlines cannot forge separate entries.
_LOG_MAX_CHARS = 2000
# The log is bounded: when it outgrows the cap it is rotated once
# (overwriting the previous rotation), so at most two files exist.
_LOG_MAX_BYTES = 1024 * 1024
# A D-Bus client can invoke the helper without authorization, so per-call
# logging is rate-limited per (sender, method) window.
_METHOD_CALL_WINDOW_S = 60.0
_METHOD_CALL_LIMIT = 20
_method_call_counts: dict[
    tuple[str, str], list
] = {}  # (sender, method) -> [count, window_start, noted]

# udevadm verification / refresh states. The distinction matters: a
# refresh that could not be verified (udevadm missing) must be reported
# as such and must never enable the fast-path logout as though the tags
# were confirmed fresh.
REFRESH_VERIFIED = "verified"  # every step succeeded; tags confirmed
REFRESH_UNSUPPORTED = "unsupported"  # udevadm unavailable; cannot verify
REFRESH_FAILED = "failed"  # a step errored, timed out, or was rejected


def _sanitize_log_text(message):
    """One log line, bounded and flattened.

    Newlines and carriage returns are rendered literally (an embedded
    newline could otherwise forge a separate entry), other control
    characters are dropped, and the line is capped at _LOG_MAX_CHARS so a
    single call cannot grow the log file without bound.
    """
    text = message.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch for ch in text if ch.isprintable())
    if len(text) > _LOG_MAX_CHARS:
        text = text[:_LOG_MAX_CHARS] + "…[truncated]"
    return text


def _maybe_rotate_log():
    """Rotate the log once when it outgrows _LOG_MAX_BYTES (best effort)."""
    try:
        if os.path.getsize(LOG_PATH) > _LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def _log(message):
    """Append one diagnostic line to stderr and /var/log/gpu-switcher-helper.log.

    Best-effort by design: logging must never crash the helper itself.
    The message is bounded and flattened first (see _sanitize_log_text).
    """
    line = "gpu-switcher-helper %s %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        _sanitize_log_text(message),
    )
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        _maybe_rotate_log()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _method_call_allowed(sender, method):
    """Rate-limit the per-call diagnostic line for this (sender, method).

    A client may invoke the helper repeatedly without authorization, so
    after _METHOD_CALL_LIMIT calls in the window the line is suppressed;
    the suppression is noted in the log once per window.
    """
    key = (sender, method)
    now = time.monotonic()
    entry = _method_call_counts.get(key)
    if entry is None or now - entry[1] > _METHOD_CALL_WINDOW_S:
        entry = [0, now, False]
        _method_call_counts[key] = entry
        for stale in [
            k
            for k, e in _method_call_counts.items()
            if now - e[1] > _METHOD_CALL_WINDOW_S * 2
        ]:
            del _method_call_counts[stale]
    if entry[0] >= _METHOD_CALL_LIMIT:
        if not entry[2]:
            _log(
                "rate limit: suppressing further %s call logs from %s "
                "for %ds" % (method, sender, int(_METHOD_CALL_WINDOW_S))
            )
            entry[2] = True
        return False
    entry[0] += 1
    return True


def _internal_error(invocation, detail):
    """Reply with a D-Bus error the caller can display, and log the detail."""
    _log("error: " + detail)
    invocation.return_gerror(
        GLib.Error.new_literal(
            GLib.quark_from_string("gpu-switcher-error"),
            "Internal error; see /var/log/gpu-switcher-helper.log",
            1,
        )
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


def _authorize(connection, sender, locale):
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

    Returns (status, detail): status is "authorized", "denied", "cancelled"
    (dialog dismissed), "timeout", or "error"; detail describes a failed
    check and is empty otherwise.
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
    _log(f"polkit CheckAuthorization: sender={sender} action={POLKIT_ACTION}")
    try:
        result = connection.call_sync(
            "org.freedesktop.PolicyKit1",
            POLKIT_PATH,
            POLKIT_IFACE,
            "CheckAuthorization",
            params,
            GLib.VariantType("((bba{ss}))"),
            0,
            170000,
            None,
        )
    except GLib.Error as exc:
        if exc.matches(GLib.quark_from_string("g-io-error-quark"), 5):
            # G_IO_ERROR_TIMEOUT: the check is still pending in polkitd,
            # cancel it so a stale dialog cannot linger.
            _log(f"polkit CheckAuthorization timed out: {exc}")
            _cancel_check_authorization(connection, cancellation_id)
            return "timeout", ""
        _log(f"polkit CheckAuthorization failed: {exc}")
        return "error", str(exc)
    # Reply body is one struct (b b a{ss}):
    # (authorized, whether_challenge, details).
    authorized, whether_challenge, details = result[0]
    _log(
        f"polkit result: authorized={authorized} "
        f"whether_challenge={whether_challenge} details={details!r}"
    )
    if not authorized:
        if details.get("polkit.dismissed"):
            return "cancelled", ""
        return "denied", ""
    return "authorized", ""


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
        _log(f"CancelCheckAuthorization: {cancellation_id}")
    except GLib.Error as exc:
        _log(f"CancelCheckAuthorization failed: {exc}")


def _authorized(invocation, connection, locale):
    """polkit authorization result for the caller of this invocation.

    Returns (status, detail) from _authorize; status is "authorized",
    "denied", "cancelled", "timeout", or "error".
    """
    sender = invocation.get_message().get_sender()
    return _authorize(connection, sender, locale)


_PCI_ADDRESS_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9]")


def is_valid_pci_format(pci: str) -> bool:
    """Full `DDDD:BB:DD.D` PCI address (domain included, lowercase hex).

    The exact form the managed rule and udev KERNELS== matching use;
    domain-omitted or non-hex addresses are rejected before any /sys read.
    """
    return _PCI_ADDRESS_RE.fullmatch(pci) is not None


def _resolve_gpu(pci):
    """(card, vendor, device) for a valid display controller, else None.

    Mirrors the reference script, extended to the 3D controller class:
    class 0x030000 or 0x030200, a bound driver, and exactly one
    /dev/dri/card* device bound to this PCI device.
    """
    if not is_valid_pci_format(pci):
        return None
    pdir = f"/sys/bus/pci/devices/{pci}"
    if not os.path.isdir(pdir):
        return None
    vendor = read_text(f"{pdir}/vendor")
    device = read_text(f"{pdir}/device")
    if read_text(f"{pdir}/class") not in DISPLAY_PCI_CLASSES:
        return None
    driver_link = f"{pdir}/driver"
    if not os.path.islink(driver_link):
        return None
    real = os.path.realpath(pdir)
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return None
    card = None
    for entry in entries:
        if not re.fullmatch(r"card[0-9]+", entry):
            continue
        if os.path.realpath(f"/sys/class/drm/{entry}/device") != real:
            continue
        if not os.path.exists(f"/dev/dri/{entry}"):
            continue
        card = entry
    if card is None:
        return None
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


def _verify_rule(path):
    """Run udevadm verify and report the verification state.

    Returns REFRESH_VERIFIED when udevadm verify ran and accepted the
    rule, REFRESH_UNSUPPORTED when udevadm cannot run (verification is
    not possible - not a rejection, but the rule is also not confirmed),
    and REFRESH_FAILED when udevadm verify errored, timed out, or
    rejected the rule.
    """
    if shutil.which("udevadm") is None:
        _log("udev verify: udevadm not available; rule not verified")
        return REFRESH_UNSUPPORTED
    try:
        out = subprocess.run(
            ["udevadm", "verify", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _log("udev verify: udevadm verify failed to run")
        return REFRESH_FAILED
    if out.returncode != 0:
        _log(f"udev verify: udevadm verify rejected the rule (rc={out.returncode})")
        return REFRESH_FAILED
    return REFRESH_VERIFIED


def _reload_rules():
    try:
        out = subprocess.run(
            ["udevadm", "control", "--reload-rules"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _trigger_drm():
    """Re-emit DRM kernel events so udev re-runs the rules and refreshes
    the persistent device database with the new tags.

    Scoped to the DRM subsystem: only display devices are touched, nothing
    else is re-probed. --settle waits for the triggered events to be
    processed before returning (bounded by the 30 s subprocess timeout),
    so the tag is queryable right after the trigger. Returns True when the
    trigger ran cleanly.
    """
    try:
        out = subprocess.run(
            ["udevadm", "trigger", "--subsystem-match=drm", "--settle"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    _log(
        f"udevadm trigger --subsystem-match=drm --settle rc={out.returncode} "
        f"output={out.stdout.strip() or out.stderr.strip() or '-'}"
    )
    return out.returncode == 0


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


def _card_has_tag(card):
    """Whether the card's current udev tags carry the mutter tag.

    Runs udevadm info --query=property on /sys/class/drm/<card> and checks
    CURRENT_TAGS - the tags the last uevent computed, which is what mutter
    reads when the session starts. (The cumulative TAGS line may still show
    a stale tag from a previous rule; it is irrelevant to mutter.) This is
    the check that makes "refresh ok" true: without the tag, mutter will
    not select the primary even after a relogin. Returns False when the tag
    is absent OR when it cannot be verified (udevadm unavailable or the
    query fails) - an unverifiable tag must not be treated as present.
    """
    if shutil.which("udevadm") is None:
        _log(f"tag check: udevadm not available; cannot verify tag on {card}")
        return False
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--path=/sys/class/drm/{card}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _log(f"tag check: udevadm info failed on /sys/class/drm/{card}")
        return False
    if out.returncode != 0:
        _log(f"tag check: udevadm info rc={out.returncode} on {card}")
        return False
    tags = parse_udev_current_tags(out.stdout)
    has = MUTTER_TAG in tags
    _log(f"tag check: {card} current_tags={tags} has={has}")
    return has


def _refresh_udev(card=None):
    """Reload the udev rules, re-trigger (settled) the DRM devices, and
    verify the new rule's tag is present in the card's current udev tags
    (what mutter reads after a re-login).

    Scoped to the DRM subsystem: only display devices are touched, nothing
    else is re-probed. When `card` is given (SetPrimary), the tag is checked
    on that card after the settled trigger; SetAuto passes no card, since
    other rules may legitimately set the tag. Returns REFRESH_VERIFIED only
    when every step succeeded, REFRESH_UNSUPPORTED when udevadm is
    unavailable (the refresh could not be verified, so it must not be
    treated as fresh), and REFRESH_FAILED when a step failed.
    """
    if shutil.which("udevadm") is None:
        _log("udev refresh: udevadm not available; refresh not verified")
        return REFRESH_UNSUPPORTED
    if not _reload_rules():
        _log("udev refresh: udevadm control --reload-rules failed")
        return REFRESH_FAILED
    if not _trigger_drm():
        _log("udev refresh: udevadm trigger --subsystem-match=drm --settle failed")
        return REFRESH_FAILED
    if card is not None and not _card_has_tag(card):
        _log(f"udev refresh: tag not present (or unverifiable) on {card} after refresh")
        return REFRESH_FAILED
    _log("udev refresh: reload-rules, DRM trigger and tag check succeeded")
    return REFRESH_VERIFIED


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


def handle_status(connection, sender, params, invocation) -> None:
    locale = params[0]
    tr = _translator(locale)
    rule = read_text(MANAGED_RULE)
    if rule:
        message = tr("Managed rule present.")
    else:
        message = tr("No managed rule (auto selection).")
    invocation.return_value(GLib.Variant("(ss)", (rule, message)))


def handle_set_primary(connection, sender, params, invocation) -> None:
    pci, locale = params
    tr = _translator(locale)
    _log(f"SetPrimary: pci={pci} locale={locale} sender={sender}")
    status, detail = _authorized(invocation, connection, locale)
    if status != "authorized":
        if status == "denied":
            message = tr("Authorization was denied. Nothing changed.")
        elif status == "cancelled":
            message = tr("Authorization was cancelled. Nothing changed.")
        elif status == "timeout":
            message = tr("The authorization check timed out. Nothing changed.")
        else:
            message = (
                tr("Could not check authorization (%s). Nothing changed.") % detail
            )
        _log(f"SetPrimary: authorization {status}; nothing changed")
        invocation.return_value(GLib.Variant("(bssb)", (False, message, "", False)))
        return
    _log("SetPrimary: authorized")
    # Root cannot skip unreadable rules: they may set the tag.
    conflicts = rule_conflicts(on_unreadable="flag")
    if conflicts:
        _log(f"SetPrimary: conflicts found: {conflicts}")
        invocation.return_value(
            GLib.Variant(
                "(bssb)",
                (
                    False,
                    tr(
                        "Another udev rule sets the preferred GPU: %s. "
                        "Resolve it manually first."
                    )
                    % conflicts[0],
                    "",
                    False,
                ),
            )
        )
        return
    resolved = _resolve_gpu(pci)
    if resolved is None:
        _log(f"SetPrimary: invalid PCI {pci}")
        invocation.return_value(
            GLib.Variant(
                "(bssb)",
                (
                    False,
                    tr("Target PCI %s is not a valid display controller.") % pci,
                    "",
                    False,
                ),
            )
        )
        return
    _card, vendor, device = resolved
    _log(f"SetPrimary: resolved {pci} -> card={_card} vendor={vendor} device={device}")

    if os.path.islink(MANAGED_RULE):
        _log("SetPrimary: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant(
                "(bssb)",
                (
                    False,
                    tr("The managed rule is a symlink; refusing to modify it."),
                    "",
                    False,
                ),
            )
        )
        return

    content = _rule_text(pci, vendor, device)
    try:
        with open(MANAGED_RULE, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if existing == content:
        _log(f"SetPrimary: rule already set for {pci}; nothing to do")
        state = _refresh_udev(_card)
        refresh_ok = state == REFRESH_VERIFIED
        if refresh_ok:
            message = tr("The rule is already set for %s. Udev rules reloaded.") % pci
        elif state == REFRESH_UNSUPPORTED:
            message = (
                tr(
                    "The rule is already set for %s. "
                    "The udev refresh could not be verified "
                    "(udevadm unavailable); a reboot is required."
                )
                % pci
            )
        else:
            message = (
                tr(
                    "The rule is already set for %s. "
                    "The udev refresh failed; a reboot is required."
                )
                % pci
            )
        invocation.return_value(GLib.Variant("(bssb)", (True, message, "", refresh_ok)))
        return

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".gpu-switcher-", dir=os.path.dirname(MANAGED_RULE)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chown(tmp_path, 0, 0)
        os.chmod(tmp_path, 0o644)
        if _verify_rule(tmp_path) == REFRESH_FAILED:
            os.unlink(tmp_path)
            _log("SetPrimary: udevadm verify rejected the rule")
            invocation.return_value(
                GLib.Variant(
                    "(bssb)",
                    (
                        False,
                        tr("udevadm verify rejected the rule; nothing changed."),
                        "",
                        False,
                    ),
                )
            )
            return
        if os.path.exists(MANAGED_RULE):
            backup = _backup_rule()
            if backup == "":
                os.unlink(tmp_path)
                _log("SetPrimary: backup of the existing rule failed")
                invocation.return_value(
                    GLib.Variant(
                        "(bssb)",
                        (
                            False,
                            tr("Backup of the existing rule failed; nothing changed."),
                            "",
                            False,
                        ),
                    )
                )
                return
        else:
            backup = ""
        try:
            os.rename(tmp_path, MANAGED_RULE)
        except OSError:
            os.unlink(tmp_path)
            tmp_path = ""
            _log("SetPrimary: failed to move the rule into place")
            invocation.return_value(
                GLib.Variant(
                    "(bssb)",
                    (
                        False,
                        tr("Failed to write the managed rule; nothing changed."),
                        "",
                        False,
                    ),
                )
            )
            return
        tmp_path = ""
        _log(f"SetPrimary: rule written for {pci} (backup: {backup or 'none'})")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    state = _refresh_udev(_card)
    refresh_ok = state == REFRESH_VERIFIED
    if refresh_ok:
        if backup:
            message = tr(
                "Rule written for %s. A backup was saved to %s. "
                "Udev rules reloaded; the change takes effect "
                "after the next login (or after a reboot)."
            ) % (pci, backup)
        else:
            message = (
                tr(
                    "Rule written for %s. Udev rules reloaded; "
                    "the change takes effect after the next login "
                    "(or after a reboot)."
                )
                % pci
            )
    elif state == REFRESH_UNSUPPORTED:
        if backup:
            message = tr(
                "Rule written for %s. A backup was saved to %s. "
                "The udev refresh could not be verified "
                "(udevadm unavailable); a reboot is required."
            ) % (pci, backup)
        else:
            message = (
                tr(
                    "Rule written for %s. "
                    "The udev refresh could not be verified "
                    "(udevadm unavailable); a reboot is required."
                )
                % pci
            )
    else:
        if backup:
            message = tr(
                "Rule written for %s. A backup was saved to %s. "
                "The udev refresh failed; a reboot is required."
            ) % (pci, backup)
        else:
            message = (
                tr(
                    "Rule written for %s. "
                    "The udev refresh failed; a reboot is required."
                )
                % pci
            )
    invocation.return_value(GLib.Variant("(bssb)", (True, message, backup, refresh_ok)))


def handle_set_auto(connection, sender, params, invocation) -> None:
    locale = params[0]
    tr = _translator(locale)
    _log(f"SetAuto: locale={locale} sender={sender}")
    status, detail = _authorized(invocation, connection, locale)
    if status != "authorized":
        if status == "denied":
            message = tr("Authorization was denied. Nothing changed.")
        elif status == "cancelled":
            message = tr("Authorization was cancelled. Nothing changed.")
        elif status == "timeout":
            message = tr("The authorization check timed out. Nothing changed.")
        else:
            message = (
                tr("Could not check authorization (%s). Nothing changed.") % detail
            )
        _log(f"SetAuto: authorization {status}; nothing changed")
        invocation.return_value(GLib.Variant("(bsb)", (False, message, False)))
        return
    _log("SetAuto: authorized")
    if os.path.islink(MANAGED_RULE):
        _log("SetAuto: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant(
                "(bsb)",
                (
                    False,
                    tr("The managed rule is a symlink; refusing to remove it."),
                    False,
                ),
            )
        )
        return
    if not os.path.exists(MANAGED_RULE):
        _log("SetAuto: no managed rule; already auto")
        # A previous Reset may have removed the rule while its udev refresh
        # failed, so the stale tags persist; refresh udev on the
        # already-absent path too (as SetPrimary does for an unchanged rule).
        state = _refresh_udev()
        refresh_ok = state == REFRESH_VERIFIED
        if refresh_ok:
            message = tr(
                "No managed rule (auto selection already in effect). "
                "Udev rules reloaded."
            )
        elif state == REFRESH_UNSUPPORTED:
            message = tr(
                "No managed rule (auto selection already in effect). "
                "The udev refresh could not be verified "
                "(udevadm unavailable); a reboot is required."
            )
        else:
            message = tr(
                "No managed rule (auto selection already in effect). "
                "The udev refresh failed; a reboot is required."
            )
        invocation.return_value(GLib.Variant("(bsb)", (True, message, refresh_ok)))
        return
    backup = _backup_rule()
    if backup == "":
        _log("SetAuto: backup of the existing rule failed")
        invocation.return_value(
            GLib.Variant(
                "(bsb)",
                (
                    False,
                    tr("Backup of the existing rule failed; nothing removed."),
                    False,
                ),
            )
        )
        return
    try:
        os.unlink(MANAGED_RULE)
    except OSError:
        _log("SetAuto: failed to remove the managed rule")
        invocation.return_value(
            GLib.Variant(
                "(bsb)",
                (
                    False,
                    tr("Failed to remove the managed rule; nothing changed."),
                    False,
                ),
            )
        )
        return
    _log(f"SetAuto: rule removed (backup: {backup})")
    state = _refresh_udev()
    refresh_ok = state == REFRESH_VERIFIED
    if refresh_ok:
        message = (
            tr(
                "Managed rule removed. A backup was saved to %s. "
                "Udev rules reloaded; auto selection applies after "
                "the next login (or after a reboot)."
            )
            % backup
        )
    elif state == REFRESH_UNSUPPORTED:
        message = (
            tr(
                "Managed rule removed. A backup was saved to %s. "
                "The udev refresh could not be verified "
                "(udevadm unavailable); a reboot is required."
            )
            % backup
        )
    else:
        message = (
            tr(
                "Managed rule removed. A backup was saved to %s. "
                "The udev refresh failed; a reboot is required."
            )
            % backup
        )
    invocation.return_value(GLib.Variant("(bsb)", (True, message, refresh_ok)))


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
        _log("idle timeout; exiting so the next call re-activates fresh code")
        if _LOOP is not None:
            _LOOP.quit()
        return False

    _IDLE_SOURCE = GLib.timeout_add(IDLE_TIMEOUT_MS, on_idle)


def handle_method_call(
    connection, sender, object_path, interface_name, method_name, params, invocation
) -> None:
    if _method_call_allowed(sender, method_name):
        _log(f"method call: {method_name} from {sender}")
    try:
        if method_name == "Status":
            handle_status(connection, sender, params, invocation)
        elif method_name == "SetPrimary":
            handle_set_primary(connection, sender, params, invocation)
        elif method_name == "SetAuto":
            handle_set_auto(connection, sender, params, invocation)
        else:
            invocation.return_gerror(
                GLib.Error.new_literal(
                    GLib.quark_from_string("gpu-switcher-error"),
                    f"Unknown method: {method_name}",
                    1,
                )
            )
    except Exception:
        _internal_error(
            invocation,
            f"unhandled exception in {method_name}:\n{traceback.format_exc()}",
        )
    _reset_idle_timer()


_LOOP = None


def on_bus_acquired(connection, name) -> None:
    _log(f"connected to system bus (name: {name})")


def on_name_acquired(connection, name) -> None:
    _log(f"name acquired: {name}; ready to serve calls")


def on_name_lost(connection, name) -> None:
    where = (
        "on the owning connection" if connection is not None else "connection closed"
    )
    _log(f"name lost: {name} {where}; exiting")
    if _LOOP is not None:
        _LOOP.quit()


def main() -> None:
    global _LOOP
    _log(f"helper starting (uid={os.getuid()}, argv={sys.argv})")
    _LOOP = GLib.MainLoop.new(None, False)
    # Register the object on the bus connection BEFORE owning the name:
    # the broker retries a client's pending call the instant the name is
    # acquired, so the object must already exist (cold-start race).
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    connection.register_object(
        OBJECT_PATH, node.interfaces[0], handle_method_call, None, None
    )
    _log(f"object {OBJECT_PATH} registered; requesting name {SERVICE_NAME}")
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
    _LOOP.run()
    _log("main loop ended")


if __name__ == "__main__":
    main()
