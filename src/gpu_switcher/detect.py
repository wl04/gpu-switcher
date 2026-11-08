"""Unprivileged primary-GPU detection.

Read-only: /sys, /proc, `lspci`, the gnome-shell journal and the managed
rule file; nothing here needs privileges. Parsing is pure (text in, data out)
so it is testable without the real tools; the thin wrappers do the I/O.

The running primary is read from the journal of the single gnome-shell pid
owned by our uid. With zero or several such pids the attribution is
ambiguous and the running primary is unknown.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import re
import shutil
import subprocess

from gpu_switcher.core.udev import (
    MANAGED_RULE,
    OnUnreadable,
    is_display_gpu,
    join_continuations,
    read_text,
    rule_conflicts,
    sets_preferred_gpu,
)

JOURNAL_UNIT = "/usr/bin/gnome-shell"


class RuleState(enum.Enum):
    """Condition of the managed rule file.

    ABSENT: no file, so automatic selection. VALID: an active supported rule
    pins a PCI. UNRECOGNIZED: the file exists but holds no active supported
    rule (commented out, malformed, edited by hand). UNREADABLE: it could not
    be read, so the configured state is unknown.
    """

    ABSENT = "absent"
    VALID = "valid"
    UNRECOGNIZED = "unrecognized"
    UNREADABLE = "unreadable"


@dataclasses.dataclass
class Gpu:
    card: str  # "card2"
    node: str  # "/dev/dri/card2"
    pci: str  # "0000:12:00.0"
    vendor: str  # "1002"
    device: str  # "13c0"
    name: str  # human-readable name (lspci or fallback)
    in_use: bool  # running primary of this boot (journal)
    preferred: bool  # configured preferred (managed rule on disk)


@dataclasses.dataclass
class Detection:
    gpus: list[Gpu]
    running_card: str | None  # card picked by gnome-shell this boot
    running_card_error: str | None  # set when the journal could not be read
    preferred_pci: str | None  # PCI from the managed rule on disk
    conflicts: list[str]  # other udev rules setting the tag
    gnome_wayland: bool
    rule_state: RuleState = RuleState.ABSENT  # managed rule on disk


# Display-controller PCI device classes: 0x0300 (VGA) and 0x0302 (3D).
_LSPCI_DISPLAY_CLASSES = ("0300", "0302")
_LSPCI_MM_ADDR_RE = re.compile(r'^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9])\s+"')
_LSPCI_CLASS_CODE_RE = re.compile(r"\[([0-9a-fA-F]{4})\]\s*$")
_LSPCI_ID_TAIL_RE = re.compile(r"\s*\[[0-9a-fA-F]{4}\]$")


def parse_lspci_mm(output: str) -> dict[str, str]:
    """Map PCI address -> display name from `lspci -Dmmnn` machine output.

    Each line is `ADDR "CLASS [code]" "VENDOR [id]" "DEVICE [id]" ...`. Only
    display-controller classes (0x0300 VGA, 0x0302 3D) are kept; the trailing
    `[id]` is stripped from the vendor and device so the name is
    "<vendor> <device>".
    """
    names: dict[str, str] = {}
    for line in output.splitlines():
        addr_match = _LSPCI_MM_ADDR_RE.match(line)
        if not addr_match:
            continue
        addr = addr_match.group(1)
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) < 3:
            continue
        class_field, vendor, device = fields[0], fields[1], fields[2]
        code_match = _LSPCI_CLASS_CODE_RE.search(class_field)
        if not code_match or code_match.group(1) not in _LSPCI_DISPLAY_CLASSES:
            continue
        vendor = _LSPCI_ID_TAIL_RE.sub("", vendor)
        device = _LSPCI_ID_TAIL_RE.sub("", device)
        name = " ".join(part for part in (vendor, device) if part).strip()
        if name:
            names[addr] = name
    return names


def lspci_names() -> dict[str, str]:
    """Map PCI address -> display name via `lspci -Dmmnn`; {} when lspci is
    missing or fails (names then fall back to vendor:device)."""
    try:
        out = subprocess.run(
            ["lspci", "-Dmmnn"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    return parse_lspci_mm(out.stdout)


def fallback_name(vendor: str, device: str) -> str:
    # Without lspci only the PCI vendor/device ids are known.
    return f"PCI v0x{vendor} d0x{device}"


# gnome-shell logs the primary selection once per session start, in two
# forms:
#   rule mode:  GPU /dev/dri/cardN selected primary given udev rule
#   auto mode:  Boot VGA GPU /dev/dri/cardN selected as primary
PRIMARY_LINE_RE = re.compile(r"GPU /dev/dri/(card[0-9]+) selected (?:as )?primary")
# journalctl --grep filter for the selection lines (matches both the rule-mode
# "selected primary" and auto-mode "selected as primary" forms).
PRIMARY_GREP = "selected (as )?primary"


def primary_card_from_line(line: str) -> str | None:
    """Card name ('cardN') from a gnome-shell primary-selection journal line."""
    m = PRIMARY_LINE_RE.search(line)
    return m.group(1) if m else None


def last_primary_card(lines: list[str]) -> str | None:
    """Card from the last primary-selection journal line, or None."""
    card = None
    for line in lines:
        card = primary_card_from_line(line) or card
    return card


def gnome_shell_pids_for_uid(
    tree: dict[int, tuple[int, str, int | None]], uid: int
) -> list[int]:
    """gnome-shell pids owned by `uid`, sorted and deduplicated.

    Scopes the running-primary attribution to our user without logind:
    other users'/sessions' shells never count. Pids whose uid is unknown
    (unreadable status) never match.
    """
    found = set()
    for pid, (_ppid, comm, pid_uid) in tree.items():
        if comm == "gnome-shell" and pid_uid == uid:
            found.add(pid)
    return sorted(found)


def _uid_of(proc_root: str, pid: int) -> int | None:
    """The uid owning `pid`, from /proc/<pid>/status `Uid:`, or None when
    unreadable (a process vanishing mid-scan, or another user's process we
    cannot read)."""
    status = read_text(f"{proc_root}/{pid}/status")
    for line in status.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
            return None
    return None


def _process_tree(
    proc_root: str = "/proc",
) -> dict[int, tuple[int, str, int | None]]:
    """{pid: (ppid, comm, uid)} for the process table under `proc_root`.

    /proc/<pid>/stat is `pid (comm) state ppid ...`; comm may contain
    spaces and parentheses, so the line is split at the last ')'. The uid
    comes from /proc/<pid>/status and is None when unreadable. Unreadable
    entries are skipped (processes vanish mid-scan normally); non-UTF-8
    comm bytes are replaced, not fatal (see read_text).
    """
    tree: dict[int, tuple[int, str, int | None]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return tree
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = read_text(f"{proc_root}/{entry}/stat")
        if not stat:
            continue
        try:
            head, rest = stat.rsplit(")", 1)
            comm = head.split("(", 1)[1]
            ppid = int(rest.split()[1])
        except (ValueError, IndexError):
            continue
        pid = int(entry)
        tree[pid] = (ppid, comm, _uid_of(proc_root, pid))
    return tree


def running_primary() -> tuple[str | None, str | None]:
    """Card picked by gnome-shell in our session (e.g. 'card2'), and the
    error text when the journal could not be read (None on success).

    The journal is scoped to the single gnome-shell pid owned by our uid;
    other users'/sessions' shells never count. With zero or multiple
    in-scope pids the attribution is ambiguous and (None, None) is
    returned — the existing "unknown" note covers it in the UI.
    """
    tree = _process_tree()
    pids = gnome_shell_pids_for_uid(tree, os.getuid())
    if len(pids) != 1:
        return None, None
    pid = pids[0]
    try:
        out = subprocess.run(
            [
                "journalctl",
                "-b",
                f"_PID={pid}",
                JOURNAL_UNIT,
                "-o",
                "cat",
                "--grep",
                PRIMARY_GREP,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"journalctl could not be run ({error})"
    if out.returncode != 0:
        return None, f"journalctl exited with code {out.returncode}"
    return last_primary_card(out.stdout.splitlines()), None


_KERNELS_RE = re.compile(r'(?<!\w)KERNELS\s*==\s*"([^"]+)"')


def managed_rule_pci_from_text(text: str) -> str | None:
    """PCI address pinned by an active preferred-GPU rule in `text`, or None.

    Comment lines and lines that do not assign the preferred-GPU tag are
    ignored, so a commented-out or unrelated KERNELS== never counts.
    """
    for line in join_continuations(text):
        if line.lstrip().startswith("#") or not sets_preferred_gpu(line):
            continue
        match = _KERNELS_RE.search(line)
        if match:
            return match.group(1)
    return None


def managed_rule_status(path: str = MANAGED_RULE) -> tuple[RuleState, str | None]:
    """Condition of the managed rule file and the PCI it pins (VALID only)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return RuleState.ABSENT, None
    except OSError:
        return RuleState.UNREADABLE, None
    pci = managed_rule_pci_from_text(text)
    if pci is None:
        return RuleState.UNRECOGNIZED, None
    return RuleState.VALID, pci


