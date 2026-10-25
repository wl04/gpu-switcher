"""Primary GPU Switcher - GTK4 application.

M1: GPU list with "In use now" / "Preferred (configured)" badges and the
GNOME Wayland session guard. The polkit-gated Apply / Reboot / Log out
actions arrive in M2-M3 (see PLAN.md).
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from . import detect, i18n

APP_ID = "com.github.wl04.gpu-switcher"


def _badge(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


class GpuSwitcherApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def _gpu_row(self, radio, name: str, sub: str, badges: list) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(radio)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.append(Gtk.Label(label=name))
        lower = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if sub:
            sub_label = Gtk.Label(label=sub)
            sub_label.add_css_class("dim-label")
            lower.append(sub_label)
        for badge in badges:
            lower.append(_badge(badge))
        text.append(lower)
        row.append(text)
        return row

    def do_activate(self) -> None:
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("gpu-switcher")
        window.set_default_size(560, 430)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        result = detect.detect()

        if not result.gnome_wayland:
            warning = Gtk.Label(label=i18n._(
                "Not running in a GNOME Wayland session - this build only supports GNOME."))
            warning.set_wrap(True)
            warning.add_css_class("dim-label")
            box.append(warning)

        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if not result.gpus:
            rows.append(Gtk.Label(label=i18n._("No GPUs detected.")))
        else:
            first_radio = None
            radios_by_gpu = {}
            for gpu in result.gpus:
                badges = []
                if gpu.in_use:
                    badges.append(i18n._("In use now"))
                if gpu.preferred:
                    badges.append(i18n._("Preferred (configured)"))
                radio = Gtk.CheckButton(label="")
                radio.add_css_class("radio")
                if first_radio is None:
                    first_radio = radio
                else:
                    radio.set_group(first_radio)
                radios_by_gpu[gpu.card] = radio
                sub = f"PCI {gpu.pci} \u00b7 {gpu.node}"
                rows.append(self._gpu_row(radio, gpu.name, sub, badges))
            auto = Gtk.CheckButton(label="")
            auto.add_css_class("radio")
            auto.set_group(first_radio)
            rows.append(self._gpu_row(auto,
                                     i18n._("System default (auto)"),
                                     "", []))
            # Default selection: the configured GPU, else system default.
            for gpu in result.gpus:
                if gpu.preferred:
                    radios_by_gpu[gpu.card].set_active(True)
                    break
            else:
                auto.set_active(True)
        box.append(rows)

        if result.running_card is None:
            unknown = Gtk.Label(label=i18n._(
                "Running primary: unknown (verify in the journal after restart)."))
            unknown.set_wrap(True)
            unknown.add_css_class("dim-label")
            box.append(unknown)

        note = Gtk.Label(label=i18n._(
            "M1: detection and selection only. "
            "Apply, Reboot and Log out arrive in M2-M3."))
        note.set_wrap(True)
        note.add_css_class("dim-label")
        box.append(note)

        window.set_child(box)
        window.present()


def run(argv) -> int:
    app = GpuSwitcherApp()
    return app.run(argv)


if __name__ == "__main__":
    import sys

    sys.exit(run(sys.argv))
