"""gpu-switcher helper: root, D-Bus-activated system-bus service.

Methods:
  SetPrimary(pci, locale) -> (ok, message, backup_path, refresh_ok)
  SetAuto(locale)         -> (ok, message, refresh_ok)

Each call authorizes through polkit (auth.py), changes the managed rule, then
refreshes udev under the same authorization. A failed refresh sets
refresh_ok to False and leaves reboot as the fallback. SetPrimary also checks
that the chosen card's current udev tags carry the mutter tag.

Concurrency: one mutating call runs at a time; a concurrent one gets the Busy
D-Bus error. All long work is asynchronous, so the main loop stays
responsive. Expected failures are HelperFailure and reply with the failure
tuple; anything else becomes the Internal D-Bus error, with the detail logged
to stderr (the journal).

GDBus: method calls are served by the object registered with
connection.register_object(); its handler is called with (connection, sender,
path, interface, method, params, invocation). The object is registered before
g_bus_own_name() because the broker retries a client's pending call the
instant the name is acquired.
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
    OnUnreadable,
    rule_conflicts,
)
from gpu_switcher.helper import auth, rule_store
from gpu_switcher.helper import udev as hud
from gpu_switcher.helper.auth import AuthStatus
from gpu_switcher.helper.errors import HelperFailure

SERVICE_NAME = "com.github.wl04.gpu_switcher.Helper"
OBJECT_PATH = "/com/github/wl04/gpu_switcher/Helper"
INTERFACE = "com.github.wl04.gpu_switcher.Helper"

LOCALE_BASE = "/usr/share/gpu-switcher/locale"

INTROSPECTION_XML = f"""<node name="{OBJECT_PATH}">
  <interface name="{INTERFACE}">
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


# The locale arrives from an unauthenticated caller. Only shipped catalog
# languages are accepted; anything else (unknown, path-shaped, over-long)
# becomes English, so catalog lookups never leave LOCALE_BASE.
def _shipped_locales(localedir: str) -> tuple[str, ...]:
    """The languages shipped under localedir, plus the English fallback. A
    missing or unreadable directory yields just "en"."""
    try:
        entries = os.listdir(localedir)
    except OSError:
        entries = []
    locales = sorted(e for e in entries if os.path.isdir(os.path.join(localedir, e)))
    return tuple(dict.fromkeys(["en", *locales]))


# Read from the installed locale tree so it matches the packaged catalogs.
SUPPORTED_LOCALES = _shipped_locales(LOCALE_BASE)
_MAX_LOCALE_LEN = 32


def normalize_locale(locale):
    """Map a caller-supplied locale to a supported catalog language.

    Returns the language part ('ru_RU.UTF-8' -> 'ru') when supported, else
    'en'. Path-shaped values and values over _MAX_LOCALE_LEN are rejected:
    gettext would use them as path components and could open files outside
    LOCALE_BASE.
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

    The locale goes through normalize_locale first.
    """
    language = normalize_locale(locale)
    langs = [language] if language == "en" else [language, "en"]
    try:
        return gettext.translation(
            "gpu-switcher", LOCALE_BASE, languages=langs, fallback=True
        ).gettext
    except OSError:
        return lambda s: s


# Operation lock: one mutating call at a time; a concurrent one gets Busy.
_operation_in_progress = False
# The invocation holding the lock: a failure releases the lock only if it is
# the failing call's.
_active_invocation = None


def _claim_operation(invocation=None) -> bool:
    """Claim the operation lock; False when another operation is in flight."""
    global _operation_in_progress, _active_invocation
    if _operation_in_progress:
        return False
    _operation_in_progress = True
    _active_invocation = invocation
    return True


def _release_operation() -> None:
    global _operation_in_progress, _active_invocation
    _operation_in_progress = False
    _active_invocation = None