def is_gnome_wayland(
    session_type: str, xdg_current_desktop: str, gnome_shell_present: bool
) -> bool:
    """True when the session is Wayland and the desktop is GNOME.

    The desktop is XDG_CURRENT_DESKTOP normalized (last colon-separated
    component, casefolded, == "gnome"); when it is unset, gnome-shell
    presence is the fallback (a GNOME Wayland session always runs
    gnome-shell, and its absence is the only signal without the
    variable).
    """
    if session_type.lower() != "wayland":
        return False
    if not xdg_current_desktop:
        return gnome_shell_present
    return xdg_current_desktop.split(":")[-1].casefold() == "gnome"


def gnome_wayland_session() -> bool:
    """Whether the current desktop session is GNOME on Wayland."""
    return is_gnome_wayland(
        os.environ.get("XDG_SESSION_TYPE", ""),
        os.environ.get("XDG_CURRENT_DESKTOP", ""),
        shutil.which("gnome-shell") is not None,
    )


def detect() -> Detection:
    """Full detection snapshot used by the UI."""
    gpus = []
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        entries = []
    names = lspci_names()
    for entry in entries:
        # Only real cards ("cardN"); connectors are "cardN-CONN".
        if not re.fullmatch(r"card[0-9]+", entry):
            continue
        pci = os.path.basename(os.path.realpath(f"/sys/class/drm/{entry}/device"))
        # The shared display-GPU contract (display class, bound driver,
        # exactly one card per PCI, /dev/dri node).
        if is_display_gpu(pci) != entry:
            continue
        pdir = f"/sys/bus/pci/devices/{pci}"
        vendor = read_text(f"{pdir}/vendor").removeprefix("0x").lower()
        device = read_text(f"{pdir}/device").removeprefix("0x").lower()
        gpus.append(
            Gpu(
                card=entry,
                node=f"/dev/dri/{entry}",
                pci=pci,
                vendor=vendor,
                device=device,
                name=names.get(pci) or fallback_name(vendor, device),
                in_use=False,
                preferred=False,
            )
        )
    running_card, running_card_error = running_primary()
    rule_state, preferred = managed_rule_status()
    for g in gpus:
        g.in_use = g.card == running_card
        g.preferred = g.pci == preferred
    return Detection(
        gpus=gpus,
        running_card=running_card,
        running_card_error=running_card_error,
        preferred_pci=preferred,
        conflicts=rule_conflicts(on_unreadable=OnUnreadable.SKIP),
        gnome_wayland=gnome_wayland_session(),
        rule_state=rule_state,
    )
