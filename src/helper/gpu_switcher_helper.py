#!/usr/bin/env python3
"""gpu-switcher helper: D-Bus-activated system-bus service (runs as root).

dbus-daemon starts this process on demand when an unprivileged client (the
GUI app) calls one of its methods.

Methods:
  Status(locale)            -> (rule_on_disk, message)
  SetPrimary(pci, locale)   -> (ok, message, backup_path)
  SetAuto(locale)           -> (ok, message)

Authorization: polkit CheckAuthorization over raw D-Bus with the INTERACTIVE
flag, so the standard agent dialog authenticates the caller. The dialog
message is localized from the locale argument, because a root-activated
process has no user locale of its own.

Privileged work mirrors the reference script mutter-primary-gpu.sh: the same
managed rule file, the same rule line format, the same backup scheme, the
same conflict scan, and udevadm verify before the rule is moved into place.
"""
import gettext
import os
import re
import shutil
import subprocess
import tempfile
import time

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
POLKIT_INTERACTIVE = 4  # POLKIT_AUTHORIZATION_CHECK_INTERACTIVE

MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"
MUTTER_TAG = "mutter-device-preferred-primary"
MUTTER_ENV = "MUTTER_DEVICE_PREFERRED_PRIMARY"
LOCALE_BASE = "/usr/share/gpu-switcher/locale"

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


def _sender_pid_uid(invocation):
    """(pid, uid) of the D-Bus caller, via org.freedesktop.DBus."""
    unique = invocation.get_message().get_remote_peer()
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    variant = GLib.Variant("(s)", (unique,))

    def dbus_int(method):
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            method, variant, "(i)", 0, 5000, None)
        return result[0]

    return dbus_int("GetConnectionUnixProcessID"), dbus_int("GetConnectionUnixUser")


def _authorize(pid, uid, locale, message_text):
    """polkit CheckAuthorization with the INTERACTIVE flag.

    The polkit subject is an a{sv} dictionary carrying the caller's pid and
    unix user, per the polkit Reference Manual. The locale argument selects
    the localized policy message for the agent dialog; the caller supplies
    it because a root-activated process has no user locale of its own.
    """
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    subject = GLib.Variant("a{sv}", {
        "pid": GLib.Variant("i", pid),
        "unix-user": GLib.Variant("a{sv}", {"uid": GLib.Variant("i", uid)}),
    })
    params = GLib.Variant("(a{sv}sssu)",
                         (subject, POLKIT_ACTION, locale, message_text,
                          POLKIT_INTERACTIVE))
    try:
        result = connection.call_sync(
            "org.freedesktop.PolicyKit1", POLKIT_PATH, POLKIT_IFACE,
            "CheckAuthorization", params, "(bbis)",
            0, 120000, None)
    except GLib.Error:
        return False
    authorized, _allow_active, _interactive_required, _error_message = result
    return bool(authorized)


def _authorized(invocation, locale, message_text):
    """Full polkit check for the caller of this invocation."""
    try:
        pid, uid = _sender_pid_uid(invocation)
        if pid < 0 or uid < 0:
            return False
    except Exception:
        return False
    return _authorize(pid, uid, locale, message_text)


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
    if not _authorized(invocation, locale,
                       tr("Authentication is required to change the primary display adapter.")):
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False, tr("Authorization was denied. Nothing changed."), "")))
        return
    conflicts = _rule_conflicts()
    if conflicts:
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("Another udev rule sets the preferred GPU: %s. "
                             "Resolve it manually first.") % conflicts[0],
                          "")))
        return
    resolved = _resolve_gpu(pci)
    if resolved is None:
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("Target PCI %s is not a valid display controller.") % pci,
                          "")))
        return
    _card, vendor, device = resolved

    if os.path.islink(MANAGED_RULE):
        invocation.return_value(
            GLib.Variant("(bss)",
                         (False,
                          tr("The managed rule is a symlink; refusing to modify it."),
                          "")))
        return

    content = _rule_text(pci, vendor, device)
    existing = _read(MANAGED_RULE)
    if existing == content:
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
    if not _authorized(invocation, locale,
                       tr("Authentication is required to change the primary display adapter.")):
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False, tr("Authorization was denied. Nothing changed."))))
        return
    if os.path.islink(MANAGED_RULE):
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("The managed rule is a symlink; refusing to remove it."))))
        return
    if not os.path.exists(MANAGED_RULE):
        invocation.return_value(
            GLib.Variant("(bs)",
                         (True,
                          tr("No managed rule (auto selection already in effect)."))))
        return
    backup = _backup_rule()
    if backup == "":
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("Backup of the existing rule failed; nothing removed."))))
        return
    try:
        os.unlink(MANAGED_RULE)
    except OSError:
        invocation.return_value(
            GLib.Variant("(bs)",
                         (False,
                          tr("Failed to remove the managed rule; nothing changed."))))
        return
    message = tr("Managed rule removed. A backup was saved to %s. "
                 "Auto selection applies after a reboot.") % backup
    if not _reload_rules():
        message += " " + tr("Note: udev rule reload failed; "
                            "the change will take effect on reboot.")
    invocation.return_value(GLib.Variant("(bs)", (True, message)))


def handle_method_call(connection, sender, object_path, interface_name,
                       method_name, params, invocation):
    if method_name == "Status":
        handle_status(connection, sender, params, invocation)
    elif method_name == "SetPrimary":
        handle_set_primary(connection, sender, params, invocation)
    elif method_name == "SetAuto":
        handle_set_auto(connection, sender, params, invocation)
    else:
        invocation.return_error(GLib.Error.new_literal(
            GLib.quark_from_string("gpu-switcher-error"),
            f"Unknown method: {method_name}", 1))


def on_name_acquired(connection, name):
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    connection.register_object(OBJECT_PATH, node.interfaces[0],
                               handle_method_call, None, None)


_LOOP = None


def on_name_lost(connection, name):
    if _LOOP is not None:
        _LOOP.quit()


def main():
    global _LOOP
    _LOOP = GLib.MainLoop.new(None, False)
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        SERVICE_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_name_acquired,
        on_name_lost,
        handle_method_call,
    )
    _LOOP.run()


if __name__ == "__main__":
    main()
