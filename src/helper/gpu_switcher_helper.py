#!/usr/bin/env python3
"""gpu-switcher helper: D-Bus-activated system-bus service (runs as root).

dbus-broker starts this process on demand when an unprivileged client (the
GUI app) calls one of its methods.

Methods:
  Status(locale)            -> (rule_on_disk, message)
  SetPrimary(pci, locale)   -> (ok, message, backup_path)
  SetAuto(locale)           -> (ok, message)

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

SERVICE_NAME = "com.github.wl04.gpu_switcher.Helper"
OBJECT_PATH = "/com/github/wl04/gpu_switcher/Helper"
INTERFACE = "com.github.wl04.gpu_switcher.Helper"

POLKIT_ACTION = "com.github.wl04.gpu_switcher.set-primary"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_INTERACTIVE = 1  # CheckAuthorizationFlags: AllowUserInteraction

MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"
MUTTER_TAG = "mutter-device-preferred-primary"
MUTTER_ENV = "MUTTER_DEVICE_PREFERRED_PRIMARY"
LOCALE_BASE = "/usr/share/gpu-switcher/locale"
LOG_PATH = "/var/log/gpu-switcher-helper.log"

RULE_DIRS = (
    "/etc/udev/rules.d",
    "/run/udev/rules.d",
    "/usr/local/lib/udev/rules.d",
    "/usr/lib/udev/rules.d",
    "/lib/udev/rules.d",
)

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
    </method>
    <method name="SetAuto">
      <arg type="s" name="locale" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="message" direction="out"/>
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


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


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
    vendor = _read(f"{pdir}/vendor")
    device = _read(f"{pdir}/device")
    if _read(f"{pdir}/class") != "0x030000":
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


def _rule_conflicts():
    """Other udev rules that set the primary-GPU tag.

    Root can read every rule file, so this is the authoritative scan (the
    app-side scan only flags readable files).
    """
    conflicts = []
    seen = set()
    for d in RULE_DIRS:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".rules") or entry in seen:
                continue
            seen.add(entry)
            path = os.path.join(d, entry)
            if path == MANAGED_RULE:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                conflicts.append(path)
                continue
            lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
            if any(MUTTER_TAG in l or MUTTER_ENV in l for l in lines):
                conflicts.append(path)
    return conflicts


def _rule_text(pci, vendor, device):
    # Same rule line format as the reference script mutter-primary-gpu.sh.
    return (
        f"# Managed by gpu-switcher; selected PCI {pci}\n"
        "# Mutter uses TAG; ENV is retained for compatibility with the previous local rule.\n"
        'SUBSYSTEM=="drm", ENV{DEVTYPE}=="drm_minor", KERNEL=="card[0-9]*", '
        'KERNEL!="card*-*", SUBSYSTEMS=="pci", KERNELS=="' + pci + '",'
        ' ATTRS{vendor}=="' + vendor + '",'
        ' ATTRS{device}=="' + device + '",'
        ' ENV{MUTTER_DEVICE_PREFERRED_PRIMARY}="1",'
        ' TAG+="mutter-device-preferred-primary"\n'
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
    rule = _read(MANAGED_RULE)
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
            GLib.Variant("(bss)",
                         (False, tr("Authorization was denied. Nothing changed."), "")))
        return
    _log("SetPrimary: authorized")
    conflicts = _rule_conflicts()
    if conflicts:
        _log(f"SetPrimary: conflicts found: {conflicts}")
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("Another udev rule sets the preferred GPU: %s. "
                             "Resolve it manually first.") % conflicts[0],
                          "")))
        return
    resolved = _resolve_gpu(pci)
    if resolved is None:
        _log(f"SetPrimary: invalid PCI {pci}")
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("Target PCI %s is not a valid display controller.") % pci,
                          "")))
        return
    _card, vendor, device = resolved
    _log(f"SetPrimary: resolved {pci} -> card={_card} vendor={vendor} device={device}")

    if os.path.islink(MANAGED_RULE):
        _log("SetPrimary: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("The managed rule is a symlink; refusing to modify it."),
                          "")))
        return

    content = _rule_text(pci, vendor, device)
    try:
        with open(MANAGED_RULE, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if existing == content:
        _log(f"SetPrimary: rule already set for {pci}; nothing to do")
        invocation.return_value(
            GLib.Variant("(bss)",
                         (True,
                          tr("The rule is already set for %s. Nothing changed.") % pci,
                          "")))
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
                GLib.Variant("(bss)",
                             (False,
                              tr("udevadm verify rejected the rule; nothing changed."),
                              "")))
            return
        if os.path.exists(MANAGED_RULE):
            backup = _backup_rule()
            if backup == "":
                os.unlink(tmp_path)
                _log("SetPrimary: backup of the existing rule failed")
                invocation.return_value(
                    GLib.Variant("(bss)",
                                 (False,
                                  tr("Backup of the existing rule failed; nothing changed."),
                                  "")))
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

    if backup:
        message = tr("Rule written for %s. A backup was saved to %s. "
                     "The change takes effect after a reboot.") % (pci, backup)
    else:
        message = tr("Rule written for %s. "
                     "The change takes effect after a reboot.") % pci
    if not _reload_rules():
        message += " " + tr("Note: udev rule reload failed; "
                            "the change will take effect on reboot.")
    invocation.return_value(GLib.Variant("(bss)", (True, message, backup)))


def handle_set_auto(connection, sender, params, invocation):
    locale = params[0]
    tr = _translator(locale)
    _log(f"SetAuto: locale={locale} sender={sender}")
    if not _authorized(invocation, connection, locale):
        _log("SetAuto: authorization denied or failed; nothing changed")
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False, tr("Authorization was denied. Nothing changed."))))
        return
    _log("SetAuto: authorized")
    if os.path.islink(MANAGED_RULE):
        _log("SetAuto: managed rule is a symlink; refusing")
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("The managed rule is a symlink; refusing to remove it."))))
        return
    if not os.path.exists(MANAGED_RULE):
        _log("SetAuto: no managed rule; already auto")
        invocation.return_value(
            GLib.Variant("(bs)",
                         (True,
                          tr("No managed rule (auto selection already in effect)."))))
        return
    backup = _backup_rule()
    if backup == "":
        _log("SetAuto: backup of the existing rule failed")
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("Backup of the existing rule failed; nothing removed."))))
        return
    try:
        os.unlink(MANAGED_RULE)
    except OSError:
        _log("SetAuto: failed to remove the managed rule")
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("Failed to remove the managed rule; nothing changed."))))
        return
    _log(f"SetAuto: rule removed (backup: {backup})")
    message = tr("Managed rule removed. A backup was saved to %s. "
                 "Auto selection applies after a reboot.") % backup
    if not _reload_rules():
        message += " " + tr("Note: udev rule reload failed; "
                            "the change will take effect on reboot.")
    invocation.return_value(GLib.Variant("(bs)", (True, message)))


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
