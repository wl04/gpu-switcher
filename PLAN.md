# Primary GPU Switcher — Research Findings & Implementation Plan (v1)

Scope: **v1 = GNOME / Mutter only.** KDE Plasma and X11 paths are designed as
future extensions, not built now.

Identity:
- Local repo: `~/Dev/gpu-switcher` · remote: `github.com/wl04/gpu-switcher` (GitHub: @wl04)
- Package name: `gpu-switcher`
- GApplication / desktop ID: `com.github.wl04.gpu-switcher`
- D-Bus service name: `com.github.wl04.gpu_switcher.Helper` (D-Bus names cannot contain `-`)

---

## 1. Research findings

### 1.1 Is a reboot actually required? — Verified, answer: *not strictly*

Primary sources (Mutter upstream):

- **GNOME/mutter MR !1562** ("renderer/native: Add udev rule to select primary GPU"):
  > "Reboot **or manual triggering of udev rules** to make it take effect may be required."
- **mutter `doc/multi-gpu.md`** (current main): write the udev rule, then
  "Finally, **restart** and check that the desired GPU was selected", verified with
  `journalctl -b0 /usr/bin/gnome-shell` → line `GPU /dev/dri/cardN selected primary given udev rule`.

How it works under the hood:

- The tag `mutter-device-preferred-primary` is a **udev property**, stored in the persistent
  udev database (`/run/udev/data`), not in a file the compositor reads.
- Mutter reads the tags **when the DRM backend initializes devices at session start**
  (`meta-udev.c` / `g_udev_device_get_tags`). It does not re-read tags mid-session.
- Therefore, after writing a new rule file, the tag in the DB is still stale until:
  - a **reboot** (udev re-evaluates everything at boot) — always works, or
  - **`udevadm control --reload-rules` + `udevadm trigger --subsystem-match=drm`** —
    re-emits kernel events, udev re-runs the rules, the DB gets the new tag. Then a
    **logout/login** is enough (gnome-shell restarts and reads the fresh tag). No reboot.

**Consequence for the app:** offer both paths, exactly as Mutter's own docs phrase it:

| Path | Steps | Downtime |
|---|---|---|
| **Reboot (default, safe)** | write rule → `reload-rules` → "Reboot now" | full reboot |
| **Logout (fast path)** | write rule → `reload-rules` + scoped `udevadm trigger` → "Log out" | session only |

The fast path is deliberately scoped (`--subsystem-match=drm`) and optional; if `udevadm
trigger` fails, the app falls back to "reboot required". This matches the conservatism of
the existing script (no trigger by default) while unlocking the faster UX when wanted.

### 1.2 Privilege escalation — polkit best practices

From the official **polkit Reference Manual ("Writing polkit applications")**:

- **DO use polkit** when writing a *privileged mechanism* intended to be used by
  unprivileged programs. (Our case: a GUI app writing `/etc/udev/rules.d/*`.)
- Pattern: install a **`.policy` file into `/usr/share/polkit-1/actions`**; a privileged
  **mechanism** performs `CheckAuthorization()` (via D-Bus API or libpolkit-gobject-1);
  the **unprivileged GUI app simply calls the mechanism** and is oblivious to polkit.
- Default to **`auth_admin`** (not `auth_self`); "fairly likely that the default
  configuration should deny authorization in at least some cases."
- Pass a **customized authentication message** (`polkit.message` variable) so the dialog says
  what is actually happening.
- **DON'T block the UI thread** while waiting for the authority — use async D-Bus.

From **Ubuntu 26.10 release notes**: Ubuntu migrated away from its legacy policy setup to
the upstream/Debian **polkit default** — polkit remains the standard mechanism here
(and on every distro we might target later).

**Design consequence:**

- Custom action `…set-primary` with `auth_admin`, localized `message`.
- Privileged mechanism = a **D-Bus-activated system-bus helper** (dbus-daemon starts it
  **as root** on demand, like `systemd-logind`; **no setuid, no root daemon, no pkexec**).
