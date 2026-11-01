# gpu-switcher — Design notes

What the app does and why it is built this way. For usage and build
instructions see [README.md](README.md).

## Scope

**v1 = GNOME / Wayland (Mutter) only.** KDE Plasma and X11 are designed as
future extensions, not built.

## Technology

Python 3 + GTK 4 (PyGObject); no compilation step.

- One language for the GUI app and the privileged helper; they share the
  D-Bus protocol and the detection logic.
- The `.deb` is assembled from source files; the runtime dependencies
  (`python3`, `python3-gi`, `libgtk-4-1`, ...) are standard on Ubuntu/Debian.
- Rust + GTK4 was evaluated for v1: the architecture (D-Bus protocol, helper
  contract) is independent of the UI technology, so it remains the sensible
  choice later if a standalone static binary is ever wanted.

i18n: all strings go through gettext — `po/gpu-switcher-{en,ru}.po` compiled
with the pure-Python `tools/po2mo.py` (no gettext tools needed) and installed
to `/usr/share/gpu-switcher/locale/{lang}/LC_MESSAGES/`. Adding a language =
adding one `.po` file.

## How activation works (why a reboot is not strictly required)

Mutter reads the udev tag `mutter-device-preferred-primary` from the
persistent udev database when the DRM backend initializes at **session
start**; it does not re-read tags mid-session. Consequences:

- After the rule file changes, the tag in the database is stale until udev
  re-runs its rules:
  - **reboot** — udev re-evaluates everything at boot; always works, or
  - `udevadm control --reload-rules` + scoped
    `udevadm trigger --subsystem-match=drm` — udev re-emits the DRM events and
    the database gets the new tag; a **logout/login** is then enough
    (gnome-shell restarts and reads the fresh tag).
- The app therefore offers both paths: **Reboot** (default, safe) and
  **Log out** (fast path; enabled only when the refresh ran successfully).
  The refresh runs inside Apply / Reset under the same polkit authorization,
  so the fast path needs no extra prompt.
- Mutter's own docs phrase it the same way: "Reboot or manual triggering of
  udev rules to make it take effect may be required."

## Architecture

```
┌──────────────────────────────┐        ┌──────────────────────────────────────┐
│  gpu-switcher (GTK4, user)   │        │  helper (root, D-Bus-activated,      │
│                              │  D-Bus │  on demand, idle-exit ~60 s)          │
│  • enumerate GPUs (/sys)     │◄──────►│  • polkit CheckAuthorization         │
│  • journal → running primary │        │  • write/remove udev rule (+ backup) │
│  • udevadm → configured      │        │  • reload-rules (+ scoped trigger)   │
│  • login1 → reboot/logout    │        │  • refuses if conflicts found        │
└──────────────────────────────┘        └──────────────────────────────────────┘
```

### D-Bus interface (system bus)

- Service `com.github.wl04.gpu_switcher.Helper` (D-Bus names cannot contain
  `-`), object `/com/github/wl04/gpu_switcher/Helper`.
