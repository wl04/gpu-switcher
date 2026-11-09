"""Render the running app window to a PNG using GTK itself.

Usage: screenshot.py OUTPUT.png

Needs no external screenshot tool and works on Wayland (GNOME's own
screenshot D-Bus API is restricted and gnome-screenshot is gone). The window
is captured from GTK's render node, so it is independent of other windows and
the desktop background. Run through tools/screenshot.sh, which sets the UI
language before this module imports the app.
"""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gpu_switcher import main

SETTLE_MS = 500  # let the rebuilt window lay out after detection
TIMEOUT_S = 30


def _save_png(window: Gtk.Window, path: str) -> None:
    paintable = Gtk.WidgetPaintable.new(window)
    width, height = window.get_width(), window.get_height()
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    texture = window.get_native().get_renderer().render_texture(node, None)
    texture.save_to_png(path)


def run(path: str) -> int:
    app = main.GpuSwitcherApp()
    # A separate id: an installed, already running instance must not receive
    # this activation.
    app.set_application_id(main.APP_ID + ".screenshot")
    status = {"code": 1}
    waited = [0.0]  # seconds spent waiting for detection

    def capture() -> bool:
        _save_png(app.window, path)
        print(f"Saved {path}")
        status["code"] = 0
        app.quit()
        return False

    def poll() -> bool:
        waited[0] += 0.2
        if app.detection is not None:
            GLib.timeout_add(SETTLE_MS, capture)
            return False
        if waited[0] > TIMEOUT_S:
            print("Timed out waiting for detection", file=sys.stderr)
            app.quit()
            return False
        return True

    app.connect_after("activate", lambda _app: GLib.timeout_add(200, poll))
    app.run([sys.argv[0]])
    return status["code"]


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
