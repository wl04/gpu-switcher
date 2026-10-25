"""Primary GPU Switcher - GTK4 application (M0 skeleton).

M0 shows the window shell and localized strings only. GPU enumeration,
primary-GPU detection and the polkit-gated apply/activation buttons arrive
in M1-M3 (see PLAN.md).
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from . import i18n

APP_ID = "com.github.wl04.gpu-switcher"


class GpuSwitcherApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self)
        window.set_title(i18n._("Primary GPU Switcher"))
        window.set_default_size(560, 430)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label(label=i18n._("Primary GPU Switcher"))
        title.add_css_class("large-title")
        box.append(title)

        placeholder = Gtk.Label(
            label=i18n._("M0 skeleton - GPU detection arrives in the next milestone.")
        )
        placeholder.set_wrap(True)
        box.append(placeholder)

        window.set_child(box)
        window.present()


def run(argv) -> int:
    app = GpuSwitcherApp()
    return app.run(argv)
