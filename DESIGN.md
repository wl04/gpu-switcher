# gpu-switcher — Design notes

What the app does and why it is built this way. For usage see
[README.md](README.md); for build and release instructions see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Scope

**GNOME / Wayland (Mutter) only.** KDE Plasma and X11 are not supported.

## Technology

Python 3 + GTK 4 (PyGObject); no compilation step.

- One language for the GUI app and the privileged helper; they share the
  D-Bus protocol and the detection logic.
- The `.deb` is assembled from source files; the runtime dependencies
  (`python3`, `python3-gi`, `libgtk-4-1`, ...) are standard on Ubuntu/Debian.
- The architecture (D-Bus protocol, helper contract) is independent of the
  UI technology.

i18n: all strings go through gettext — `po/gpu-switcher-{en,ru}.po` compiled
with `msgfmt` (build-time `gettext` dependency) and installed to
`/usr/share/gpu-switcher/locale/{lang}/LC_MESSAGES/`. Adding a language =
adding one `.po` file.

## How activation works (why a reboot is not strictly required)

Mutter reads the udev tag `mutter-device-preferred-primary` from the card's
**current tags** when the DRM backend initializes at **session start**; it
does not re-read tags mid-session. udev keeps two tag lists per device: the
cumulative `TAGS` (kept until the device's udev entry is replaced — in
practice, a reboot) and `CURRENT_TAGS` (recomputed on every uevent).
Consequences:

