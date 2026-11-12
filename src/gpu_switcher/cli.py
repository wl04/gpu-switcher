"""Read-only command-line front-end for the GPU situation.

`gpu-switcher` with no command opens the window. Given a command it answers, in a
terminal, the two questions that do not need a polkit dialog or a display:

    list     which display GPUs this machine has, and what marks each of them
    status   what the rule on disk pins, which tag udev applies, what runs now,
             and what that adds up to
    version  the version of this package

It reads the same detection snapshot and shares the same decisions as the window,
so the two front-ends cannot disagree about what the machine is doing. It changes
nothing: the verbs that write are only in the window, where an operation is
announced, confirmed and authorized.

Exit codes: 0 when the answer was given, 2 when the command line itself was wrong
(usage on stderr), 1 when nothing could be read.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO

from . import __version__, decisions, detect, gpu_state, i18n
from .core.udev import MANAGED_RULE
from .state import Outcome

COMMANDS = ("list", "status", "version")
HELP_COMMAND = "help"
USAGE = "usage: gpu-switcher [COMMAND]"
EXIT_OK = 0
EXIT_NOTHING_READ = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Request:
    """What the command line asked for.

    `command` is one of COMMANDS, HELP_COMMAND, or None when no command was given
    (the window) or the arguments belong to the window. `error` is a sentence
    naming what was wrong, set only when the command line cannot be honoured.
    """

    command: str | None
    error: str | None


def help_text() -> str:
    """The help body: usage, the commands, and what no command does."""
    return "\n".join(
        (
            USAGE,
            "",
            "  list     the display GPUs this machine has, and what marks each",
            "  status   the rule on disk, the tag udev applies, the running",
            "           adapter and what that adds up to",
            "  version  the version of this package",
            "",
            "With no COMMAND, opens the window. A command changes nothing; the",
            "verbs that write a rule are only in the window.",
        )
    )


def parse_args(argv: Sequence[str]) -> Request:
    """Read the command out of a command line without running anything.

    Arguments that start with "-" are left to the window: Gtk.Application has its
    own options (--gapplication-app-id and friends) and this front-end must not
    swallow them.
    """
    words = list(argv)[1:]
    if not words:
        return Request(None, None)
    name = words[0]
    if name in ("-h", "--help"):
        name = HELP_COMMAND
    if name not in COMMANDS and name not in (HELP_COMMAND,):
        if name.startswith("-"):
            return Request(None, None)
        return Request(None, f"gpu-switcher: unknown command {name!r}")
    if len(words) > 1:
        return Request(
            None,
            f"gpu-switcher: {name!r} takes no arguments, got {words[1]!r}",
        )
    return Request(name, None)


def gpu_rows(detection: detect.Detection) -> list[str]:
    """One row per detected GPU: card, PCI, model, then what marks it.

    The marks are the window's own badges, so a terminal and the window describe a
    card the same way. A card whose sysfs could not be read carries no display
    badge at all: "no display connected" is a fact the kernel reported, never an
    absence of an answer.
    """
    if not detection.gpus:
        return [i18n._("No GPUs detected.")]
    card_width = max(len(g.card) for g in detection.gpus)
    pci_width = max(len(g.pci) for g in detection.gpus)
    rows: list[str] = []
    for gpu in detection.gpus:
        row = f"{gpu.card:<{card_width}}  {gpu.pci:<{pci_width}}  {gpu.name}"
        badges = decisions.gpu_badges(gpu)
        if badges:
            row = f"{row}  {'; '.join(badges)}"
        rows.append(row)
    return rows


def _rule_line(detection: detect.Detection) -> str:
    """What the managed rule on disk says, or what stopped it from being read."""
    _ = i18n._
    if detection.rule_state is detect.RuleState.ABSENT:
        return _("No managed rule (auto selection).")
    if detection.rule_state is detect.RuleState.UNREADABLE:
        return (
            _(
                "Could not read the udev rule %s. It might set the preferred GPU; "
                "make it readable or remove it."
            )
            % MANAGED_RULE
        )
    if detection.rule_state is detect.RuleState.UNRECOGNIZED:
        return _(
            "The managed udev rule could not be interpreted, so the configured "
            "GPU is unknown (this is not automatic selection). Use 'Reset to "
            "auto' or Apply to rewrite it."
        )
    return _("Managed rule sets PCI %s") % str(detection.preferred_pci)


def _tag_line(detection: detect.Detection) -> str | None:
    """Whether udev carries the preferred-GPU tag on the configured card."""
    _ = i18n._
    if detection.tag_applied is None or detection.preferred_pci is None:
        return None
    card = next(
        (g.card for g in detection.gpus if g.pci == detection.preferred_pci),
        detection.preferred_pci,
    )
    if detection.tag_applied:
        return _("udev applies the preferred-GPU tag on %s.") % card
    return _("udev does not apply the preferred-GPU tag on %s.") % card


def _running_line(detection: detect.Detection) -> str:
    """The adapter the desktop runs on now, or why that is not known."""
    _ = i18n._
    if detection.running_card is not None:
        gpu = next(
            (g for g in detection.gpus if g.card == detection.running_card), None
        )
        if gpu is None:
            return _("Running primary: %s") % detection.running_card
        return _("Running primary: %s") % f"{gpu.pci} ({gpu.card})"
    if detection.running_card_error is not None:
        return (
            _("Running primary: could not read the session journal (%s).")
            % detection.running_card_error
        )
    return _("Running primary: unknown (verify in the journal after restart).")


def status_lines(detection: detect.Detection) -> list[str]:
    """The situation as sentences: disk, udev, what runs, what it means.

    The derived state is read with Outcome.idle - the startup value of a session
    that ran no operation - because a terminal read has no Apply/Reset behind it
    and must not claim one. Its sentence is left out when it would only repeat the
    running adapter line above, which is what `decisions.state_status` does in the
    window for the same reason.
    """
    _ = i18n._
    lines: list[str] = []
    if detection.conflicts:
        lines.append(
            _(
                "Another udev rule sets the preferred GPU: %s. "
                "Resolve it manually first."
            )
            % ", ".join(detection.conflicts)
        )
    lines.append(_rule_line(detection))
    if not detection.gnome_wayland:
        # The rule is on disk whatever the desktop is, but which adapter runs and
        # what the rule adds up to are GNOME questions.
        lines.append(
            _(
                "Not running in a GNOME Wayland session - this build only "
                "supports GNOME."
            )
        )
        return lines
    tag = _tag_line(detection)
    if tag is not None:
        lines.append(tag)
    lines.append(_running_line(detection))
    state = gpu_state.derive_state(detection, Outcome.idle)
    meaning = gpu_state.describe(state, detection)
    if meaning and state is not gpu_state.GpuState.UNKNOWN:
        lines.append(meaning)
    return lines


def _launch_window(argv: Sequence[str]) -> int:
    """Open the window, importing GTK only when the window was asked for."""
    from . import main

    return main.run(list(argv))


def run(
    argv: Sequence[str],
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Answer the command line and return the exit code.

    Writes the answer to `stdout`, usage and read failures to `stderr`, and never
    changes anything. `detect.detect()` may fail on a machine without the tools
    installed; that is reported as a sentence rather than a traceback.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    _ = i18n._

    request = parse_args(argv)
    if request.error is not None:
        err.write(f"{request.error}\n{help_text()}\n")
        return EXIT_USAGE
    if request.command is None:
        return _launch_window(argv)
    if request.command == HELP_COMMAND:
        out.write(f"{help_text()}\n")
        return EXIT_OK
    if request.command == "version":
        out.write(f"gpu-switcher {__version__}\n")
        return EXIT_OK

    try:
        detection = detect.detect()
    except Exception as exc:  # a missing lspci or sysfs is an answer, not a crash
        err.write(_("Detection failed: %s.") % exc + "\n")
        return EXIT_NOTHING_READ

    lines = (
        gpu_rows(detection) if request.command == "list" else status_lines(detection)
    )
    for line in lines:
        out.write(f"{line}\n")
    return EXIT_OK
