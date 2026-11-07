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
the whole operation. Expected, user-facing failures are HelperFailure:
the dispatcher catches them once and replies with the operation-failure
tuple. Unhandled exceptions are reported as the
com.github.wl04.gpu_switcher.Error.Internal D-Bus error; the detail is
logged to the journal.

Authorization: polkit CheckAuthorization over raw D-Bus with the
INTERACTIVE flag, so the standard agent dialog authenticates the caller
(see auth.py).

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
"""

import gettext
import os
import sys
import traceback

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.gpu import resolve_gpu, rule_text
from gpu_switcher.core.udev import (
    DBUS_ERROR_BUSY,
    DBUS_ERROR_INTERNAL,
    read_text,
    rule_conflicts,
)
from gpu_switcher.helper import auth, rule_store
from gpu_switcher.helper import udev as hud
from gpu_switcher.helper.errors import HelperFailure

SERVICE_NAME = "com.github.wl04.gpu_switcher.Helper"
OBJECT_PATH = "/com/github/wl04/gpu_switcher/Helper"
INTERFACE = "com.github.wl04.gpu_switcher.Helper"

LOCALE_BASE = "/usr/share/gpu-switcher/locale"

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


# The locale arguments arrive from an unauthenticated D-Bus caller. Only
# the catalog languages shipped with the helper are accepted; anything else
# (unknown language, path-shaped value, over-long value) falls back to
# English, so catalog lookups can never leave LOCALE_BASE.
SUPPORTED_LOCALES = ("en", "ru")
_MAX_LOCALE_LEN = 32


def normalize_locale(locale):
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


def translator(locale):
    """gettext for this helper invocation (locale supplied by the caller).

    The locale is normalized to the allowlisted languages first (see
    normalize_locale), so catalog loading stays inside LOCALE_BASE.
    """
    language = normalize_locale(locale)
    langs = [language] if language == "en" else [language, "en"]
    try:
        return gettext.translation(
            "gpu-switcher", LOCALE_BASE, languages=langs, fallback=True
        ).gettext
    except OSError:
        return lambda s: s


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


def authorization_message(tr, status, detail):
    """Localized reply for a call that was not authorized."""
    if status == "denied":
        return tr("Authorization was denied. Nothing changed.")
    if status == "cancelled":
        return tr("Authorization was cancelled. Nothing changed.")
    if status == "timeout":
        return tr("The authorization check timed out. Nothing changed.")
    return tr("Could not check authorization (%s). Nothing changed.") % detail


def handle_status(connection, sender, params, invocation) -> None:
    locale = params[0]
    tr = translator(locale)
    rule = read_text(rule_store.MANAGED_RULE)
    if rule:
        message = tr("Managed rule present.")
    else:
        message = tr("No managed rule (auto selection).")
    invocation.return_value(GLib.Variant("(ss)", (rule, message)))


def _set_primary_plan(pci, tr):
    """Pre-authorization checks for SetPrimary; raises HelperFailure.

    Root cannot skip unreadable rules: they may set the tag. Returns
    (card, vendor, device) when the target is valid.
    """
    conflicts = rule_conflicts(on_unreadable="flag")
    if conflicts:
        print(f"SetPrimary: conflicts found: {conflicts}", file=sys.stderr)
        raise HelperFailure(
            tr(
                "Another udev rule sets the preferred GPU: %s. Resolve it manually first."
            )
            % conflicts[0]
        )
    resolved = resolve_gpu(pci)
    if resolved is None:
        print(f"SetPrimary: invalid PCI {pci}", file=sys.stderr)
        raise HelperFailure(
            tr("Target PCI %s is not a valid display controller.") % pci
        )
    card, vendor, device = resolved
    print(
        f"SetPrimary: resolved {pci} -> card={card} vendor={vendor} device={device}",
        file=sys.stderr,
    )
    rule_store.check_not_symlink(
        tr("The managed rule is a symlink; refusing to modify it.")
    )
    return card, vendor, device


def handle_set_primary(connection, sender, params, invocation) -> None:
    """SetPrimary(pci, locale). Expected failures raise HelperFailure
    (caught by handle_method_call); the async chain completes via
    invocation.return_value."""
    pci, locale = params
    tr = translator(locale)
    print(f"SetPrimary: pci={pci} locale={locale} sender={sender}", file=sys.stderr)
    if not _claim_operation():
        print("SetPrimary: operation already in progress; rejecting", file=sys.stderr)
        invocation.return_dbus_error(
            DBUS_ERROR_BUSY, "Another operation is in progress."
        )
        return
    card, vendor, device = _set_primary_plan(pci, tr)
    cancellable = invocation.get_cancellable()

    def finish(ok, message, backup, refresh_ok):
        _release_operation()
        _reset_idle_timer()
        invocation.return_value(
            GLib.Variant("(bssb)", (ok, message, backup, refresh_ok))
        )

    def on_auth(status, detail):
        if status != "authorized":
            print(
                f"SetPrimary: authorization {status}; nothing changed", file=sys.stderr
            )
            finish(False, authorization_message(tr, status, detail), "", False)
            return
        print("SetPrimary: authorized", file=sys.stderr)
        content = rule_text(pci, vendor, device)
        # Read as bytes: the existing rule may hold invalid UTF-8 (a broken
        # hand edit); comparing bytes keeps the overwrite path working.
        try:
            with open(rule_store.MANAGED_RULE, "rb") as f:
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
                    rule_store.refresh_message(
                        tr,
                        state,
                        leading=tr("The rule is already set for %s.") % pci,
                        verified_sentence=tr("Udev rules reloaded."),
                    ),
                    "",
                    state == hud.REFRESH_VERIFIED,
                )

            hud.refresh_udev_async(card, on_refresh_idempotent, cancellable)
            return
        rule_store.write_rule_atomically(pci, content, card, tr, finish, cancellable)

    sender_bus = invocation.get_message().get_sender()
    auth.authorize(connection, sender_bus, locale, on_auth, cancellable)


def _set_auto_apply(tr):
    """Synchronous part of SetAuto after authorization; raises
    HelperFailure. Returns (backup, leading, verified_sentence)."""
    rule_store.check_not_symlink(
        tr("The managed rule is a symlink; refusing to remove it.")
    )
    backup = ""
    if os.path.exists(rule_store.MANAGED_RULE):
        backup = rule_store.backup_rule()
        if backup == "":
            raise HelperFailure(
                tr("Backup of the existing rule failed; nothing removed.")
            )
        try:
            rule_store.unlink_managed_rule()
        except OSError:
            print("SetAuto: failed to remove the managed rule", file=sys.stderr)
            raise HelperFailure(
                tr("Failed to remove the managed rule; nothing changed.")
            )
        print(f"SetAuto: rule removed (backup: {backup})", file=sys.stderr)
        leading = tr("Managed rule removed.")
        verified_sentence = tr(
            "Udev rules reloaded; auto selection applies after the next "
            "login (or after a reboot)."
        )
    else:
        print("SetAuto: no managed rule; already auto", file=sys.stderr)
        # A previous Reset may have removed the rule while its udev
        # refresh failed, so the stale tags persist; refresh udev on the
        # already-absent path too (as SetPrimary does for an unchanged
        # rule).
        leading = tr("No managed rule (auto selection already in effect).")
        verified_sentence = tr("Udev rules reloaded.")
    return backup, leading, verified_sentence


def handle_set_auto(connection, sender, params, invocation) -> None:
    """SetAuto(locale). Authorization failure and post-authorization file
    failures complete with the operation-failure tuple; the async udev
    refresh completes via invocation.return_value."""
    locale = params[0]
    tr = translator(locale)
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

    def on_auth(status, detail):
        if status != "authorized":
            print(f"SetAuto: authorization {status}; nothing changed", file=sys.stderr)
            finish(False, authorization_message(tr, status, detail), False)
            return
        print("SetAuto: authorized", file=sys.stderr)
        try:
            backup, leading, verified_sentence = _set_auto_apply(tr)
        except HelperFailure as failure:
            finish(False, failure.args[0], False)
            return

        def on_refresh(state):
            message = rule_store.refresh_message(
                tr,
                state,
                leading=leading,
                backup=backup,
                verified_sentence=verified_sentence,
            )
            finish(True, message, state == hud.REFRESH_VERIFIED)

        hud.refresh_udev_async(None, on_refresh, cancellable)

    sender_bus = invocation.get_message().get_sender()
    auth.authorize(connection, sender_bus, locale, on_auth, cancellable)


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
    except HelperFailure as failure:
        # An expected, user-facing failure: reply with the operation
        # failure tuple for the method (the lock is released here - the
        # async chain has not started, so finish() will not run).
        _release_operation()
        if method_name == "SetPrimary":
            invocation.return_value(
                GLib.Variant("(bssb)", (False, failure.args[0], "", False))
            )
        else:  # SetAuto
            invocation.return_value(
                GLib.Variant("(bsb)", (False, failure.args[0], False))
            )
    except Exception:
        _internal_error(
            invocation,
            f"unhandled exception in {method_name}:\n{traceback.format_exc()}",
        )
    _reset_idle_timer()


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
    hud.check_udevadm()  # single availability check, cached for the process
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