- The helper checks authorization itself, with a localized message; the GUI app never
  talks to polkit.
- Reboot/logout use **login1's standard actions** (`org.freedesktop.login1.reboot`,
  `Session.Logout`) — no custom policy needed for those.
- Fallback if raw-D-Bus polkit proves flaky: `pkexec helper` (coarser, generic "exec"
  authorization, still polkit-mediated). Kept as a documented fallback, not the default.

Note: `libpolkit-gobject-1` is installed, but there is **no Python typelib for Polkit** on
this system, so the Python helper calls the polkit authority over **raw D-Bus**
(`org.freedesktop.PolicyKit1.Authority.CheckAuthorization` via GLib/Gio) — zero extra
dependencies.

### 1.3 Facts verified on this machine (Ubuntu 26.10 "Stonking Stingray")

| Item | Result |
|---|---|
| Desktop | GNOME Shell 51, Wayland session |
| GPUs | `card1` = RX 9070 XT (AMD `1002:7550`, PCI `0000:03:00.0`); `card2` = Ryzen 9800X3D iGPU (AMD `1002:13c0`, PCI `0000:12:00.0`); both on `amdgpu` |
| Current config | Your udev rule is live: `card2` carries `TAGS=…mutter-device-preferred-primary…`; journal: `GPU /dev/dri/card2 selected primary given udev rule` (iGPU is currently primary) |
| Tooling present | `python3` + PyGObject with **GTK 4**, `gi.repository.Gio` D-Bus, `udevadm` 261, `pkexec`+polkit, `lspci` |
| Tooling absent | KDE, Xorg, `polkit` Python typelib, PCI `label` files (empty — GPU naming must use `lspci -nn`) |
| GPU names (lspci) | `Navi 48 [Radeon RX 9070/9070 XT/9070 GRE]`, `Granite Ridge [Radeon Graphics]` |

---

## 2. Technology choice: Python + GTK4 vs Rust + GTK4

| | Python + GTK4 (PyGObject) | Rust + GTK4 (gtk4-rs) |
|---|---|---|
| Toolchain | None (already installed on target) | rustc + `libgtk-4-dev` + gtk-rs crates |
| Build time for `.deb` | install files, done | compile (minutes, cross-checks against gtk-rs version) |
| GTK4 API coverage | complete, current | good, occasionally lags GTK releases |
| Helper (privileged part) | same language, raw D-Bus polkit call | libpolkit-gobject-1 C API — textbook, more code |
| i18n | stdlib `gettext` + catalogs, trivial | gettext via env or `i18n-embed`, more setup |
| Runtime deps in `.deb` | `python3`, `python3-gi`, `libgtk-4-1` | none (dynamically linked to system gtk) |
| Startup | ~0.3–0.6 s (interpreter) | ~0.1 s |
| Fit for "simple GUI app" | ideal — single-language, no build step | overkill for v1 |

**Recommendation: Python + GTK4 for v1.** Everything (app + helper) in one language,
zero compilation, installable on Ubuntu/Debian/Fedora out of the box, and the app is
already demonstrably runnable on this machine today. Rust becomes the sensible choice
later if we want a standalone static binary (AppImage / minimal `.deb` with no runtime
deps) — the architecture (D-Bus protocol, helper contract) stays identical, so that
switch is cheap.

---

## 3. Architecture

```
┌──────────────────────────────┐        ┌────────────────────────────────────┐
│  gpu-switcher (GTK4, user)   │        │  gpu-switcher helper (root, D-Bus  │
│                              │        │  activated service, on-demand)     │
│  • enumerate GPUs (/sys)     │  D-Bus │  • polkit CheckAuthorization      │
│  • journal → running primary │◄──────►│  • write udev rule (+ backup)     │
│  • udevadm → configured      │        │  • reload-rules (+ trigger)       │
│  • login1 → reboot/logout    │        │  • refuses if conflicts found     │
└──────────────────────────────┘        └────────────────────────────────────┘
```