- After the rule file changes, the current tags are stale until udev
  re-runs its rules:
  - **reboot** — udev re-evaluates everything at boot; always works, or
  - `udevadm control --reload-rules` + scoped
    `udevadm trigger --subsystem-match=drm --settle` — udev re-emits the DRM
    events and every card's current tags are recomputed under the new rule
    (only the configured card carries the tag); a **logout/login** is then
    enough (gnome-shell restarts and reads the fresh current tags).
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
│  • managed rule → configured │        │  • reload-rules (+ scoped trigger)   │
│  • login1 → reboot, session  │
│    manager → logout          │        │  • refuses if conflicts found        │
└──────────────────────────────┘        └──────────────────────────────────────┘
```

### D-Bus interface (system bus)

- Service `com.github.wl04.gpu_switcher.Helper` (D-Bus names cannot contain
  `-`), object `/com/github/wl04/gpu_switcher/Helper`.
- Methods (all async from the app's point of view):
  - `SetPrimary(pci: s, locale: s) → (ok: b, message: s, backup_path: s,
    refresh_ok: b)` — writes the rule (or confirms it is already in place),
    then runs the udev refresh under the same authorization.
  - `SetAuto(locale: s) → (ok: b, message: s, refresh_ok: b)` — removes only
    the managed rule, then runs the same refresh.
- The refresh lives inside SetPrimary/SetAuto: one polkit prompt covers the
  rule change and its activation; a failed refresh is reported per call
  (`refresh_ok` is false) and leaves reboot as the fallback.
- The helper quits ~60 s after the last call (never while an operation is in
  flight: the interactive polkit dialog alone may outlast that); the D-Bus
  broker re-activates a fresh process from the on-disk code on the next call,
  so a package upgrade cannot leave a stale helper serving old method
  signatures.
- At most one mutating call runs at a time. A second one gets the
  `com.github.wl04.gpu_switcher.Error.Busy` D-Bus error; an unexpected
  failure releases the lock and is reported as
  `com.github.wl04.gpu_switcher.Error.Internal` (details go to the journal).
  Expected, user-facing failures are returned in the reply tuple
  (`ok = false` plus a localized `message`).
- All long work is asynchronous (polkit via `connection.call`, `udevadm` via
  `Gio.Subprocess` with a per-step timeout; a timed-out child is killed), so
  the helper's main loop stays responsive. The client's call timeout is
  derived from the helper's worst case (`core/udev.py`).
- `locale` is passed explicitly: a root-activated helper has **no user locale
  in its environment**, so the localized polkit dialog message must come
  from the caller.

### Polkit

- Custom action `com.github.wl04.gpu_switcher.set-primary`, `auth_admin`,
  localized `message`; the policy file is installed to
  `/usr/share/polkit-1/actions/gpu-switcher.policy`.
- The privileged mechanism is a **D-Bus-activated system-bus helper**:
  the broker starts it on demand as the hardened systemd unit
  `gpu-switcher-helper.service` (root; `ProtectSystem=strict`, write access
  limited to `/etc/udev/rules.d`; no setuid, no resident root daemon, no
  pkexec). The helper checks authorization itself over raw D-Bus
  (`org.freedesktop.PolicyKit1.Authority.CheckAuthorization`); the GUI app
  never talks to polkit directly. Raw D-Bus is used because there is no Python
  Polkit typelib on the target systems — zero extra dependencies.
- Reboot uses login1's standard action (`org.freedesktop.login1.reboot`);
  Log out goes through the GNOME session manager on the session bus — no
  custom policy for either.

### Managed rule file

The app manages the same file the reference script `mutter-primary-gpu.sh`
writes — `/etc/udev/rules.d/61-mutter-primary-gpu.rules` — with the same rule
line (`KERNELS=="<PCI>"`, vendor/device, the
`mutter-device-preferred-primary` tag), the same backup scheme
(`.bak.<UTC-timestamp>.<random-suffix>`), and the same conflict detection (any
*other* rule that assigns the tag blocks the switch; the scan inspects every
assignment on a line and joins `\` continuations). The script and the app
therefore coexist without fighting over state; `udevadm verify` runs when
available.

### Detection (unprivileged, app side)

1. **Enumerate**: `/sys/class/drm/card*` → `device` symlink → PCI address;
   keep only display-controller classes (`0x030000` VGA, `0x030200` 3D
   controller) with a bound driver.
2. **Name**: `lspci -Dmmnn`; fallback: `PCI v<vendor> d<device>` (no
   hard-coded GPU table).
3. **Running primary**: latest `journalctl -b -q _PID=<pid>
   /usr/bin/gnome-shell` line matching `GPU /dev/dri/cardN selected (as
   )?primary` — mutter logs `GPU /dev/dri/cardN selected primary given udev
   rule` in rule mode and `Boot VGA GPU /dev/dri/cardN selected as primary`
   in auto mode. The pid is scoped to the single gnome-shell pid owned by
   our uid (read from `/proc/<pid>/status`), with no logind round trip;
   other users'/sessions' shells never count. Zero or multiple in-scope
   pids leave the running primary unknown.
4. **Configured preference**: the managed rule's `KERNELS` value, plus the
   conflict scan (the root helper repeats it authoritatively).
5. **Session guard**: if the session is not GNOME/Wayland
   (`XDG_SESSION_TYPE=wayland` and `XDG_CURRENT_DESKTOP` is GNOME, or
   `gnome-shell` present when it is unset) — warning banner, privileged
   actions disabled.

## UI

One window (Adwaita, no custom CSS):

- GPU rows: `lspci` name, `PCI <addr> · /dev/dri/cardN`, badges **"In use
  now"** (running primary) and **"Preferred (configured)"** (managed rule).
- Radio selection: each GPU + **"System default (auto)"**.
- **Apply** — confirmation dialog → polkit → `SetPrimary` (+ backup notice).
- **Reset to auto** — `SetAuto` (+ note that auto-selection applies after
  restart).
- **Reboot now** — two confirmations → login1 `Manager.Reboot`.
- **Log out** — fast path via the GNOME session manager `Logout` on the
  session bus; enabled only after an Apply/Reset whose udev refresh
  succeeded (no polkit prompt of its own).
- Contextual status banners: applied / denied / conflict / unavailable
  configured GPU / unknown outcome after a timeout.
- Collapsible verification hint: the exact `journalctl` command to confirm
  after restart.

Interaction rules: privileged calls are async (the buttons are disabled while
an operation or a re-detection runs; the UI never blocks, detection runs on a
worker thread); Apply/Reset change the udev rule, and every destructive step
has a confirmation dialog.

## File layout

```
gpu-switcher/
├── data/
│   ├── gpu-switcher.desktop
│   ├── polkit/gpu-switcher.policy
│   ├── dbus/com.github.wl04.gpu_switcher.Helper.service
│   │   # D-Bus activation (SystemdService=, Exec= fallback; file name must be
│   │   # exactly <service name>.service)
│   ├── dbus/system.d/com.github.wl04.gpu_switcher.Helper.conf
│   │   # system-bus policy: root owns the helper name, any user may call it
│   ├── icons/          # app icon + symbolic
│   └── locale/         # compiled .mo (built from po/)
├── debian/
│   ├── control  gpu-switcher.manpages  source/format  rules
│   ├── changelog  copyright  postrm
│   └── gpu-switcher-helper.service    # systemd unit for the helper
├── man/gpu-switcher.1
├── po/                 # gpu-switcher-en.po  gpu-switcher-ru.po
├── src/
│   ├── wrapper/gpu-switcher           # installed as /usr/bin/gpu-switcher
│   └── gpu_switcher/
│       ├── main.py                    # GTK4 UI
│       ├── detect.py                  # GPU enumeration, names, running/configured
│       │                              #   primary, conflicts, session guard
│       ├── selection.py               # pure radio-selection logic
│       ├── state.py                   # operation outcome, banner/button rules
│       ├── bus.py                     # D-Bus clients: helper, login1, session
│       │                              #   manager
│       ├── i18n.py                    # gettext setup
│       ├── core/                      # shared by app and helper (stdlib only,
│       │   │                          #   no gi import)
│       │   ├── udev.py                # rule constants, timeouts, conflict scan
│       │   └── gpu.py                 # PCI validation, GPU resolution, rule text
│       └── helper/                    # root D-Bus service
│           ├── __main__.py            # entry: python3 -m gpu_switcher.helper
│           ├── service.py             # D-Bus object, dispatch, lock, idle timer
│           ├── auth.py                # raw-D-Bus polkit CheckAuthorization
│           ├── rule_store.py          # atomic rule write, backups, removal
│           ├── udev.py                # async udevadm steps and verification
│           └── errors.py              # HelperFailure
├── tests/                             # unit tests (stdlib unittest), one
│                                      #   test_<module>.py per area
├── tools/
│   ├── build-deb.sh                   # .deb builder (plain dpkg-deb)
│   └── release.sh                     # release: bump version, changelog,
│                                      #   checks, build, lintian, commit,
│                                      #   GitHub release
├── .github/workflows/ci.yml
├── docs/screenshot.png
├── Makefile  pyproject.toml  requirements-dev.txt
├── DESIGN.md                       # this file
├── README.md  CONTRIBUTING.md  ROADMAP.md
└── LICENSE
```

## Packaging

- Package `gpu-switcher` (`Architecture: all`); GApplication/desktop ID
  `com.github.wl04.gpu-switcher`; the D-Bus service name uses `gpu_switcher`
  (hyphens are not allowed in D-Bus names).
- `tools/build-deb.sh` (plain `dpkg-deb`, no debhelper required) is the
  canonical build; `debian/` (control, rules, manpages, changelog, copyright,
  postrm, `gpu-switcher-helper.service`; source format `3.0 (quilt)`) supports
  the `dpkg-buildpackage -us -uc` flow. Both paths call the same
  `make install` target (the single file list), which also compiles the
  catalogs with `msgfmt`, so their contents are identical by construction. The
  `.deb` version is read from the top entry of `debian/changelog` (single
  source of truth).
- Installed docs: `changelog.Debian.gz` (gzip-compressed per Debian policy),
  plain `copyright`, gzipped man page.
- `postrm` leaves the managed rule and its `.bak.*` backups in place on
  `apt remove` — they are the user's primary-GPU choice, not package data.
- Quality gates: `lintian` clean on the built `.deb`, `dpkg-deb --info` /
  `--contents` sanity, `systemd-analyze verify` on the helper unit (CI).

## Versioning

- The `.deb` version is `<upstream>-<revision>` (e.g. `0.1.0-26`):
  `<upstream>` is the semver-style app version, `<revision>` counts
  public releases. The top entry of `debian/changelog` is the single
  source of truth (per Debian policy, `debian/control` has no `Version:`).
- **Revision `-NN`**: bumped by every public release.
- **Patch (`0.1.0 → 0.1.1`)**: bug fixes, hardening, and maintenance
  only — no new user-facing features, no breaking changes.
- **Minor (`0.1.x → 0.2.0`)**: new user-facing features (the roadmap
  items).
- **Major / `1.0.0`**: a stable release; breaking contract changes.
- When the upstream component changes, the Debian revision restarts
  from 1 (e.g. `0.1.1-1`, not `0.1.1-27`).
