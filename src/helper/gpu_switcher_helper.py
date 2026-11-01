#!/usr/bin/env python3
"""gpu-switcher helper: D-Bus-activated system-bus service (runs as root).

dbus-broker starts this process on demand when an unprivileged client (the
GUI app) calls one of its methods.

Methods:
  Status(locale)            -> (rule_on_disk, message)
  SetPrimary(pci, locale)   -> (ok, message, backup_path, refresh_ok)
  SetAuto(locale)           -> (ok, message, refresh_ok)

SetPrimary and SetAuto also refresh udev (udevadm control --reload-rules
plus a scoped udevadm trigger for the DRM subsystem) under the SAME
authorization, so activating the rule needs no second prompt; a failed
refresh is reported per call (refresh_ok is false) and leaves reboot as
the fallback.

Authorization: polkit CheckAuthorization over raw D-Bus with the INTERACTIVE
flag, so the standard agent dialog authenticates the caller. The dialog
message is localized from the locale argument, because a root-activated
process has no user locale of its own.

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

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

# The shared udev rule module (src/udev_rules.py in the source tree,
# /usr/lib/gpu-switcher/udev_rules.py installed) lives next to this
# package; add the parent directory to sys.path so it can be imported
# without pulling in the GUI package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from udev_rules import MANAGED_RULE, MUTTER_ENV, MUTTER_TAG, read_text, rule_conflicts

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


def _log(message):
    """Append one diagnostic line to stderr and /var/log/gpu-switcher-helper.log.

    Best-effort by design: logging must never crash the helper itself.
    """
    line = "gpu-switcher-helper %s %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _internal_error(invocation, detail):
    """Reply with a D-Bus error the caller can display, and log the detail."""
    _log("error: " + detail)
    invocation.return_gerror(GLib.Error.new_literal(
        GLib.quark_from_string("gpu-switcher-error"),
        "Internal error; see /var/log/gpu-switcher-helper.log", 1))


def _translator(locale):
    """gettext for this helper invocation (locale supplied by the caller)."""
    langs = []
    for part in (locale, locale.split(".")[0], locale.split("_")[0]):
        if part and part not in langs:
            langs.append(part)
    langs.append("en")
    try:
        return gettext.translation("gpu-switcher", LOCALE_BASE,
                                  languages=langs, fallback=True).gettext
    except OSError:
        return lambda s: s


def _sender_pid_uid(invocation, connection):
    """(pid, uid) of the D-Bus caller, via org.freedesktop.DBus."""
    unique = invocation.get_message().get_sender()
    variant = GLib.Variant("(s)", (unique,))

    # Both methods return unsigned int per the D-Bus spec; a mismatched
    # reply type raises GLib.Error, which must not look like a denial.
    def dbus_uint(method):
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            method, variant, GLib.VariantType("(u)"), 0, 5000, None)
        return result[0]

    return dbus_uint("GetConnectionUnixProcessID"), dbus_uint("GetConnectionUnixUser")


def _authorize(connection, pid, uid, locale):
    """polkit CheckAuthorization with the AllowUserInteraction flag.

    polkitd (current "js" backend, backend_version 127) uses the new
    CheckAuthorization signature: in (sa{sv}) subject struct
    (subject_kind, subject_details), s action_id, a{ss} options,
    u flags, s locale; out is one struct (bba{ss})
    (authorized, whether_challenge, details), so the reply body type is
    ((bba{ss})). The local-process subject is
    ("unix-process", {"pid": u, "start-time": t}); polkitd maps the pid to
    the caller's session and user. The locale selects the localized policy
    message shown in the agent dialog (the text lives in the .policy file);
    the caller supplies it because a root-activated process has no user
    locale of its own.
    """
    subject = GLib.Variant.new_tuple(
        GLib.Variant("s", "unix-process"),
        GLib.Variant("a{sv}", {
            "pid": GLib.Variant("u", pid),
            "start-time": GLib.Variant("t", 0),
        }),
    )
    # pygobject's GLib.Variant cannot nest an a{sv} inside a tuple literal
    # (the creator iterates it as an array); build the tuple with
    # GLib.Variant.new_tuple instead.
    params = GLib.Variant.new_tuple(
        subject,
        GLib.Variant("s", POLKIT_ACTION),
        GLib.Variant("a{ss}", []),
        GLib.Variant("u", POLKIT_INTERACTIVE),
        GLib.Variant("s", locale),
    )
    _log(f"polkit CheckAuthorization: pid={pid} uid={uid} action={POLKIT_ACTION}")
    try:
        result = connection.call_sync(
            "org.freedesktop.PolicyKit1", POLKIT_PATH, POLKIT_IFACE,
            "CheckAuthorization", params, GLib.VariantType("((bba{ss}))"),
            0, 170000, None)
    except GLib.Error as exc:
        _log(f"polkit CheckAuthorization failed: {exc}")
        return False
    # Reply body is one struct (b b a{ss}):
    # (authorized, whether_challenge, details).
    authorized, whether_challenge, details = result[0]
    _log(f"polkit result: authorized={authorized} "
         f"whether_challenge={whether_challenge} details={details!r}")
    return bool(authorized)


def _authorized(invocation, connection, locale):
    """Full polkit check for the caller of this invocation."""
    try:
        pid, uid = _sender_pid_uid(invocation, connection)
    except Exception as exc:
        _log(f"could not determine caller pid/uid: {exc}")
        return False
    return _authorize(connection, pid, uid, locale)


def _resolve_gpu(pci):
    """(card, vendor, device) for a valid display controller, else None.

    Mirrors the reference script: class 0x030000, a bound driver, and exactly
    one /dev/dri/card* device bound to this PCI device.
    """
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9]", pci):
        return None
    pdir = f"/sys/bus/pci/devices/{pci}"
    if not os.path.isdir(pdir):
        return None
    vendor = read_text(f"{pdir}/vendor")
    device = read_text(f"{pdir}/device")
    if read_text(f"{pdir}/class") != "0x030000":
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
    """udevadm verify when available; skipped when udevadm cannot run."""
    if shutil.which("udevadm") is None:
        return True
    try:
        out = subprocess.run(["udevadm", "verify", path],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    return out.returncode == 0


def _reload_rules():
    try:
        out = subprocess.run(["udevadm", "control", "--reload-rules"],
                             capture_output=True, text=True, timeout=30)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _trigger_drm():
    """Re-emit DRM kernel events so udev re-runs the rules and refreshes
    the persistent device database with the new tags.

    Scoped to the DRM subsystem: only display devices are touched, nothing
    else is re-probed. Returns True when the trigger ran cleanly.
    """
    try:
        out = subprocess.run(["udevadm", "trigger", "--subsystem-match=drm"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    _log(f"udevadm trigger --subsystem-match=drm rc={out.returncode} "
         f"output={out.stdout.strip() or out.stderr.strip() or '-'}")
    return out.returncode == 0


def _refresh_udev():
    """Reload the udev rules and re-trigger the DRM devices so the new
    rule's tag lands in the persistent device database without a reboot.

    Scoped to the DRM subsystem: only display devices are touched, nothing
    else is re-probed. Returns True only when both steps succeeded.
    """
    if not _reload_rules():
        _log("udev refresh: udevadm control --reload-rules failed")
        return False
    if not _trigger_drm():
        _log("udev refresh: udevadm trigger --subsystem-match=drm failed")
        return False
    _log("udev refresh: reload-rules and DRM trigger succeeded")
    return True


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


def handle_status(connection, sender, params, invocation):
    locale = params[0]
    tr = _translator(locale)
    rule = read_text(MANAGED_RULE)
    if rule:
        message = tr("Managed rule present.")
    else:
        message = tr("No managed rule (auto selection).")
    invocation.return_value(GLib.Variant("(ss)", (rule, message)))


def handle_set_primary(connection, sender, params, invocation):
    pci, locale = params
    tr = _translator(locale)
    _log(f"SetPrimary: pci={pci} locale={locale} sender={sender}")
    if not _authorized(invocation, connection, locale):
        _log("SetPrimary: authorization denied or failed; nothing changed")
        invocation.return_value(
            GLib.Variant("(bssb)",
                         (False, tr("Authorization was denied. Nothing changed."), "", False)))
        return
    _log("SetPrimary: authorized")
    # Root cannot skip unreadable rules: they may set the tag.
    conflicts = rule_conflicts(on_unreadable="flag")
    if conflicts:
        _log(f"SetPrimary: conflicts found: {conflicts}")
        invocation.return_value(
            GLib.Variant("(bssb)",
                         (False,
                          tr("Another udev rule sets the preferred GPU: %s. "
                             "Resolve it manually first.") % conflicts[0],
                          "", False)))
        return
    resolved = _resolve_gpu(pci)
    if resolved is None:
        _log(f"SetPrimary: invalid PCI {pci}")
        invocation.return_value(
            GLib.Variant("(bssb)",
                         (False,
                          tr("Target PCI %s is not a valid display controller.") % pci,
                          "", False)))
        return
    _card, vendor, device = resolved
    _log(f"SetPrimary: resolved {pci} -> card={_card} vendor={vendor} device={device}")

    if os.path.islink(MANAGED_RULE):
        _log("SetPrimary: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant("(bssb)",
                         (False,
                          tr("The managed rule is a symlink; refusing to modify it."),
                          "", False)))
        return

    content = _rule_text(pci, vendor, device)
    try:
        with open(MANAGED_RULE, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if existing == content:
        _log(f"SetPrimary: rule already set for {pci}; nothing to do")
        refresh_ok = _refresh_udev()
        if refresh_ok:
            message = tr("The rule is already set for %s. Udev rules reloaded.") % pci
        else:
            message = tr("The rule is already set for %s. "
                         "The udev refresh failed; a reboot is required.") % pci
        invocation.return_value(
            GLib.Variant("(bssb)", (True, message, "", refresh_ok)))
        return

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".gpu-switcher-", dir=os.path.dirname(MANAGED_RULE))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chown(tmp_path, 0, 0)
        os.chmod(tmp_path, 0o644)
        if not _verify_rule(tmp_path):
            os.unlink(tmp_path)
            _log("SetPrimary: udevadm verify rejected the rule")
            invocation.return_value(
                GLib.Variant("(bssb)",
                             (False,
                              tr("udevadm verify rejected the rule; nothing changed."),
                              "", False)))
            return
        if os.path.exists(MANAGED_RULE):
            backup = _backup_rule()
            if backup == "":
                os.unlink(tmp_path)
                _log("SetPrimary: backup of the existing rule failed")
                invocation.return_value(
                    GLib.Variant("(bssb)",
                                 (False,
                                  tr("Backup of the existing rule failed; nothing changed."),
                                  "", False)))
                return
        else:
            backup = ""
        os.rename(tmp_path, MANAGED_RULE)
        tmp_path = ""
        _log(f"SetPrimary: rule written for {pci} (backup: {backup or 'none'})")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    refresh_ok = _refresh_udev()
    if refresh_ok:
        if backup:
            message = tr("Rule written for %s. A backup was saved to %s. "
                         "Udev rules reloaded; the change takes effect "
                         "after the next login (or after a reboot).") % (pci, backup)
        else:
            message = tr("Rule written for %s. Udev rules reloaded; "
                         "the change takes effect after the next login "
                         "(or after a reboot).") % pci
    else:
        if backup:
            message = tr("Rule written for %s. A backup was saved to %s. "
                         "The udev refresh failed; a reboot is required.") % (pci, backup)
        else:
            message = tr("Rule written for %s. "
                         "The udev refresh failed; a reboot is required.") % pci
    invocation.return_value(
        GLib.Variant("(bssb)", (True, message, backup, refresh_ok)))


def handle_set_auto(connection, sender, params, invocation):
    locale = params[0]
    tr = _translator(locale)
    _log(f"SetAuto: locale={locale} sender={sender}")
    if not _authorized(invocation, connection, locale):
        _log("SetAuto: authorization denied or failed; nothing changed")
        invocation.return_value(
            GLib.Variant("(bsb)",
                         (False, tr("Authorization was denied. Nothing changed."), False)))
        return
    _log("SetAuto: authorized")
    if os.path.islink(MANAGED_RULE):
        _log("SetAuto: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant("(bsb)",
                         (False,
                          tr("The managed rule is a symlink; refusing to remove it."), False)))
        return
    if not os.path.exists(MANAGED_RULE):
        _log("SetAuto: no managed rule; already auto")
        invocation.return_value(
            GLib.Variant("(bsb)",
                         (True,
                          tr("No managed rule (auto selection already in effect)."), False)))
        return
    backup = _backup_rule()
    if backup == "":
        _log("SetAuto: backup of the existing rule failed")
        invocation.return_value(
            GLib.Variant("(bsb)",
                         (False,
                          tr("Backup of the existing rule failed; nothing removed."), False)))
        return
    try:
        os.unlink(MANAGED_RULE)
    except OSError:
        _log("SetAuto: failed to remove the managed rule")
        invocation.return_value(
            GLib.Variant("(bsb)",
                         (False,
                          tr("Failed to remove the managed rule; nothing changed."), False)))
        return
    _log(f"SetAuto: rule removed (backup: {backup})")
    refresh_ok = _refresh_udev()
    if refresh_ok:
        message = tr("Managed rule removed. A backup was saved to %s. "
                     "Udev rules reloaded; auto selection applies after "
                     "the next login (or after a reboot).") % backup
    else:
        message = tr("Managed rule removed. A backup was saved to %s. "
                     "The udev refresh failed; a reboot is required.") % backup
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


def handle_method_call(connection, sender, object_path, interface_name,
                       method_name, params, invocation):
    _log(f"method call: {method_name} from {sender}")
    try:
        if method_name == "Status":
            handle_status(connection, sender, params, invocation)
        elif method_name == "SetPrimary":
            handle_set_primary(connection, sender, params, invocation)
        elif method_name == "SetAuto":
            handle_set_auto(connection, sender, params, invocation)
        else:
            invocation.return_gerror(GLib.Error.new_literal(
                GLib.quark_from_string("gpu-switcher-error"),
                f"Unknown method: {method_name}", 1))
    except Exception:
        _internal_error(
            invocation,
            f"unhandled exception in {method_name}:\n{traceback.format_exc()}")
    _reset_idle_timer()


_LOOP = None


def on_bus_acquired(connection, name):
    _log(f"connected to system bus (name: {name})")


def on_name_acquired(connection, name):
    _log(f"name acquired: {name}; ready to serve calls")


def on_name_lost(connection, name):
    where = "on the owning connection" if connection is not None else "connection closed"
    _log(f"name lost: {name} {where}; exiting")
    if _LOOP is not None:
        _LOOP.quit()


def main():
    global _LOOP
    _log(f"helper starting (uid={os.getuid()}, argv={sys.argv})")
    _LOOP = GLib.MainLoop.new(None, False)
    # Register the object on the bus connection BEFORE owning the name:
    # the broker retries a client's pending call the instant the name is
    # acquired, so the object must already exist (cold-start race).
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    connection.register_object(OBJECT_PATH, node.interfaces[0],
                               handle_method_call, None, None)
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