def _internal_error(invocation, detail):
    """Reply with the internal-error D-Bus error and log the detail."""
    print("error: " + detail, file=sys.stderr)
    invocation.return_dbus_error(
        DBUS_ERROR_INTERNAL, "Internal error; see the journal."
    )


def _fail_operation(invocation, detail):
    """Unexpected failure: release the lock when this call holds it, then
    reply with the internal error."""
    if _operation_in_progress and _active_invocation is invocation:
        _release_operation()
    _internal_error(invocation, detail)


def _abort_active_operation(detail):
    """Failure inside an asynchronous callback of the in-flight operation."""
    invocation = _active_invocation
    if invocation is None:
        print("error: " + detail, file=sys.stderr)
        _release_operation()
        return
    _fail_operation(invocation, detail)
    _reset_idle_timer()


hud.callback_error_handler = _abort_active_operation


def _guarded(invocation, fn):
    """Wrap an asynchronous callback so an unexpected exception releases the
    lock and answers the call instead of wedging the helper."""

    def wrapper(*args):
        try:
            fn(*args)
        except Exception:
            _fail_operation(
                invocation,
                f"unhandled exception in callback:\n{traceback.format_exc()}",
            )
            _reset_idle_timer()

    return wrapper


def authorization_message(tr, status, detail):
    """Localized reply for a call that was not authorized."""
    if status is AuthStatus.DENIED:
        return tr("Authorization was denied. Nothing changed.")
    if status is AuthStatus.CANCELLED:
        return tr("Authorization was cancelled. Nothing changed.")
    if status is AuthStatus.TIMEOUT:
        return tr("The authorization check timed out. Nothing changed.")
    return tr("Could not check authorization (%s). Nothing changed.") % detail


def _set_primary_plan(pci, tr):
    """Pre-authorization checks for SetPrimary; raises HelperFailure.

    Root cannot skip unreadable rules: they may set the tag. Returns
    (card, vendor, device) when the target is valid.
    """
    conflicts = rule_conflicts(on_unreadable=OnUnreadable.FLAG)
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
        print(f"SetPrimary: invalid PCI {pci!r}", file=sys.stderr)
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
    """SetPrimary(pci, locale). Expected failures raise HelperFailure (caught
    by handle_method_call); otherwise the async chain replies through
    finish()."""
    pci, locale = params
    tr = translator(locale)
    print(f"SetPrimary: pci={pci!r} locale={locale!r} sender={sender}", file=sys.stderr)
    if not _claim_operation(invocation):
        print("SetPrimary: operation already in progress; rejecting", file=sys.stderr)
        invocation.return_dbus_error(
            DBUS_ERROR_BUSY, "Another operation is in progress."
        )
        return
    card, vendor, device = _set_primary_plan(pci, tr)
    # GDBusMethodInvocation has no get_cancellable() in pygobject; use our own.
    cancellable = Gio.Cancellable.new()

    def finish(ok, message, backup, refresh_ok):
        _release_operation()
        _reset_idle_timer()
        invocation.return_value(
            GLib.Variant("(bssb)", (ok, message, backup, refresh_ok))
        )

    def on_auth(status, detail):
        if status is not AuthStatus.AUTHORIZED:
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
                    state == hud.RefreshState.VERIFIED,
                )

            hud.refresh_udev_async(card, on_refresh_idempotent, cancellable)
            return
        rule_store.write_rule_atomically(pci, content, card, tr, finish, cancellable)

    sender_bus = invocation.get_message().get_sender()
    auth.authorize(
        connection, sender_bus, locale, _guarded(invocation, on_auth), cancellable
    )


