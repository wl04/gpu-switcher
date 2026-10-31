# gpu-switcher

A small GUI Linux application that switches which GPU GNOME (Mutter) uses as the
**primary display adapter** — no hand-edited udev rules.

Built for dual-GPU machines (dGPU + iGPU), e.g. a desktop with an AMD RX 9070 XT
plus the CPU's integrated graphics.

> **v1 scope: GNOME / Wayland (Mutter) only.** KDE Plasma and X11 paths are
> designed as later extensions, not built now.

## Status

**0.1.0-11 (M2)** — GPU detection: card list with "in use now" /
"preferred (configured)" badges, plus polkit-gated **Apply** / **Reset to
auto** (a root D-Bus-activated helper writes or removes the managed udev rule
with backup and conflict checks), confirmation dialogs, **Reboot now**,
contextual status banners, i18n (English + Russian), `.deb` packaging. The
Log out fast path lands in M3 (see [Roadmap](#roadmap)).

## Screenshot

_TBD_

## How it works

Mutter chooses the primary GPU when the session starts, reading the udev tag
`mutter-device-preferred-primary` from the udev database. `gpu-switcher`:

1. writes the managed udev rule `/etc/udev/rules.d/61-mutter-primary-gpu.rules`
   (with backup and conflict checks) under **polkit** authorization — a
   root D-Bus-activated helper does the privileged work, the GUI app never
   touches polkit directly;
2. activates the change via **Reboot** (always works) or a **Log out** fast
   path (`udevadm` rules reload + scoped trigger), after which gnome-shell
   re-reads the fresh tag at login.

Verification after restart (straight from Mutter's own docs):

```sh
journalctl -b0 /usr/bin/gnome-shell | grep 'selected primary'
```

## Install

### `.deb`

```sh
sudo dpkg -i gpu-switcher_0.1.0-11_all.deb
```

### From source

```sh
git clone https://github.com/wl04/gpu-switcher
cd gpu-switcher
./tools/build-deb.sh    # builds ../gpu-switcher_0.1.0-11_all.deb
sudo dpkg -i ../gpu-switcher_0.1.0-11_all.deb
```

Requires: `python3 (>= 3.12)`, `python3-gi`, `libgtk-4-1`, `polkitd`, `dbus`,
`udev`, `systemd`, `pciutils` — all standard on Ubuntu/Debian.

## Roadmap

| Milestone | What lands |
|---|---|
| **M1** | GPU detection: enumerate cards, lspci names, running primary (journal), configured preference (rule + udev tags), session guard |
| **M2** | polkit-gated **Apply** / **Reset to auto**, confirmation dialogs, **Reboot** button |
| **M3** | **Log out** fast path, status banners, verification hint |
| **M4** | Packaging polish (lintian clean, changelog), release test matrix, `v0.1.0` tag |

## Development notes

- App + helper are plain Python 3 with PyGObject (GTK 4) — no compilation.
- `.deb` is built with plain `dpkg-deb` via `tools/build-deb.sh`
  (`debian/` is kept for a future debhelper flow).
- gettext catalogs (`po/`) are compiled with the pure-Python `tools/po2mo.py`.
- `PLAN.md` contains the full research notes and the original plan.

## License

MIT — see [LICENSE](LICENSE) for details.
