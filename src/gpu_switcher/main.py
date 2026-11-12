"""Primary GPU Switcher - GTK4 application.

Unprivileged GUI over the root D-Bus helper: lists the GPUs with "In use
now" / "Preferred (configured)" badges, warns outside GNOME Wayland, and
sends Apply / Reset to the helper (polkit-gated) with a status banner. Reboot
goes through login1, Log out through the GNOME session manager. The outcome
of the last Apply/Reset (state.Outcome) decides whether Log out is enabled.
Detection runs on a worker thread so the main loop never blocks.

What the window shows and what an action means are decided in decisions.py: this
module turns those answers into widgets and nothing else, which is what lets a
command-line front-end use the same decisions.
"""

from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gtk

from . import bus, decisions, detect, i18n
from .gpu_state import GpuState, derive_state
from .selection import (
    SELECTION_AUTO,
    SELECTION_UNAVAILABLE,
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


REDETECT_AFTER_S = 15.0


def should_recheck(age_s: float | None, busy: bool) -> bool:
    """Whether returning to the window should read GPUs, rules and journal again.

    The window shows a snapshot, and the world moves while the user is away: a
    monitor gets plugged in, another tool edits the rule, a switch lands. Reading
    on every focus change would mean sysfs and journal traffic for every mouse
    movement, so only a snapshot that has had time to go stale is re-read - and
    never while the window is in the middle of something, because re-detection
    rebuilds the content it is built on.
    """
    if busy:
        return False
    return age_s is None or age_s >= REDETECT_AFTER_S


def _badge(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    return label


def _gpu_row(radio, name: str, sub: str, badges: list) -> Gtk.Box:
    # The name is the radio's child: it becomes the accessible name and the
    # whole name is clickable. PCI address and badges go on a second line to
    # keep the window narrow.
    name_label = Gtk.Label(label=name)
    # GtkLabel centers its text by default.
    name_label.set_xalign(0.0)
    radio.set_child(name_label)
    container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    container.append(radio)
    lower = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    if sub:
        sub_label = Gtk.Label(label=sub)
        sub_label.add_css_class("dim-label")
        lower.append(sub_label)
    for badge in badges:
        lower.append(_badge(badge))
    if lower.get_first_child() is not None:
        # Indent the detail line under the name, past the radio indicator.
        lower.set_margin_start(22)
        container.append(lower)
    return container


class GpuSwitcherApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        # Widgets are typed as their GTK classes (PyGObject has no stubs, so
        # they are Any) and stay None until the matching _build_* assigns them.
        self.window: Gtk.ApplicationWindow = None
        self.detection: detect.Detection | None = None
        self.selected_card: str = SELECTION_AUTO  # card name or a SELECTION_* sentinel
        self.unavailable_radio: Gtk.CheckButton = None  # configured-but-undetected row
        self.auto_radio: Gtk.CheckButton = None
        self.unavailable_pci: str | None = None
        self.radios_by_gpu: dict[str, Gtk.CheckButton] = {}
        self.rows_box: Gtk.Box = None
        self.banner: Gtk.Revealer = None
        self.banner_box: Gtk.Box = None
        self.banner_label: Gtk.Label = None
        self.apply_btn: Gtk.Button = None
        self.reset_btn: Gtk.Button = None
        self.reboot_btn: Gtk.Button = None
        self.logout_btn: Gtk.Button = None
        self.refresh_btn: Gtk.Button = None
        self.gpu_state: GpuState = GpuState.UNKNOWN
        self.outcome: Outcome = Outcome.idle  # explicit state of the last operation
        self._detect_generation: int = 0  # discards stale detection results
        self._detect_started_at: float | None = None  # when the snapshot was read
        self._confirm_open: int = 0  # confirmations awaiting an answer
        self._busy: bool = False  # an action or a detection is in flight
        self._pending_banner: tuple[str, BannerKind] | None = (
            None  # shown after a rebuild
        )

    def _build_loading(self) -> None:
        """Placeholder shown until the first detection finishes."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        label = Gtk.Label(label=i18n._("Detecting…"))
        label.add_css_class("dim-label")
        box.append(label)
        self.window.set_child(box)

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
        assert result is not None
        self.gpu_state = derive_state(result, self.outcome)
        status = self._build_state_status(result, self.gpu_state)
        if status is not None:
            box.append(status)
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
        """One radio row per GPU, a disabled row for a configured GPU that is
        not detected (if any), and the auto row.

        The auto row always exists, so Reset and the revert path never touch
        a missing widget. The selection mirrors the configured state
        (selection.mirror_configured_state).
        """
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.radios_by_gpu = {}
        first_radio = None
        for gpu in result.gpus:
            badges = decisions.gpu_badges(gpu)
            radio = Gtk.CheckButton(label="")
            radio.add_css_class("radio")
            if first_radio is not None:
                radio.set_group(first_radio)
            first_radio = radio
            card = gpu.card
            radio.connect("toggled", self._on_radio_toggled, card)
            self.radios_by_gpu[card] = radio
            sub = f"PCI {gpu.pci} \u00b7 {gpu.node}"
            self.rows_box.append(_gpu_row(radio, gpu.name, sub, badges))
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
                _gpu_row(
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
            _gpu_row(self.auto_radio, i18n._("System default (auto)"), "", [])
        )
        # Initial build: mirror the managed rule on disk (auto when nothing
        # is configured).
        self._select_radio(mirror_configured_state(result))

    def _select_radio(self, card: str) -> None:
        """Select the radio for a card name or a SELECTION_* sentinel."""
        self.selected_card = card
        if card == SELECTION_AUTO:
            self.auto_radio.set_active(True)
        elif card == SELECTION_UNAVAILABLE:
            self.unavailable_radio.set_active(True)
        else:
            self.radios_by_gpu[card].set_active(True)

    def _build_state_status(self, result, state: GpuState) -> Gtk.Box | None:
        """Icon + text row for the derived GPU state, or None when
        decisions.state_status has nothing to add."""
        status = decisions.state_status(state, result)
        if status is None:
            return None
        text, kind, icon_name = status
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if kind is BannerKind.INFO:
            row.add_css_class("dim-label")
        else:
            row.add_css_class(kind.value)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_valign(Gtk.Align.START)
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_xalign(0.0)
        label.set_hexpand(True)
        row.append(icon)
        row.append(label)
        return row

    def _build_banner(self, result) -> None:
        """Status banner: a Revealer with a Box and Label styled with Adwaita
        classes (Gtk.InfoBar is deprecated). Shows a warning up front for an
        undetected configured GPU or a conflicting rule."""
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
        banner = decisions.startup_banner(result)
        if banner is not None:
            self._banner_show(*banner)

    def _build_buttons(self) -> Gtk.Box:
        assert self.detection is not None  # the window is built only after detection
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
        # Reading the world again asks nothing of polkit, so it stays available
        # even where the privileged actions are disabled.
        self.refresh_btn = Gtk.Button(label=i18n._("Refresh"))
        self.refresh_btn.set_tooltip_text(
            i18n._("Re-read the GPUs, the rule on disk and the running adapter")
        )
        self.refresh_btn.connect("clicked", lambda _b: self._refresh())
        buttons.append(self.refresh_btn)
        # Outside a GNOME Wayland session no privileged action works
        # (DESIGN.md: GNOME only); the warning banner explains why.
        actions = actions_enabled(self.detection.gnome_wayland)
        self.apply_btn.set_sensitive(actions)
        self.reset_btn.set_sensitive(actions)
        self.reboot_btn.set_sensitive(actions)
        self.logout_btn.set_sensitive(actions and logout_enabled(False, self.outcome))
        return buttons

    def _build_verification_hint(self) -> Gtk.Expander:
        """Collapsible hint with the journalctl command that verifies the
        switch (from Mutter's multi-GPU docs); only commands are monospace."""
        hint = Gtk.Expander()
        hint.set_label(i18n._("How to verify the switch after restart"))
        # The app could not confirm the result itself: show the evidence.
        hint.set_expanded(self.gpu_state in (GpuState.UNKNOWN, GpuState.IGNORED))
        hint_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hint_box.set_margin_top(8)
        # The expander and GtkLabel center by default; align every line left.
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
        # Adwaita status classes for the three result kinds; info stays the
        # neutral dim-label. Button classes (suggested-action /
        # destructive-action) are not status styling and are not used.
        for css_class in ("success", "warning", "error", "dim-label"):
            self.banner_box.remove_css_class(css_class)
        if kind is BannerKind.INFO:
            self.banner_box.add_css_class("dim-label")
        else:
            self.banner_box.add_css_class(kind.value)
        self.banner_label.set_label(text)
        self.banner.set_reveal_child(True)

    def _set_busy(self, busy: bool) -> None:
        """Enable/disable the action buttons. Log out follows
        state.logout_enabled, so ending the busy state alone never enables it."""
        self._busy = busy
        assert self.detection is not None  # actions are only reachable after detection
        actions = actions_enabled(self.detection.gnome_wayland)
        for button in (self.apply_btn, self.reset_btn, self.reboot_btn):
            button.set_sensitive(actions and not busy)
        self.refresh_btn.set_sensitive(not busy)
        self.logout_btn.set_sensitive(actions and logout_enabled(busy, self.outcome))

    def _in_flight(self) -> bool:
        """Whether the window is in the middle of something the user is looking at:
        a confirmation awaiting an answer, or an action or detection running.
        """
        return self._busy or self._confirm_open > 0

    def _confirm(self, text: str, confirm_label: str, on_result) -> None:
        """Modal confirmation via Gtk.AlertDialog (GTK >= 4.10).

        choose()/choose_finish() is the async flow. The result is the clicked
        button's index: 0 = Cancel, 1 = confirm button; Escape and closing the
        window both map to Cancel. The dialog's window is disposed by
        choose_finish itself.
        """
        dialog = Gtk.AlertDialog()
        dialog.set_message(text)
        dialog.set_modal(True)
        dialog.set_buttons([i18n._("Cancel"), confirm_label])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)

        self._confirm_open += 1

        def on_choose(_d, task):
            self._confirm_open -= 1
            try:
                index = _d.choose_finish(task)
            except GLib.Error:
                index = -1
            on_result(index == 1)

        dialog.choose(self.window, None, on_choose)

    def _on_apply(self, _button) -> None:
        assert self.detection is not None  # Apply is only enabled after detection
        decision = decisions.apply_decision(self.detection, self.selected_card)
        if decision.kind is decisions.ApplyKind.CONFIRM:
            self._confirm(
                decision.confirm_text or "",
                i18n._("Apply"),
                lambda ok: self._do_apply(ok, decision.target),
            )
            return
        assert decision.banner is not None  # a refusal always says why
        self._banner_show(*decision.banner)
        if decision.kind is decisions.ApplyKind.RECHECK:
            # The card vanished since the row was built: re-check the world
            # instead of applying a stale selection.
            self._refresh()

    def _revert_selection(self) -> None:
        """Show the configured GPU again after a cancelled or failed action
        (nothing on disk changed)."""
        assert self.detection is not None
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

    def _on_apply_done(self, ok, message, refresh_ok, outcome_unknown) -> None:
        """Result of SetPrimary and SetAuto (same callback shape)."""
        if outcome_unknown:
            self._on_unknown_outcome(message)
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

    def _on_unknown_outcome(self, reason: str) -> None:
        """The call gave no answer that proves nothing changed: re-detect the
        rule on disk and report what is known instead of guessing.

        `reason` is what bus.py concluded about the helper (it ran out of
        time, it stopped answering); the outcome sentence is what the app
        adds on top of it.
        """
        self.outcome = Outcome.unknown
        self._set_busy(False)
        unknown = i18n._("The outcome is unknown; the current state was rechecked.")
        self._refresh(banner=(f"{reason} {unknown}", BannerKind.WARNING))

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
        """Re-detect and rebuild the window. `banner` is a (text, kind) pair
        shown after the rebuild, since the rebuild replaces the banner widget.
        """
        self._pending_banner = banner
        self._start_detection()

    def _start_detection(self) -> None:
        """Run detect.detect() on a worker thread and rebuild the UI on the
        main loop. A generation counter drops results superseded by a newer
        detection."""
        self._detect_generation += 1
        generation = self._detect_generation
        self._detect_started_at = time.monotonic()
        if self.banner is not None:
            # The rebuild replaces the buttons; keep the old ones inert so a
            # click cannot act on stale state in the meantime.
            self._set_busy(True)
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
                result = detect.detect()
            except Exception as error:
                # Never leave "Detecting…" forever: report the failure.
                # (Captured before the except-block exit clears the binding.)
                error_message = i18n._("Detection failed: %s.") % error

                def show_error():
                    if generation != self._detect_generation:
                        return
                    self._pending_banner = None  # not shown after a failure
                    if self.banner is None:
                        # First detection: there is no banner widget yet,
                        # so the error replaces the loading box.
                        label = Gtk.Label(label=error_message)
                        label.set_wrap(True)
                        label.add_css_class("error")
                        label.set_xalign(0.0)
                        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                        box.set_margin_top(24)
                        box.set_margin_bottom(24)
                        box.set_margin_start(24)
                        box.set_margin_end(24)
                        box.append(label)
                        self.window.set_child(box)
                        return
                    self._set_busy(False)
                    self._banner_show(error_message, BannerKind.ERROR)

                GLib.idle_add(show_error)
                return
            GLib.idle_add(apply, result)

        threading.Thread(target=worker, daemon=True).start()

    def do_activate(self) -> None:
        # A second start of the app must not build a second window:
        # present the existing one (single-instance via GApplication).
        if self.window is not None:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("gpu-switcher")
        self.window.connect("notify::is-active", self._on_window_active)
        # No fixed default size: the window sizes to its content and grows
        # only when the verification hint is expanded.
        self._build_loading()
        self.window.present()
        self._start_detection()

    def _on_window_active(self, window, _pspec) -> None:
        """Re-read the world when the user comes back to the window.

        `notify::is-active` fires in both directions, so only the transition into
        being the active window is acted on. The property is read by name: this is
        a plain GObject property of Gtk.Window and PyGObject gives no accessor for
        it in every version.
        """
        if not window.get_property("is-active"):
            return
        age = (
            None
            if self._detect_started_at is None
            else time.monotonic() - self._detect_started_at
        )
        if should_recheck(age, self._in_flight()):
            self._refresh()


def run(argv: list[str]) -> int:
    app = GpuSwitcherApp()
    return app.run(argv)


if __name__ == "__main__":
    import sys

    sys.exit(run(sys.argv))