def _set_auto_apply(tr):
    """Remove the managed rule after authorization; raises HelperFailure.
    Returns (backup, leading, verified_sentence) for the reply message."""
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
        rule_store.retain_backups()
        try:
            rule_store.unlink_managed_rule()
        except OSError as error:
            print("SetAuto: failed to remove the managed rule", file=sys.stderr)
            raise HelperFailure(
                tr("Failed to remove the managed rule; nothing changed.")
            ) from error
        print(f"SetAuto: rule removed (backup: {backup})", file=sys.stderr)
        leading = tr("Managed rule removed.")
        verified_sentence = tr(
            "Udev rules reloaded; auto selection applies after the next "
            "login (or after a reboot)."
        )
    else:
        print("SetAuto: no managed rule; already auto", file=sys.stderr)
        # An earlier removal may have left stale tags (its refresh failed), so
        # refresh even when the rule is already absent, as SetPrimary does
        # for an unchanged rule.
        leading = tr("No managed rule (auto selection already in effect).")
        verified_sentence = tr("Udev rules reloaded.")
    return backup, leading, verified_sentence


def handle_set_auto(connection, sender, params, invocation) -> None:
    """SetAuto(locale). Authorization and file failures reply with the
    failure tuple; otherwise the async udev refresh replies through
    finish()."""
    locale = params[0]
    tr = translator(locale)
    print(f"SetAuto: locale={locale!r} sender={sender}", file=sys.stderr)
    if not _claim_operation(invocation):
        print("SetAuto: operation already in progress; rejecting", file=sys.stderr)
        invocation.return_dbus_error(
            DBUS_ERROR_BUSY, "Another operation is in progress."
        )
        return
    # GDBusMethodInvocation has no get_cancellable() in pygobject; use our own.
    cancellable = Gio.Cancellable.new()

    def finish(ok, message, refresh_ok):
        _release_operation()
        _reset_idle_timer()
        invocation.return_value(GLib.Variant("(bsb)", (ok, message, refresh_ok)))

    def on_auth(status, detail):
        if status is not AuthStatus.AUTHORIZED:
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
            finish(True, message, state == hud.RefreshState.VERIFIED)

        hud.refresh_udev_async(None, on_refresh, cancellable)

    sender_bus = invocation.get_message().get_sender()
    auth.authorize(
        connection, sender_bus, locale, _guarded(invocation, on_auth), cancellable
    )


def handle_method_call(
    connection, sender, object_path, interface_name, method_name, params, invocation
) -> None:
    print(f"method call: {method_name} from {sender}", file=sys.stderr)
    try:
        if method_name == "SetPrimary":
            handle_set_primary(connection, sender, params, invocation)
        elif method_name == "SetAuto":
            handle_set_auto(connection, sender, params, invocation)
        # GDBus dispatches only the methods declared in the introspection
        # node, so there is no "unknown method" case here.
    except HelperFailure as failure:
        # Expected failure: reply with the failure tuple. The async chain has
        # not started, so finish() will not release the lock; do it here.
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
        _fail_operation(
            invocation,
            f"unhandled exception in {method_name}:\n{traceback.format_exc()}",
        )
    _reset_idle_timer()


# Quit shortly after the last call so a package upgrade is never served by a
# stale process; the broker re-activates the helper from the on-disk code.
IDLE_TIMEOUT_MS = 60000
_IDLE_SOURCE = None


def _on_idle_timeout():
    """Idle timer tick; True keeps the timer (an operation is in flight: the
    polkit dialog alone may outlast the idle timeout), False ends it."""
    if _operation_in_progress:
        return True
    print(
        "idle timeout; exiting so the next call re-activates fresh code",
        file=sys.stderr,
    )
    if _LOOP is not None:
        _LOOP.quit()
    return False


def _reset_idle_timer():
    global _IDLE_SOURCE
    if _IDLE_SOURCE is not None:
        GLib.source_remove(_IDLE_SOURCE)

    _IDLE_SOURCE = GLib.timeout_add(IDLE_TIMEOUT_MS, _on_idle_timeout)


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
    hud.check_udevadm()
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
    # Arm the timer now: calls the bus answers itself (introspection) never
    # reach a handler, and the helper must still exit when idle.
    _reset_idle_timer()
    _LOOP.run()
    print("main loop ended", file=sys.stderr)