### 3.1 D-Bus interface (system bus)

- Service: `com.github.wl04.gpu_switcher.Helper`
- Object: `/com/github/wl04/gpu_switcher/Helper`
- Interface methods (all async from the app's point of view):
  - `Status(locale: s) → (rule_on_disk: s, message: s)` — implemented (M0): read-only
    view of the managed rule
  - `SetPrimary(pci: s, locale: s) → (ok: b, message: s, backup_path: s)` — M2
  - `SetAuto(locale: s) → (ok: b, message: s)` — M2 (removes only the managed rule)
  - `RefreshUdev(locale: s) → (ok: b, message: s)` — M3 (reload-rules + scoped
    trigger; used by the fast path; also run after every SetPrimary/SetAuto)

`locale` is passed explicitly because a root-activated helper has **no user locale in its
environment** — the localized polkit dialog message must come from the caller.

### 3.2 Polkit policy (installed to `/usr/share/polkit-1/actions/gpu-switcher.policy`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
  "-//freedesktop//DTD PolicyKit Policy Configuration 1//EN"
  "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="com.github.wl04.gpu_switcher.set-primary">
    <description>Change the primary display adapter (primary GPU)</description>
    <description xml:lang="ru">Смена основного дисплейного адаптера (основного GPU)</description>
    <message>Authentication is required to change the primary display adapter.</message>
    <message xml:lang="ru">Для смены основного дисплейного адаптера требуется авторизация.</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_active>no</allow_active>
      <allow_inactive>no</allow_inactive>
    </defaults>
    <defaults>
      <allow_any>no</allow_any>
      <allow_active>auth_admin</allow_active>
    </defaults>
  </action>
</policyconfig>
```

### 3.3 Managed rule file (compatibility with your existing script)

The app writes the **same file your script manages**:
`/etc/udev/rules.d/61-mutter-primary-gpu.rules`, with the same rule line
(`KERNELS=="<PCI>"`, `ATTRS{vendor}`, `ATTRS{device}`, `ENV{MUTTER_DEVICE_PREFERRED_PRIMARY}="1"`,
`TAG+="mutter-device-preferred-primary"`), the same backup scheme
(`.bak.<UTC-timestamp>.<random-suffix>`), the same conflict detection (any *other* rule
mentioning the tag blocks the switch), and `udevadm verify` when available (it is:
`udevadm` 261 here). Reusing the file means the script and the app coexist without
fighting over state.

### 3.4 Detection logic (unprivileged, app side)

1. **Enumerate**: `/sys/class/drm/card*` → `device` symlink → PCI address; keep only
   class `0x030000` with a bound driver; read `vendor`, `device`, `driver`.
2. **Name**: `lspci -nn` parse (present on this box); fallback: built-in vendor:device
   table; last resort: `PCI v00001002 d00007550`.
3. **Running primary**: latest `journalctl -b -q /usr/bin/gnome-shell` line matching
   `selected primary` → `cardN` → PCI. Fallback: "unknown (verify in journal after restart)".
4. **Configured preference**: managed rule file's `KERNELS` + each card's `TAGS`
   (`udevadm info --query=property`), plus the conflict scan.
5. **Session guard**: if not a GNOME Wayland session (`XDG_SESSION_TYPE=wayland` and
   gnome-shell present), show a warning banner: this build only supports GNOME.

### 3.5 File layout

```
gpu-switcher/
├── debian/
│   ├── control  compat  source/format  rules   (changelog added in M4)
├── src/
│   ├── gpu_switcher/
│   │   ├── __init__.py          # GApplication, wiring
│   │   ├── main.py              # GTK4 UI
│   │   ├── detect.py            # GPU enumeration, names, running/configured primary, conflicts, session guard
│   │   ├── bus.py               # M2: D-Bus clients: helper + login1
│   │   └── i18n.py              # gettext setup
│   └── helper/gpu_switcher_helper.py   # root D-Bus service + raw-D-Bus polkit
├── data/
│   ├── gpu-switcher.desktop
│   ├── polkit/gpu-switcher.policy
│   ├── dbus/com.github.wl04.gpu_switcher.Helper.service   # D-Bus activation (system, user=root)
│   ├── dbus/system.d/com.github.wl04.gpu_switcher.Helper.conf   # system-bus policy
│   ├── icons/  (SVG: app icon + symbolic)
│   └── locale/  (compiled .mo built from po/)
├── po/  gpu-switcher-en.po  gpu-switcher-ru.po
├── tools/
│   ├── build-deb.sh             # .deb builder (plain dpkg-deb, no debhelper)
│   └── po2mo.py                 # pure-Python .po → .mo compiler
├── Makefile                     # locale / package / install / uninstall
├── README.md
└── PLAN.md
```

---

## 4. UI / UX spec

One window (~560×430, Adwaita, no custom CSS):

- **Title**: "Primary GPU Switcher" / «Переключатель основного GPU»
- **GPU list** (one row per GPU):
  - icon, name (lspci), `PCI 0000:03:00.0 · /dev/dri/card1`
  - badges: **"In use now"** (running primary), **"Preferred (configured)"**
- **Selection**: radio buttons — each GPU + **"System default (auto)"**
- **Action buttons**:
  - **Apply** → confirmation dialog (shows what will be written + that a restart of the
    session is needed) → polkit dialog → helper `SetPrimary` (+ backup notice)
  - **Reboot now** → confirmation → `login1 Manager.Reboot`
  - **Log out** (fast path, secondary style) → `RefreshUdev` then `login1 Session.Logout`;
    disabled if trigger is unavailable/failed
  - **Reset to auto** → `SetAuto` (polkit) + note that auto-selection applies after restart
- **Status banner** (contextual):
  - after apply: "Change saved. It takes effect after reboot." / fast path: "…after next login."
  - denial: "Authorization was denied — nothing changed."
  - conflict: "Another udev rule sets a preferred GPU: <path>. Resolve it manually first."
- **Verification hint** (footer, collapsible): the exact `journalctl` command to confirm
  after restart, mirroring Mutter's own docs.

Interaction rules:
- All privileged calls are **async**; the button shows a spinner; UI never blocks.
- Apply/Reset are the only buttons that can change state; reboot/logout are clearly separated.
- Every destructive step has a confirmation dialog (reboot always double-confirmed).

---

## 5. Internationalization (en + ru from day one)

- All strings via **gettext**: `po/gpu-switcher-en.po`, `po/gpu-switcher-ru.po` →
  compiled with the pure-Python `tools/po2mo.py` (no gettext tools needed) to
  `gpu-switcher.mo`, installed to
  `/usr/share/gpu-switcher/locale/{en,ru}/LC_MESSAGES/`.
- App: `gettext.translation("gpu-switcher", …, languages=[env locale, "en"])`,
  searching the installed location first, then the source tree, with an
  English identity fallback.
- GTK widget built-ins (buttons, dialogs) localize automatically from the system locale.
- Helper: localized polkit messages selected by the `locale` argument passed over D-Bus
  (root process has no user locale).
- Both languages are LTR; no RTL concerns in v1. Adding more languages later = adding
  one more `.po` file.

---

## 6. `.deb` packaging

- Package name: `gpu-switcher` (command: `gpu-switcher`).
- GApplication / desktop ID: `com.github.wl04.gpu-switcher` (D-Bus service name uses
  `gpu_switcher` — hyphens are not allowed in D-Bus names).
- Repository: `~/Dev/gpu-switcher`, branch `main`, remote `origin` →
  `https://github.com/wl04/gpu-switcher` (public). Commits land per milestone
  (M0–M4); tag `v0.1.0` when the `.deb` test matrix passes.
- Depends: `python3 (>=3.12)`, `python3-gi`, `libgtk-4-1`, `polkitd`, `dbus`, `udev`,
  `systemd`, `pciutils`.
- Installed files:
  - `/usr/bin/gpu-switcher` (thin wrapper), `/usr/lib/gpu-switcher/gpu_switcher/*.py`
  - `/usr/lib/gpu-switcher/helper/gpu_switcher_helper.py` (mode 0755, root-owned;
    exec: `/usr/bin/python3 <path>`)
  - `/usr/share/applications/com.github.wl04.gpu-switcher.desktop`
  - `/usr/share/icons/hicolor/scalable/apps/com.github.wl04.gpu-switcher.svg`
    and `/usr/share/icons/hicolor/symbolic/apps/com.github.wl04.gpu-switcher-symbolic.svg`
  - `/usr/share/gpu-switcher/locale/{en,ru}/LC_MESSAGES/gpu-switcher.mo`
  - `/usr/share/polkit-1/actions/gpu-switcher.policy`
  - `/usr/share/dbus-1/system-services/com.github.wl04.gpu_switcher.Helper.service`
    (filename must be exactly `<service name>.service` for activation)
  - `/usr/share/dbus-1/system.d/com.github.wl04.gpu_switcher.Helper.conf`
    (the system bus denies name ownership by default; this policy lets root
    own the helper name and lets any user call the helper, which then
    authorizes via polkit)
- Build: `tools/build-deb.sh` (plain `dpkg-deb`, no debhelper required) is the
  canonical build; `debian/` is kept for a future `dpkg-buildpackage -us -uc`
  flow once `debhelper` is available. Both paths compile catalogs with the
  pure-Python `tools/po2mo.py` (`msgfmt` is not a build dependency).
- Quality gate before release: `lintian` clean, `dpkg-deb --info` sanity,
  `dpkg -i` / `dpkg -r` install+remove tested.

---

## 7. Phased build plan & test matrix

| Phase | Deliverable | Acceptance |
|---|---|---|
| **M0 skeleton — done** | git repo, project layout, `.desktop`, icon, app window, `debian/` boilerplate, `.deb` build | `dpkg -i` works, app launches from menu, first commits in |
| **M1 detection — done** | GPU enumeration + naming, running primary (journal), configured preference (rule+TAGS), session guard | both GPUs listed with correct names; "In use now" = card2 (iGPU) today; "Preferred" = card2 |
| **M2 polkit + apply** | `.policy`, helper service, `SetPrimary`/`SetAuto`, confirmation dialogs, Apply button, Reboot button | polkit dialog with localized message; rule file + backup written correctly; denial path safe |
| **M3 fast path + activation** | `RefreshUdev`, Log-out button, status banners, verification hint | logout/login path actually switches primary (journal shows new GPU); reboot path verified end-to-end |
| **M4 packaging polish** | lintian clean, changelog, icon, locale catalogs, install/remove scripts | full test matrix below passes |

**Test matrix (on this machine):**

1. Fresh install → app shows both GPUs, names, badges (running=card2, configured=card2).
2. Select card1 (dGPU) → Apply → polkit OK → verify rule file content, backup created.
3. Fast path: Apply → Log out → log back in → journal:
   `GPU /dev/dri/card1 selected primary given udev rule` → app badge moves to card1.
4. Reboot path: select card2 → Apply → Reboot → after boot, journal shows card2 primary.
5. Cancel polkit dialog → app shows denial, file unchanged.
6. Create a second rule with the mutter tag → Apply is refused with the conflict message.
7. `dpkg -r gpu-switcher` → managed rule and backups untouched.
8. Run `gpu-switcher` in a non-GNOME context (e.g. `DISPLAY=`-only shell) → warning banner,
   no crash.
