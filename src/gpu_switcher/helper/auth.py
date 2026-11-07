"""polkit authorization for the helper: CheckAuthorization + cancel.

The subject is the caller's unique bus name (polkitd maps it to the
session and user); the check runs asynchronously with the shared
POLKIT_TIMEOUT_MS budget, and a timed-out check is cancelled so a stale
dialog cannot linger.
"""

import sys
import uuid

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from gpu_switcher.core.udev import G_IO_ERROR_TIMEOUT, POLKIT_TIMEOUT_MS

POLKIT_ACTION = "com.github.wl04.gpu_switcher.set-primary"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_INTERACTIVE = 1  # CheckAuthorizationFlags: AllowUserInteraction


def authorize(connection, sender, locale, on_done, cancellable=None):
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
                cancel_check_authorization(connection, cancellation_id)
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


def cancel_check_authorization(connection, cancellation_id):
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
