# gpu-switcher — Design notes

What the app does and why it is built this way. For usage see
[README.md](README.md); for build and release instructions see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Scope

**GNOME / Wayland (Mutter) only.** KDE Plasma and X11 are not supported; see
[What a Plasma or X11 port would take](#what-a-plasma-or-x11-port-would-take-not-supported-today)
for what supporting them would involve and why the verification signal is the blocker.

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
adding one `.po` file. Catalogs are looked up in that installed directory and
then in the checkout's `data/locale/` (`i18n.localedirs()`); the helper consults
the same list, so running it from a source tree answers in the caller's language
as well. The languages the helper accepts are derived from the catalogs that are
actually shipped, never from a list kept by hand.

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
  in its environment**, so the helper's reply messages are translated from
  the caller's locale (normalized to a shipped catalog language). The polkit
  dialog text is not affected: it comes from the policy file and the
  authentication agent's own locale.

### Polkit

- Custom action `com.github.wl04.gpu_switcher.set-primary`, `auth_admin`,
  localized `message`; the policy file is installed to
  `/usr/share/polkit-1/actions/gpu-switcher.policy`.
- `auth_admin` rather than `auth_admin_keep`, on purpose. The D-Bus config lets
  any user of the session call the helper, so this check is the only thing
  between a background process and `/etc/udev/rules.d`. `auth_admin_keep` would
  let one authentication authorize every later call until logout; instead each
  operation is authorized on its own, which is why Apply and Reset ask again
  even a second after the previous one succeeded. The app keeps no prompt cache
  of its own either: the action buttons are insensitive while an operation is
  in flight, and a helper that is already working answers another call with
  `com.github.wl04.gpu_switcher.Error.Busy`.
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
assignment on a line and joins `\` continuations). A rule the scan cannot read is
a separate finding, never a conflict: the app ignores it (the helper repeats the
scan as root, where it can read them) and the helper refuses the switch while
naming the file it could not read. The script and the app
therefore coexist without fighting over state; `udevadm verify` runs when
available.

### Detection (unprivileged, app side)

1. **Enumerate**: `/sys/class/drm/card*` → `device` symlink → PCI address;
   keep only display-controller classes (`0x030000` VGA, `0x030200` 3D
   controller) with a bound driver.
2. **Name**: `lspci -Dmmnn`; fallback: `PCI v<vendor> d<device>` (no
   hard-coded GPU table).
3. **Running primary**: latest line matching `GPU /dev/dri/cardN selected (as
   )?primary`, read from this user's own journal (`journalctl --user`) and only
   then from the system journal: a normal user can always read their own journal,
   while the system one is restricted to `adm` / `systemd-journal` members on many
   distros. mutter logs `GPU /dev/dri/cardN selected primary given udev rule` in
   rule mode and `Boot VGA GPU /dev/dri/cardN selected as primary` in auto mode.
   The pid is scoped to the single gnome-shell pid owned by our uid (read from
   `/proc/<pid>/status`), with no logind round trip; other users'/sessions' shells
   never count, and the query is bounded to that shell's start time so an earlier
   session's choice never answers for this one. Selection lines are matched by the
   app rather than by `journalctl --grep`, which needs a PCRE2-capable build.
   Zero or multiple in-scope pids leave the running primary unknown.
4. **Configured preference**: the managed rule's `KERNELS` value, plus the
   conflict scan (the root helper repeats it authoritatively).
5. **Tag evidence**: `udevadm info --query=property` reports the TAGS list udev
   holds for the configured card, which an ordinary user may read. It answers a
   question the rule file cannot: whether udev is acting on that rule now. Asked
   only when a rule and a configured card exist; "udev could not be asked" is kept
   apart from "udev does not carry the tag", and the evidence is only ever used to
   make the diagnosis worse, never better (a tag can survive a reload that failed).
6. **Session guard**: if the session is not GNOME/Wayland
   (`XDG_SESSION_TYPE=wayland` and `XDG_CURRENT_DESKTOP` is GNOME, or
   `gnome-shell` present when it is unset) — warning banner, privileged
   actions disabled.

## UI

One window (Adwaita, no custom CSS):

- GPU rows: `lspci` name, `PCI <addr> · /dev/dri/cardN`, badges **"In use
  now"** (running primary), **"Preferred (configured)"** (managed rule), and
  what is plugged into the card: **"Display: HDMI-A-1"** for outputs the kernel
  reports connected, **"No display connected"** when the card has outputs and
  none of them is occupied. Nothing is said when sysfs did not answer (see
  *Detection*): "no display" is a claim a user acts on.
- Apply to a card whose outputs are all free adds a warning to the confirmation:
  the session can end up with no screen until a cable is moved onto one of that
  card's outputs, which the app cannot fix from software.
- Radio selection: each GPU + **"System default (auto)"**.
- **Apply** — confirmation dialog → polkit → `SetPrimary` (+ backup notice).
- **Reset to auto** — `SetAuto` (+ note that auto-selection applies after
  restart).
- **Reboot now** — two confirmations → login1 `Manager.Reboot`.
- **Log out** — fast path via the GNOME session manager `Logout` on the
  session bus; enabled only after an Apply/Reset whose udev refresh
  succeeded (no polkit prompt of its own).
- **Refresh** — re-read GPUs, the rule on disk and the running adapter; needs no
  authorization. Returning to the window does the same automatically when the
  snapshot is older than ~15 s and nothing is in flight (a confirmation dialog or
  an action awaiting its reply) — re-detection rebuilds the content, so it never
  happens under the user's hands.
- Contextual status banners: applied / denied / conflict / unavailable
  configured GPU / unknown outcome after a timeout.
- State row (`gpu_state.py`): an icon + text derived from the running
  primary, the managed rule, the rule's mtime versus gnome-shell's start time,
  the tag evidence and the last Apply/Reset outcome. States: auto, active (GNOME
  picked the configured GPU), pending login / pending reboot (rule changed after
  the session started; the refresh result is not persisted, so after an app
  restart pending means reboot — and pending reboot also when udev provably does
  not carry the tag at all), ignored (udev applies the rule and GNOME still chose
  another GPU), unknown.
- Collapsible verification hint: the exact `journalctl` command to confirm
  after restart; expanded only when the state is unknown or ignored.

Interaction rules: privileged calls are async (the buttons are disabled while
an operation or a re-detection runs; the UI never blocks, detection runs on a
worker thread); Apply/Reset change the udev rule, and every destructive step
has a confirmation dialog.

## What a Plasma or X11 port would take (not supported today)

Scope says GNOME/Mutter only. This records what the alternatives would need, so the
limitation is a decision with a reason rather than an omission.

**The lever is different on every compositor.** What the app really changes is which
GPU the *compositor* treats as primary, and that is compositor-specific:

- **Mutter (GNOME on Wayland)** — a udev tag, `mutter-device-preferred-primary`, read
  when the session starts. Mutter also logs which card it picked, which is the
  evidence the app reports.
- **KWin (Plasma on Wayland)** — an environment variable, `KWIN_DRM_DEVICES`: a
  colon-separated list of DRI device paths whose first entry becomes the primary
  ([KWin !9003](https://invent.kde.org/plasma/kwin/-/merge_requests/9003)). There is
  no udev rule to write, so the change goes into the session environment (a systemd
  user environment entry or `~/.config/environment.d/`) - no root needed, but a
  different privilege story and a different rollback story. Card numbering is not
  stable across reboots or unbind/rebind, so paths must be resolved from PCI
  (`/dev/dri/by-path/`) at write time instead of remembered.
- **X11 (Xorg)** — an `OutputClass` section in `/etc/X11/xorg.conf.d/` matching the
  driver with `Option "PrimaryGPU" "true"`. Root again, and that file is shared with
  whatever else configures X. Per-process render offload (`DRI_PRIME=1`,
  `xrandr --setprovideroffloadsink`, the vendor offload variables) answers a *different*
  question: which GPU renders one application, not which one drives the screen.

**Evidence is the hard part, not writing.** The state machine (active / pending
login / pending reboot / ignored / unknown) rests on two observable facts on GNOME:
the tag udev actually applied, and the compositor naming the card it chose. Neither
is established for KWin or Xorg, and a port that cannot confirm its own result would
sit in "unknown" after every Apply. The verification signal has to be found before
the writing mechanism is built, not after.

**What already carries over:** GPU enumeration and PCI naming, the per-card display
outputs read from sysfs, the helper's rule discipline (atomic write, backups,
retention, verification), the D-Bus and polkit shape, and the state machine and UI
states.

**The same reasoning is upstream.** KWin's !9003 describes a hybrid laptop where the
desktop ended up composited on the iGPU although the only active output was an
external monitor on the discrete card - cross-GPU compositing and a heavy
performance penalty. That is why this app reports display-connected outputs per card
instead of guessing from card order, and why Apply to a card with no connected
display is warned about.

**Explicit non-goals for such a port:** one udev rule claimed to work for all
compositors; writing a user's session environment from the root helper (that is the
user's own configuration, and it does not belong to a privileged process).

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
│   ├── changelog  copyright  postinst  postrm  lintian-overrides
│   └── gpu-switcher-helper.service    # systemd unit for the helper
├── man/gpu-switcher.1        # @VERSION@ / @DATE@ rendered by `make man`
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
│       ├── i18n.py                    # gettext setup; the helper shares its
│       │                              #   catalog directories
│       ├── core/                      # shared by app and helper (stdlib only,
│       │   │                          #   no gi import)
│       │   ├── udev.py                # rule constants, timeouts, rule scan
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
  postinst, postrm, `gpu-switcher-helper.service`; source format `3.0 (quilt)`) supports
  the `dpkg-buildpackage -us -uc` flow. Both paths call the same
  `make install` target (the single file list), which also compiles the
  catalogs with `msgfmt`, so their contents are identical by construction. The
  `.deb` version is read from the top entry of `debian/changelog` (single
  source of truth).
- Installed docs: `changelog.Debian.gz` (gzip-compressed per Debian policy),
  plain `copyright`, gzipped man page. Its `.TH` header comes from `make man`,
  which renders the `@VERSION@` / `@DATE@` tokens in `man/gpu-switcher.1` from
  the changelog's top entry into `build/man/` (both build paths install that
  file), so an installed page cannot name a version or a date the package never
  had.
- Maintainer scripts are written here rather than generated: `dh_installsystemd`
  is disabled in `debian/rules` so both build paths ship the same `postinst` /
  `postrm`. `postinst` reloads systemd (an upgraded unit file is not picked up
  otherwise) and, on an upgrade only, `try-restart`s a helper that is still
  running so the next call is answered by the new code instead of the code that
  was just replaced. `postrm` reloads systemd again once the unit files are
  gone. `systemctl` is called directly, not through `deb-systemd-invoke`: that
  is a debhelper tool, which an installed system does not have to provide; the
  `/run/systemd/system` guard in `debian/postinst` is the check it performs
  internally, and the deviation is recorded in
  `debian/gpu-switcher.lintian-overrides`.
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
