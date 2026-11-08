"""polkit authorization for the helper: CheckAuthorization + cancel.

The subject is the caller's unique bus name (polkitd maps it to the
session and user); the check runs asynchronously with the shared
POLKIT_TIMEOUT_MS budget, and a timed-out check is cancelled so a stale
dialog cannot linger.
"""

import sys
import uuid
from enum import Enum

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from gpu_switcher.core.udev import POLKIT_TIMEOUT_MS


class AuthStatus(Enum):
    """Outcome of the polkit CheckAuthorization step."""

    AUTHORIZED = "authorized"
    DENIED = "denied"
    CANCELLED = "cancelled"  # dialog dismissed
    TIMEOUT = "timeout"  # our budget expired; the check is cancelled
    ERROR = "error"  # the check itself failed


POLKIT_ACTION = "com.github.wl04.gpu_switcher.set-primary"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_INTERACTIVE = 1  # CheckAuthorizationFlags: AllowUserInteraction


def authorize(connection, sender, locale, on_done, cancellable=None):
    """Ask polkit whether the caller may perform the action (interactive).

    The subject is the caller's unique bus name, which stays fixed for the
    life of the connection, so the identity checked is the one that made the
    call. The 5th CheckAuthorization argument is the cancellation id, not a
    locale: the dialog text comes from the policy file and the agent's own
    locale (`locale` is therefore not used here).

    Asynchronous: on_done(status, detail) runs from the reply callback with an
    AuthStatus; detail describes a failed check and is empty otherwise. The
    call uses POLKIT_TIMEOUT_MS, which is shorter than the client's budget,
    so this timeout always resolves first.
    """
    cancellation_id = uuid.uuid4().hex
    subject = GLib.Variant.new_tuple(
        GLib.Variant("s", "system-bus-name"),
        GLib.Variant("a{sv}", {"name": GLib.Variant("s", sender)}),
    )
    # pygobject cannot build a tuple containing an a{sv} from a literal, so
    # the tuples are assembled with GLib.Variant.new_tuple.
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
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
                # G_IO_ERROR_TIMEOUT: the check is still pending in polkitd,
                # cancel it so a stale dialog cannot linger.
                print(f"polkit CheckAuthorization timed out: {exc}", file=sys.stderr)
                if cancellable is not None:
                    cancellable.cancel()
                cancel_check_authorization(connection, cancellation_id)
                on_done(AuthStatus.TIMEOUT, "")
                return
            print(f"polkit CheckAuthorization failed: {message}", file=sys.stderr)
            on_done(AuthStatus.ERROR, message)
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
                on_done(AuthStatus.CANCELLED, "")
                return
            on_done(AuthStatus.DENIED, "")
            return
        on_done(AuthStatus.AUTHORIZED, "")

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
        # No user_data: pygobject would pass it to on_result as a 3rd argument.
        on_result,
    )


def cancel_check_authorization(connection, cancellation_id):
    """Best-effort asynchronous CancelCheckAuthorization after a timeout.

    The result is logged and ignored: the check may already be finished
    (denied/cancelled between our timeout and the call), and the reply
    carries no data (in-arg s only). Asynchronous so the helper's main loop
    is never blocked on polkitd.
    """

    def on_cancelled(conn, task):
        try:
            conn.call_finish(task)
            print(f"CancelCheckAuthorization: {cancellation_id}", file=sys.stderr)
        except GLib.Error as exc:
            print(f"CancelCheckAuthorization failed: {exc}", file=sys.stderr)

    connection.call(
        "org.freedesktop.PolicyKit1",
        POLKIT_PATH,
        POLKIT_IFACE,
        "CancelCheckAuthorization",
        GLib.Variant("(s)", (cancellation_id,)),
        GLib.VariantType("()"),
        0,
        5000,
        None,
        on_cancelled,
    )
