#!/usr/bin/env python3
"""gpu-switcher helper: D-Bus-activated system-bus service (runs as root).

dbus-daemon starts this process on demand when an unprivileged client (the
GUI app) calls one of its methods. M0 exposes only the read-only Status();
M2 adds SetPrimary/SetAuto/RefreshUdev behind a polkit authorization check.
"""
import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

SERVICE_NAME = "com.github.wl04.gpu_switcher.Helper"
OBJECT_PATH = "/com/github/wl04/gpu_switcher/Helper"
INTERFACE = "com.github.wl04.gpu_switcher.Helper"

MANAGED_RULE = "/etc/udev/rules.d/61-mutter-primary-gpu.rules"

INTROSPECTION_XML = f"""<node name="{OBJECT_PATH}">
  <interface name="{INTERFACE}">
    <method name="Status">
      <arg type="s" name="locale" direction="in"/>
      <arg type="s" name="rule_on_disk" direction="out"/>
      <arg type="s" name="message" direction="out"/>
    </method>
  </interface>
</node>
"""


def read_managed_rule() -> str:
    """Return the managed udev rule as text, or '' when it does not exist."""
    try:
        with open(MANAGED_RULE, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def handle_method_call(connection, sender, object_path, interface_name,
                       method_name, params, invocation):
    if method_name == "Status":
        rule = read_managed_rule()
        if rule:
            message = "Managed rule present."
        else:
            message = "No managed rule (auto selection)."
        invocation.return_value(GLib.Variant("(ss)", (rule, message)))
        return
    error = GLib.Error.new_literal("gpu-switcher-error", 1, f"Unknown method: {method_name}")
    invocation.return_error(error)


def on_bus_acquired(connection, name):
    node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
    connection.register_object(OBJECT_PATH, node.interfaces[0], handle_method_call,
                              None, None)


def on_name_acquired(connection, name):
    pass


_LOOP = None


def on_name_lost(connection, name):
    if _LOOP is not None:
        _LOOP.quit()


def main() -> None:
    global _LOOP
    _LOOP = GLib.MainLoop.new(None, False)
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        SERVICE_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )
    _LOOP.run()


if __name__ == "__main__":
    main()