- Methods (all async from the app's point of view):
  - `Status(locale: s) → (rule_on_disk: s, message: s)` — read-only view of
    the managed rule.
  - `SetPrimary(pci: s, locale: s) → (ok: b, message: s, backup_path: s,
    refresh_ok: b)` — writes the rule (or confirms it is already in place),
    then runs the udev refresh under the same authorization.
  - `SetAuto(locale: s) → (ok: b, message: s, refresh_ok: b)` — removes only
    the managed rule, then runs the same refresh.
- The refresh lives inside SetPrimary/SetAuto: one polkit prompt covers the
  rule change and its activation; a failed refresh is reported per call
  (`refresh_ok` is false) and leaves reboot as the fallback.
- The helper quits ~60 s after the last call; the D-Bus broker re-activates a
  fresh process from the on-disk code on the next call, so a package upgrade
  cannot leave a stale helper serving old method signatures.
- `locale` is passed explicitly: a root-activated helper has **no user locale
  in its environment**, so the localized polkit dialog message must come
  from the caller.

### Polkit

- Custom action `com.github.wl04.gpu_switcher.set-primary`, `auth_admin`,
  localized `message`; the policy file is installed to
  `/usr/share/polkit-1/actions/gpu-switcher.policy`.
- The privileged mechanism is a **D-Bus-activated system-bus helper**:
  dbus-daemon starts it as root on demand (no setuid, no resident root daemon,
  no pkexec). The helper checks authorization itself over raw D-Bus
  (`org.freedesktop.PolicyKit1.Authority.CheckAuthorization`); the GUI app
  never talks to polkit directly. Raw D-Bus is used because there is no Python
  Polkit typelib on the target systems — zero extra dependencies.
- Reboot/logout use login1's standard actions
  (`org.freedesktop.login1.reboot`, `Session.Logout`) — no custom policy for
  those.

### Managed rule file

The app manages the same file the reference script `mutter-primary-gpu.sh`
writes — `/etc/udev/rules.d/61-mutter-primary-gpu.rules` — with the same rule
line (`KERNELS=="<PCI>"`, vendor/device, the
`mutter-device-preferred-primary` tag), the same backup scheme
(`.bak.<UTC-timestamp>.<random-suffix>`), and the same conflict detection (any
*other* rule setting the tag blocks the switch). The script and the app
therefore coexist without fighting over state; `udevadm verify` runs when
available.

### Detection (unprivileged, app side)

1. **Enumerate**: `/sys/class/drm/card*` → `device` symlink → PCI address;
   keep only class `0x030000` with a bound driver.
2. **Name**: `lspci -nn`; fallback: built-in vendor:device table; last
   resort: `PCI v<vendor> d<device>`.
3. **Running primary**: latest `journalctl -b -q /usr/bin/gnome-shell` line
   matching `GPU /dev/dri/cardN selected (as )?primary` — mutter logs
   `GPU /dev/dri/cardN selected primary given udev rule` in rule mode and
   `Boot VGA GPU /dev/dri/cardN selected as primary` in auto mode.
4. **Configured preference**: the managed rule's `KERNELS` + each card's udev
   `TAGS`, plus the conflict scan.
5. **Session guard**: if the session is not GNOME/Wayland
   (`XDG_SESSION_TYPE=wayland` and `gnome-shell` present) — warning banner,
   no changes.

## UI

One window (Adwaita, no custom CSS):

- GPU rows: `lspci` name, `PCI <addr> · /dev/dri/cardN`, badges **"In use
  now"** (running primary) and **"Preferred (configured)"** (managed rule).
- Radio selection: each GPU + **"System default (auto)"**.
- **Apply** — confirmation dialog → polkit → `SetPrimary` (+ backup notice).
- **Reset to auto** — `SetAuto` (+ note that auto-selection applies after
  restart).
- **Reboot now** — confirmation → login1 `Manager.Reboot`.
- **Log out** — fast path, session terminate only; enabled only after an
  Apply/Reset whose udev refresh succeeded (no polkit prompt of its own).
- Contextual status banners: applied / denied / conflict.
- Collapsible verification hint: the exact `journalctl` command to confirm
  after restart.

Interaction rules: privileged calls are async (spinner; the UI never blocks);
Apply/Reset are the only state-changing buttons; every destructive step has a
confirmation dialog.

## File layout

```
gpu-switcher/
├── data/
│   ├── gpu-switcher.desktop
│   ├── polkit/gpu-switcher.policy
│   ├── dbus/com.github.wl04.gpu_switcher.Helper.service
│   │   # D-Bus activation (system bus, user=root; file name must be
│   │   # exactly <service name>.service)
│   ├── dbus/system.d/com.github.wl04.gpu_switcher.Helper.conf
│   │   # system-bus policy: root owns the helper name, any user may call it
│   ├── icons/          # app icon + symbolic
│   └── locale/         # compiled .mo (built from po/)
├── debian/
│   ├── control  compat  source/format  rules
│   └── changelog  copyright  postrm
├── man/gpu-switcher.1
├── po/                 # gpu-switcher-en.po  gpu-switcher-ru.po
├── src/
│   ├── wrapper/gpu-switcher           # installed as /usr/bin/gpu-switcher
│   ├── udev_rules.py                  # shared udev-rule constants and the
│   │                                  #   conflict scan (app + helper; stdlib
│   │                                  #   only, no gi import)
│   ├── gpu_switcher/
│   │   ├── __init__.py                # package entry point
│   │   ├── main.py                    # GTK4 UI
│   │   ├── detect.py                  # GPU enumeration, names, running/configured
│   │   │                               #   primary, conflicts, session guard
│   │   ├── selection.py               # pure radio-selection logic
│   │   ├── bus.py                     # D-Bus clients: helper + login1
│   │   └── i18n.py                    # gettext setup
│   └── helper/gpu_switcher_helper.py  # root D-Bus service + raw-D-Bus polkit
├── tests/                             # unit tests (stdlib unittest)
│   ├── test_detect.py
│   ├── test_udev_rules.py
│   ├── test_helper.py
│   ├── test_selection.py
│   ├── test_po2mo.py
│   └── test_i18n.py
├── tools/
│   ├── build-deb.sh                   # .deb builder (plain dpkg-deb)
│   └── po2mo.py                       # pure-Python .po → .mo compiler
├── docs/screenshot.png
├── Makefile
├── PLAN.md                           # this file
├── README.md
└── LICENSE
```

## Packaging

- Package `gpu-switcher` (`Architecture: all`); GApplication/desktop ID
  `com.github.wl04.gpu-switcher`; the D-Bus service name uses `gpu_switcher`
  (hyphens are not allowed in D-Bus names).
- `tools/build-deb.sh` (plain `dpkg-deb`, no debhelper required) is the
  canonical build; `debian/` (control, rules, compat, changelog, copyright,
  postrm) is kept for a `dpkg-buildpackage -us -uc` flow once debhelper is
  available. Both paths compile the catalogs with `tools/po2mo.py` and
  install the same file list; the `.deb` version is read from the `Version:`
  line in `debian/control` (single source of truth).
- Installed docs: `changelog.Debian.gz` (gzip-compressed per Debian policy),
  plain `copyright`, gzipped man page.
- `postrm` leaves the managed rule and its `.bak.*` backups in place on
  `dpkg -r` — they are the user's primary-GPU choice, not package data.
- Quality gates: `lintian` clean on the built `.deb`, `dpkg-deb --info` /
  `--contents` sanity, install/remove tested (see README).
