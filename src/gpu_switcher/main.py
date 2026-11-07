"""Primary GPU Switcher - GTK4 application.

Unprivileged GUI over the root D-Bus helper: lists the DRM cards with
"In use now" / "Preferred (configured)" badges, guards non-GNOME-Wayland
sessions with a warning, and gates the privileged changes behind polkit
(Apply / Reset to auto) with a contextual status banner. Reboot uses
login1; Log out is the fast path (GNOME session manager Logout, no
polkit prompt). The app tracks the explicit outcome of the last
Apply/Reset (state.Outcome); Log out stays enabled only while not busy
and after a successful, refreshed operation. A collapsible hint gives
the journalctl command to verify the switch after restart. Detection runs
on a worker thread, so the main loop never blocks on it.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gtk

from . import bus, detect, i18n
from .selection import (
    SELECTION_AUTO,
    SELECTION_UNAVAILABLE,
    apply_target,
    configured_unavailable_pci,
    mirror_configured_state,
)
from .state import (
    BannerKind,
    Outcome,
    actions_enabled,
    banner_kind,
    logout_enabled,
    outcome_from_result,
)

APP_ID = "com.github.wl04.gpu-switcher"


def _badge(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


class GpuSwitcherApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.window = None
        self.detection = None
        self.selected_card = SELECTION_AUTO  # card name or a SELECTION_* sentinel
        self.unavailable_radio = (
            None  # disabled row for the configured-but-undetected GPU
        )
        self.auto_radio = None
        self.unavailable_pci = None
        self.radios_by_gpu: dict = {}
        self.rows_box = None
        self.banner = None
        self.banner_box = None
        self.banner_label = None
        self.apply_btn = None
        self.reset_btn = None
        self.reboot_btn = None
        self.logout_btn = None
        self.outcome = Outcome.idle  # explicit state of the last operation
        self._detect_generation = 0  # discards stale detection results
        self._pending_banner = None  # (text, kind) shown after a rebuild

    # ------------------------------------------------------------------ UI

    def _build_loading(self) -> None:
        """Minimal content while the first detection runs: a dim
        'Detecting…' label; the full content replaces it when detection
        finishes."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        label = Gtk.Label(label=i18n._("Detecting…"))
        label.add_css_class("dim-label")
        box.append(label)
        self.window.set_child(box)

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
        """Assemble the window content; each section has its own builder."""
        result = self.detection
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        warning = self._build_session_warning(result)
        if warning is not None:
            box.append(warning)
        self._build_rows(result)
        box.append(self.rows_box)
        note = self._build_running_primary_note(result)
        if note is not None:
            box.append(note)
        self._build_banner(result)
        box.append(self.banner)
        box.append(self._build_buttons())
        box.append(self._build_verification_hint())
        self.window.set_child(box)

    def _build_session_warning(self, result) -> Gtk.Label | None:
        """Warning label outside a GNOME Wayland session, else None."""
        if result.gnome_wayland:
            return None
        warning = Gtk.Label(
            label=i18n._(
                "Not running in a GNOME Wayland session - this build only supports GNOME."
            )
        )
        warning.set_wrap(True)
        warning.add_css_class("dim-label")
        return warning

    def _build_rows(self, result) -> None:
        """One radio row per GPU, an optional disabled row for a
        configured GPU that is not currently detected, plus the
        always-present auto row.

        The auto row is created even when no GPUs are detected so
        Reset / the revert path never touch a missing widget. The
        selected radio always mirrors the configured state: the card
        matching the managed rule, the unavailable row when the rule
        pins a PCI no detected GPU carries, else auto.
        """
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.radios_by_gpu = {}
        first_radio = None
        for gpu in result.gpus:
            badges = []
            if gpu.in_use:
                badges.append(i18n._("In use now"))
            if gpu.preferred:
                badges.append(i18n._("Preferred (configured)"))
            radio = Gtk.CheckButton(label="")
            radio.add_css_class("radio")
            if first_radio is not None:
                radio.set_group(first_radio)
            first_radio = radio
            card = gpu.card
            radio.connect("toggled", self._on_radio_toggled, card)
            self.radios_by_gpu[card] = radio
            sub = f"PCI {gpu.pci} \u00b7 {gpu.node}"
            self.rows_box.append(self._gpu_row(radio, gpu.name, sub, badges))
        self.unavailable_radio = None
        self.unavailable_pci = configured_unavailable_pci(result)
        if self.unavailable_pci is not None:
            # The configured GPU is not currently detected: show the rule's
            # PCI as a disabled radio (not user-selectable) so the UI
            # mirrors the configured state without pretending it is
            # selectable.
            unavailable = Gtk.CheckButton(label="")
            unavailable.add_css_class("radio")
            unavailable.set_sensitive(False)
            if first_radio is not None:
                unavailable.set_group(first_radio)
            unavailable.connect(
                "toggled", self._on_radio_toggled, SELECTION_UNAVAILABLE
            )
            self.unavailable_radio = unavailable
            self.rows_box.append(
                self._gpu_row(
                    unavailable,
                    i18n._("Configured GPU (not currently detected)"),
                    f"PCI {self.unavailable_pci}",
                    [i18n._("Preferred (configured)")],
                )
            )
        self.auto_radio = Gtk.CheckButton(label="")
        self.auto_radio.add_css_class("radio")
        if first_radio is not None:
            self.auto_radio.set_group(first_radio)
        self.auto_radio.connect("toggled", self._on_radio_toggled, SELECTION_AUTO)
        self.rows_box.append(
            self._gpu_row(self.auto_radio, i18n._("System default (auto)"), "", [])
        )
        # Initial build: mirror the managed rule on disk (auto when nothing
        # is configured).
        self._select_radio(mirror_configured_state(result))

    def _select_radio(self, card: str) -> None:
        """Point the radio group at card (auto, the unavailable sentinel,
        or a real detected GPU)."""
        self.selected_card = card
        if card == SELECTION_AUTO:
            self.auto_radio.set_active(True)
        elif card == SELECTION_UNAVAILABLE:
            self.unavailable_radio.set_active(True)
        else:
            self.radios_by_gpu[card].set_active(True)

    def _build_running_primary_note(self, result) -> Gtk.Label | None:
        """Note when the running primary is unknown or the session journal
        could not be read; None when a running primary is known."""
        if result.running_card_error is not None:
            text = (
                i18n._("Running primary: could not read the session journal (%s).")
                % result.running_card_error
            )
        elif result.running_card is None:
            text = i18n._(
                "Running primary: unknown (verify in the journal after restart)."
            )
        else:
            text = None
        if text is None:
            return None
        note = Gtk.Label(label=text)
        note.set_wrap(True)
        note.add_css_class("dim-label")
        return note

    def _build_banner(self, result) -> None:
        """Status banner: Gtk.InfoBar is deprecated since GTK 4.10; the
        documented replacement is a Revealer with a Box + Label using
        Adwaita style classes (no custom CSS). A rule conflict is shown
        as a warning before any action."""
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
        if self.unavailable_pci is not None:
            self._banner_show(
                i18n._(
                    "The configured GPU (PCI %s) is not currently "
                    "detected. The rule remains on disk; it takes "
                    "effect when the GPU is available again. Use "
                    "'Reset to auto' to remove it."
                )
                % self.unavailable_pci,
                BannerKind.WARNING,
            )
        elif result.conflicts:
            self._banner_show(
                i18n._(
                    "Another udev rule sets the preferred GPU: %s. "
                    "Resolve it manually first."
                )
                % result.conflicts[0],
                BannerKind.WARNING,
            )

    def _build_buttons(self) -> Gtk.Box:
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
        self.logout_btn.set_sensitive(logout_enabled(False, self.outcome))
        buttons.append(self.logout_btn)
        # Outside a GNOME Wayland session no privileged action works
        # (DESIGN.md: GNOME only); the warning banner explains why.
        actions = actions_enabled(self.detection.gnome_wayland)
        self.apply_btn.set_sensitive(actions)
        self.reset_btn.set_sensitive(actions)
        self.reboot_btn.set_sensitive(actions)
        self.logout_btn.set_sensitive(actions and logout_enabled(False, self.outcome))
        return buttons

    def _build_verification_hint(self) -> Gtk.Expander:
        """Collapsible verification hint: the exact command from
        Mutter's multi-GPU docs. Prose lines use the regular font; only
        the command and the expected journal line are monospace."""
        hint = Gtk.Expander()
        hint.set_label(i18n._("How to verify the switch after restart"))
        hint_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hint_box.set_margin_top(8)
        # Left-align the block: the expander centers its child otherwise,
        # and a bare GtkLabel centers its text inside the box (default
        # xalign is 0.5), so each line needs an explicit xalign of 0.
        lead = Gtk.Label(label=i18n._("Run in a terminal after reboot or login:"))
        lead.set_wrap(True)
        command = Gtk.Label(
            label="journalctl -b0 /usr/bin/gnome-shell "
            "| grep -E 'selected (as )?primary'"
        )
        command.set_wrap(True)
        command.add_css_class("monospace")
        command.add_css_class("dim-label")
        mid = Gtk.Label(label=i18n._("and look for a line like:"))
        example = Gtk.Label(label="GPU /dev/dri/cardN selected primary given udev rule")
        example.add_css_class("monospace")
        example.add_css_class("dim-label")
        example.set_wrap(True)
        auto_example = Gtk.Label(
            label="Boot VGA GPU /dev/dri/cardN selected as primary"
        )
        auto_example.add_css_class("monospace")
        auto_example.add_css_class("dim-label")
        auto_example.set_wrap(True)
        for label in (lead, command, mid, example, auto_example):
            label.set_xalign(0.0)
            hint_box.append(label)
        hint.set_child(hint_box)
        return hint

    def _on_radio_toggled(self, radio, card: str) -> None:
        if radio.get_active():
            self.selected_card = card

    def _banner_show(self, text: str, kind: BannerKind) -> None:
        for css_class in ("suggested-action", "destructive-action", "dim-label"):
            self.banner_box.remove_css_class(css_class)
        if kind is BannerKind.WARNING:
            self.banner_box.add_css_class("dim-label")
        elif kind is BannerKind.ERROR:
            self.banner_box.add_css_class("destructive-action")
        else:
            self.banner_box.add_css_class("suggested-action")
        self.banner_label.set_label(text)
        self.banner.set_reveal_child(True)

    def _set_busy(self, busy: bool) -> None:
        """Busy state for the action buttons; Log out goes through the
        sensitivity rule (state.logout_enabled), so it is never
        re-enabled by un-busy alone."""
        actions = actions_enabled(self.detection.gnome_wayland)
        for button in (self.apply_btn, self.reset_btn, self.reboot_btn):
            button.set_sensitive(actions and not busy)
        self.logout_btn.set_sensitive(actions and logout_enabled(busy, self.outcome))

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
        target, error = apply_target(self.detection, self.selected_card)
        if error is not None:
            # Nothing to apply: the selected GPU is not currently
            # detected. Do not silently fall back to auto - tell the
            # user the rule stays on disk and offer the explicit reset.
            if error == SELECTION_UNAVAILABLE:
                self._banner_show(
                    i18n._(
                        "The configured GPU (PCI %s) is not currently "
                        "detected, so there is nothing to apply. The rule "
                        "stays on disk; use 'Reset to auto' to remove it."
                    )
                    % self.unavailable_pci,
                    BannerKind.WARNING,
                )
            else:
                # The card vanished since the row was built: re-check the
                # world instead of applying a stale selection.
                self._banner_show(
                    i18n._("The selected GPU is no longer detected; re-checking…"),
                    BannerKind.WARNING,
                )
                self._refresh()
            return
        if target == SELECTION_AUTO:
            text = i18n._(
                "Apply removes the managed udev rule, returning to automatic "
                "GPU selection.\n\n"
                "If a rule is present, a backup is saved. The change takes "
                "effect after a reboot, or after logging out and back in."
            )
        else:
            gpu = next(g for g in self.detection.gpus if g.pci == target)
            text = i18n._(
                "Apply sets %s (PCI %s) as the primary display adapter.\n\n"
                "The managed udev rule is written; if a rule is present, a "
                "backup is saved. The change takes effect after a reboot, or "
                "after logging out and back in."
            ) % (gpu.name, gpu.pci)

        self._confirm(text, i18n._("Apply"), lambda ok: self._do_apply(ok, target))

    def _revert_selection(self) -> None:
        """Reset the radio selection to the currently configured GPU.

        Used when a pending action is cancelled or fails: nothing on disk
        changed, so the UI must show the real configured state, not the
        un-applied choice.
        """
        # Revert: nothing on disk changed, so re-mirror the real configured
        # state instead of the un-applied choice.
        self._select_radio(mirror_configured_state(self.detection))

    def _do_apply(self, ok, target) -> None:
        if not ok:
            self._revert_selection()
            return
        self._set_busy(True)
        locale = i18n._requested_language()
        if target == SELECTION_AUTO:
            bus.set_auto(locale, self._on_apply_done)
        else:
            bus.set_primary(target, locale, self._on_apply_done)

    def _on_apply_done(self, ok, message, refresh_ok, timed_out) -> None:
        """Shared SetPrimary/SetAuto result (both normalize to the same
        callback)."""
        if timed_out:
            self._on_helper_timeout()
            return
        self.outcome = outcome_from_result(ok, refresh_ok, False)
        self._set_busy(False)
        if ok:
            # The rebuild replaces the banner widget, so the result is
            # shown only after the rebuild (see _start_detection).
            self._refresh(banner=(message, banner_kind(self.outcome)))
        else:
            self._banner_show(message, BannerKind.ERROR)
            self._revert_selection()

    def _on_helper_timeout(self) -> None:
        """The client call timed out: the helper's outcome is unknown.

        Set the outcome, re-detect and reconcile with the rule on disk,
        then report the unknown outcome.
        """
        self.outcome = Outcome.unknown
        self._set_busy(False)
        self._refresh(
            banner=(
                i18n._(
                    "The operation timed out; the outcome is unknown. "
                    "The current state was rechecked."
                ),
                BannerKind.WARNING,
            )
        )

    def _on_reset(self, _button) -> None:
        self.selected_card = SELECTION_AUTO
        self.auto_radio.set_active(True)
        self._on_apply(None)

    def _on_reboot(self, _button) -> None:
        self._confirm(
            i18n._("Reboot the computer now?"), i18n._("Reboot"), self._do_reboot
        )

    def _do_reboot(self, ok) -> None:
        if not ok:
            return
        self._confirm(
            i18n._(
                "Are you sure? The system will reboot immediately and "
                "the selected GPU will become primary on boot."
            ),
            i18n._("Reboot"),
            self._do_reboot_now,
        )

    def _do_reboot_now(self, ok) -> None:
        if not ok:
            return
        self._set_busy(True)

        def done(ok, message):
            if ok:
                # The machine is going down; keep the UI calm.
                self._banner_show(i18n._("Rebooting."), BannerKind.INFO)
            else:
                self._set_busy(False)
                self._banner_show(message or i18n._("Reboot failed."), BannerKind.ERROR)

        bus.reboot(done)

    # ------------------------------------------------------------- fast path

    def _on_logout(self, _button) -> None:
        self._confirm(
            i18n._(
                "Log out now? The session will end, and the configured GPU "
                "takes effect on the next login."
            ),
            i18n._("Log out"),
            self._do_logout,
        )

    def _do_logout(self, ok) -> None:
        if not ok:
            return
        self._set_busy(True)
        self._banner_show(i18n._("Logging out."), BannerKind.INFO)
        bus.logout(self._on_logout_done)

    def _on_logout_done(self, ok, message) -> None:
        if ok:
            # The session is ending; the app dies with it.
            return
        self._set_busy(False)
        self._banner_show(message or i18n._("Log out failed."), BannerKind.ERROR)

    def _refresh(self, banner=None) -> None:
        """Re-detect (read-only) and rebuild the window after a change.

        Detection runs on a worker thread (see _start_detection); the
        rebuilt selection always mirrors the on-disk configured state (see
        _build_rows), so no manual restore is needed here. `banner` is a
        (text, kind) pair shown only after the rebuild completes, because
        the rebuild replaces the banner widget.
        """
        self._pending_banner = banner
        self._start_detection()

    def _start_detection(self) -> None:
        """Run detect.detect() and the session-leader lookup on a worker
        thread and rebuild the UI on the main loop.

        A generation token discards stale results when a newer detection
        has already superseded this one (rapid Apply/Reset cycles). While
        the detection runs, the existing banner (if any) shows a dim
        'Detecting…'; the pending banner is shown after the rebuild.
        """
        self._detect_generation += 1
        generation = self._detect_generation
        if self.banner is not None:
            self._banner_show(i18n._("Detecting…"), BannerKind.INFO)

        def apply(result):
            if generation != self._detect_generation:
                return  # stale: a newer detection superseded this one
            self.detection = result
            self._build_content()
            banner = self._pending_banner
            self._pending_banner = None
            if banner is not None:
                self._banner_show(*banner)

        def worker():
            try:
                leader = bus.session_leader()
            except Exception:
                leader = None
            try:
                result = detect.detect(leader)
            except Exception as error:
                # Never leave "Detecting…" forever: report the failure.
                # (Captured before the except-block exit clears the binding.)
                error_message = i18n._("Detection failed: %s.") % error

                def show_error():
                    if generation != self._detect_generation:
                        return
                    if self.banner is None:
                        # First detection: there is no banner widget yet,
                        # so the error replaces the loading box.
                        label = Gtk.Label(label=error_message)
                        label.set_wrap(True)
                        label.add_css_class("destructive-action")
                        label.set_xalign(0.0)
                        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                        box.set_margin_top(24)
                        box.set_margin_bottom(24)
                        box.set_margin_start(24)
                        box.set_margin_end(24)
                        box.append(label)
                        self.window.set_child(box)
                        return
                    self._banner_show(error_message, BannerKind.ERROR)

                GLib.idle_add(show_error)
                return
            GLib.idle_add(apply, result)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------- lifecycle

    def do_activate(self) -> None:
        # A second start of the app must not build a second window:
        # present the existing one (single-instance via GApplication).
        if self.window is not None:
            if self.window.is_destroyed():
                # The window was closed; the app is still running, so
                # fall through and build a new window.
                self.window = None
            else:
                self.window.present()
                return
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("gpu-switcher")
        self.window.set_default_size(560, 430)
        self._build_loading()
        self.window.present()
        self._start_detection()


def run(argv) -> int:
    app = GpuSwitcherApp()
    return app.run(argv)


if __name__ == "__main__":
    import sys

    sys.exit(run(sys.argv))
