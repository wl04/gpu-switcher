"""Primary GPU Switcher - GTK4 application.

M1: GPU list with "In use now" / "Preferred (configured)" badges and the
GNOME Wayland session guard.
M2: polkit-gated Apply / Reset to auto, Reboot now, confirmation dialogs
and a contextual status banner (see PLAN.md).
M3: Log out fast path (login1 Session.Terminate; the udev refresh already
ran inside Apply/Reset under the same polkit authorization), kept
disabled until an apply whose udev refresh succeeded, plus a collapsible
verification hint with the journalctl command from Mutter's docs.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, GLib

from . import bus, detect, i18n

APP_ID = "com.github.wl04.gpu-switcher"
SELECTION_AUTO = "auto"


def _badge(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


class GpuSwitcherApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window = None
        self.detection = None
        self.selected_card = SELECTION_AUTO   # card name or SELECTION_AUTO
        self.radios_by_gpu: dict = {}
        self.rows_box = None
        self.banner = None
        self.banner_label = None
        self.apply_btn = None
        self.reset_btn = None
        self.reboot_btn = None
        self.logout_btn = None
        self.fast_path_pending = False  # last apply/reset refreshed udev successfully

    # ------------------------------------------------------------------ UI

    def _gpu_row(self, radio, name: str, sub: str, badges: list) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(radio)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label(label=name)
        # A bare GtkLabel centers its text inside the box (default
        # xalign is 0.5); the name must sit at the left edge like the
        # PCI line below it.
        name_label.set_xalign(0.0)
        text.append(name_label)
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

    def _build_content(self) -> None:
        result = self.detection
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        if not result.gnome_wayland:
            warning = Gtk.Label(label=i18n._(
                "Not running in a GNOME Wayland session - this build only supports GNOME."))
            warning.set_wrap(True)
            warning.add_css_class("dim-label")
            box.append(warning)

        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.radios_by_gpu = {}
        if not result.gpus:
            self.rows_box.append(Gtk.Label(label=i18n._("No GPUs detected.")))
        else:
            first_radio = None
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
                card = gpu.card
                radio.connect("toggled", self._on_radio_toggled, card)
                self.radios_by_gpu[card] = radio
                sub = f"PCI {gpu.pci} \u00b7 {gpu.node}"
                self.rows_box.append(self._gpu_row(radio, gpu.name, sub, badges))
            self.auto_radio = Gtk.CheckButton(label="")
            self.auto_radio.add_css_class("radio")
            self.auto_radio.set_group(first_radio)
            self.auto_radio.connect("toggled", self._on_radio_toggled,
                                   SELECTION_AUTO)
            self.rows_box.append(self._gpu_row(
                self.auto_radio, i18n._("System default (auto)"), "", []))
            known = self.selected_card in self.radios_by_gpu
            if self.selected_card == SELECTION_AUTO or not known:
                self.auto_radio.set_active(True)
            else:
                self.radios_by_gpu[self.selected_card].set_active(True)
        box.append(self.rows_box)

        if result.running_card_error is not None:
            text = i18n._(
                "Running primary: could not read the session journal (%s).") \
                % result.running_card_error
        elif result.running_card is None:
            text = i18n._(
                "Running primary: unknown (verify in the journal after restart).")
        else:
            text = None
        if text is not None:
            unknown = Gtk.Label(label=text)
            unknown.set_wrap(True)
            unknown.add_css_class("dim-label")
            box.append(unknown)

        # Status banner: Gtk.InfoBar is deprecated since GTK 4.10; the
        # documented replacement is a Revealer with a Box + Label using
        # Adwaita style classes (no custom CSS).
        self.banner = Gtk.Revealer()
        self.banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.banner_label = Gtk.Label(label="")
        self.banner_label.set_wrap(True)
        self.banner_label.set_margin_top(6)
        self.banner_label.set_margin_bottom(6)
        self.banner_label.set_margin_start(12)
        self.banner_label.set_margin_end(12)
        self.banner_box.append(self.banner_label)
        self.banner.set_child(self.banner_box)
        self.banner.set_reveal_child(False)
        if result.conflicts:
            self._banner_show(
                i18n._("Another udev rule sets the preferred GPU: %s. "
                        "Resolve it manually first.")
                % result.conflicts[0], "warning")
        box.append(self.banner)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.apply_btn = Gtk.Button(label=i18n._("Apply"))
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self._on_apply)
        buttons.append(self.apply_btn)
        self.reset_btn = Gtk.Button(label=i18n._("Reset to auto"))
        self.reset_btn.connect("clicked", self._on_reset)
        buttons.append(self.reset_btn)
        self.reboot_btn = Gtk.Button(label=i18n._("Reboot now"))
        self.reboot_btn.add_css_class("destructive-action")
        self.reboot_btn.connect("clicked", self._on_reboot)
        buttons.append(self.reboot_btn)
        self.logout_btn = Gtk.Button(label=i18n._("Log out"))
        self.logout_btn.connect("clicked", self._on_logout)
        self.logout_btn.set_sensitive(self._fast_path_enabled())
        buttons.append(self.logout_btn)
        box.append(buttons)

        # Verification hint (footer, collapsible): the exact command from
        # Mutter's multi-GPU docs. Prose lines use the regular font; only
        # the command and the expected journal line are monospace.
        hint = Gtk.Expander()
        hint.set_label(i18n._("How to verify the switch after restart"))
        hint_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hint_box.set_margin_top(8)
        # Left-align the block: the expander centers its child otherwise,
        # and a bare GtkLabel centers its text inside the box (default
        # xalign is 0.5), so each line needs an explicit xalign of 0.
        lead = Gtk.Label(label=i18n._("Run in a terminal after reboot or login:"))
        lead.set_wrap(True)
        command = Gtk.Label(label="journalctl -b0 /usr/bin/gnome-shell "
                                  "| grep -E 'selected (as )?primary'")
        command.set_wrap(True)
        command.add_css_class("monospace")
        command.add_css_class("dim-label")
        mid = Gtk.Label(label=i18n._("and look for a line like:"))
        example = Gtk.Label(
            label="GPU /dev/dri/cardN selected primary given udev rule")
        example.add_css_class("monospace")
        example.add_css_class("dim-label")
        example.set_wrap(True)
        auto_example = Gtk.Label(
            label="Boot VGA GPU /dev/dri/cardN selected as primary")
        auto_example.add_css_class("monospace")
        auto_example.add_css_class("dim-label")
        auto_example.set_wrap(True)
        for label in (lead, command, mid, example, auto_example):
            label.set_xalign(0.0)
            hint_box.append(label)
        hint.set_child(hint_box)
        box.append(hint)

        self.window.set_child(box)

    def _on_radio_toggled(self, radio, card: str) -> None:
        if radio.get_active():
            self.selected_card = card

    def _banner_show(self, text: str, kind: str) -> None:
        for css_class in ("suggested-action", "destructive-action", "dim-label"):
            self.banner_box.remove_css_class(css_class)
        if kind == "warning":
            self.banner_box.add_css_class("dim-label")
        elif kind == "error":
            self.banner_box.add_css_class("destructive-action")
        else:
            self.banner_box.add_css_class("suggested-action")
        self.banner_label.set_label(text)
        self.banner.set_reveal_child(True)

    def _set_busy(self, busy: bool) -> None:
        for button in (self.apply_btn, self.reset_btn, self.reboot_btn,
                       self.logout_btn):
            button.set_sensitive(not busy)

    def _fast_path_enabled(self) -> bool:
        """Log out is only useful after an apply/reset whose udev refresh
        succeeded: the tags are then fresh, so a logout/login switches the
        primary without another prompt. A failed refresh keeps it disabled
        (reboot is the only path left)."""
        return self.fast_path_pending

    def _confirm(self, text: str, confirm_label: str, on_result) -> None:
        """Modal confirmation via Gtk.AlertDialog (GTK >= 4.10).

        The old Gtk.MessageDialog run()/add_button API no longer exists in
        GTK 4; choose()/choose_finish() is the modern async flow. The result
        is the clicked button's index: 0 = Cancel, 1 = confirm button; Escape
        and closing the window both map to Cancel. The dialog's window is
        disposed by choose_finish itself.
        """
        dialog = Gtk.AlertDialog()
        dialog.set_message(text)
        dialog.set_modal(True)
        dialog.set_buttons([i18n._("Cancel"), confirm_label])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)

        def on_choose(_d, task):
            try:
                index = _d.choose_finish(task)
            except GLib.Error:
                index = -1
            on_result(index == 1)

        dialog.choose(self.window, None, on_choose)

    # ------------------------------------------------------------- actions

    def _on_apply(self, _button) -> None:
        if self.selected_card != SELECTION_AUTO:
            gpu = next((g for g in self.detection.gpus
                        if g.card == self.selected_card), None)
            if gpu is None:  # card vanished since selection; fall back to auto
                self.selected_card = SELECTION_AUTO
                self.auto_radio.set_active(True)
        if self.selected_card == SELECTION_AUTO:
            target = SELECTION_AUTO
            text = i18n._(
                "Apply removes the managed udev rule, returning to automatic "
                "GPU selection.\n\n"
                "If a rule is present, a backup is saved. The change takes "
                "effect after a reboot, or after logging out and back in.")
        else:
            target = gpu.pci
            text = i18n._(
                "Apply sets %s (PCI %s) as the primary display adapter.\n\n"
                "The managed udev rule is written; if a rule is present, a "
                "backup is saved. The change takes effect after a reboot, or "
                "after logging out and back in.") % (gpu.name, gpu.pci)

        self._confirm(text, i18n._("Apply"),
                      lambda ok: self._do_apply(ok, target))

    def _revert_selection(self) -> None:
        """Reset the radio selection to the currently configured GPU.

        Used when a pending action is cancelled or fails: nothing on disk
        changed, so the UI must show the real configured state, not the
        un-applied choice.
        """
        preferred = self.detection.preferred_pci
        card = next((g.card for g in self.detection.gpus
                     if g.pci == preferred), None)
        if card is None:
            self.selected_card = SELECTION_AUTO
            self.auto_radio.set_active(True)
        else:
            self.selected_card = card
            self.radios_by_gpu[card].set_active(True)

    def _do_apply(self, ok, target) -> None:
        if not ok:
            self._revert_selection()
            return
        self._set_busy(True)
        locale = i18n._requested_language()
        if target == SELECTION_AUTO:
            bus.set_auto(locale, self._on_set_auto_done)
        else:
            bus.set_primary(target, locale, self._on_set_primary_done)

    def _on_set_primary_done(self, ok, message, _backup, refresh_ok) -> None:
        self._set_busy(False)
        if ok:
            self.fast_path_pending = bool(refresh_ok)
            self._banner_show(message, "success")
            self._refresh()
        else:
            self._banner_show(message, "error")
            self._revert_selection()

    def _on_set_auto_done(self, ok, message, refresh_ok) -> None:
        self._set_busy(False)
        if ok:
            self.fast_path_pending = bool(refresh_ok)
            self._banner_show(message, "success")
            self._refresh()
        else:
            self._banner_show(message, "error")
            self._revert_selection()

    def _on_reset(self, _button) -> None:
        self.selected_card = SELECTION_AUTO
        self.auto_radio.set_active(True)
        self._on_apply(None)

    def _on_reboot(self, _button) -> None:
        self._confirm(i18n._("Reboot the computer now?"),
                      i18n._("Reboot"), self._do_reboot)

    def _do_reboot(self, ok) -> None:
        if not ok:
            return
        self._confirm(
            i18n._("Are you sure? The system will reboot immediately and "
                   "the selected GPU will become primary on boot."),
            i18n._("Reboot"), self._do_reboot_now)

    def _do_reboot_now(self, ok) -> None:
        if not ok:
            return
        self._set_busy(True)

        def done(ok, message):
            if ok:
                # The machine is going down; keep the UI calm.
                self._banner_show(i18n._("Rebooting."), "info")
            else:
                self._set_busy(False)
                self._banner_show(message or i18n._("Reboot failed."), "error")

        bus.reboot(done)

    # ------------------------------------------------------------- fast path

    def _on_logout(self, _button) -> None:
        self._confirm(i18n._(
            "Log out now? The session will end, and the configured GPU "
            "takes effect on the next login."),
            i18n._("Log out"), self._do_logout)

    def _do_logout(self, ok) -> None:
        if not ok:
            return
        self._set_busy(True)
        self._banner_show(i18n._("Logging out."), "info")
        bus.logout(self._on_logout_done)

    def _on_logout_done(self, ok, message) -> None:
        if ok:
            # The session is ending; the app dies with it.
            return
        self._set_busy(False)
        self._banner_show(message or i18n._("Log out failed."), "error")

    def _refresh(self) -> None:
        """Re-detect (read-only) and rebuild the window after a change."""
        self.detection = detect.detect()
        known = self.selected_card in self.radios_by_gpu
        if not known:
            self.selected_card = SELECTION_AUTO
        self._build_content()

    # ------------------------------------------------------------- lifecycle

    def do_activate(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("gpu-switcher")
        self.window.set_default_size(560, 430)
        self.detection = detect.detect()
        # Default selection: the configured GPU, else system default.
        for gpu in self.detection.gpus:
            if gpu.preferred:
                self.selected_card = gpu.card
                break
        self._build_content()
        self.window.present()


def run(argv) -> int:
    app = GpuSwitcherApp()
    return app.run(argv)


if __name__ == "__main__":
    import sys

    sys.exit(run(sys.argv))
