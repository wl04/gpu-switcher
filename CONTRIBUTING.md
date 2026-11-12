# Contributing

Thanks for helping out. This page covers building, testing and releasing
`gpu-switcher`; for the architecture (polkit, D-Bus helper, detection) read
[DESIGN.md](DESIGN.md).

## Setup

- Plain Python 3 (>= 3.12) + PyGObject (GTK 4); there is nothing to compile.
- Dev tools (not package dependencies) are pinned in `requirements-dev.txt`;
  install them once, e.g. `uv tool install ruff mypy`.
- gettext catalogs (`po/`) compile with `msgfmt` (build-time `gettext`
  dependency).

## Checks

Run before every commit:

- `make test` — unit tests. Plain stdlib `unittest` (no test-framework
  dependencies); test names follow `Operation_Behavior_Context`.
- `make lint` — ruff lint and format check.
- `make typecheck` — mypy over `src/` and `tests/`; `disallow_untyped_defs`
  is enforced for the core modules and tests (the helper package and the GTK
  UI are typed for errors but not yet for untyped defs).
- `make locale` — compile the gettext catalogs into the source tree.
- `make package` — build the `.deb`.

CI (`.github/workflows/ci.yml`) runs test / lint / typecheck, both `.deb`
builds with lintian, and `systemd-analyze verify` on the systemd unit. The GUI
tests need a display; CI runs them under Xvfb.

## End-to-end verification

On a real GNOME/Wayland machine: run the app, apply a switch through the
polkit prompt, restart the session, then confirm with

```sh
journalctl -b0 /usr/bin/gnome-shell | grep -E 'selected (as )?primary'
```

Also check that the packaged unit's sandbox (`ProtectSystem=strict`,
`ProtectKernelTunables=yes`) still lets the helper write its rule and nothing
more:

```sh
sudo ./tools/verify-helper-sandbox.sh
```

It copies the `[Service]` section of `debian/gpu-switcher-helper.service` into a
throwaway unit, replaces only the process, and reports whether the probe could
write outside `/etc/udev/rules.d` (only `EROFS` counts as a correct refusal) and
whether that directory is still writable. Edit the unit and the probe follows it,
which is the point: a sandbox change that breaks the helper has to fail here
rather than on someone's machine. The probe never writes to the rule directory —
it asks with `access(2)`.

The udev refresh is a separate risk that neither `systemd-analyze verify` nor the
sandbox probe can see. On a multi-GPU machine with the package installed, apply a
switch and inspect

```sh
journalctl -u gpu-switcher-helper.service | grep -i 'udev refresh'
```

Expect `udev refresh: reload-rules, DRM trigger and tag check succeeded`.

## Packaging

- `tools/build-deb.sh` — plain `dpkg-deb` build.
- `dpkg-buildpackage -us -uc` — the `debian/` flow.
- Both paths build the same package; the version comes from the top entry of
  `debian/changelog`.

## Releasing

`tools/release.sh <version> <changelog-bullets-file> [--dry-run]` prepends the
changelog entry (the version's source of truth; urgency from `$URGENCY`,
default `low`), syncs `pyproject.toml`, runs the tests, lint and type checks,
builds and lints the `.deb`, commits, pushes and creates the GitHub
release. Each changelog bullet must be a single line starting with `  * `.
Run with `--dry-run` first; the working tree must be clean.

## Conventions

- Keep commits small and focused; behaviour changes come with a test that
  fails first.
- Code and identifiers are in English; user-facing strings go through gettext
  (`po/`).

## Updating the screenshot

`docs/screenshot.png` is English regardless of your system language:

```sh
make locale
tools/screenshot.sh        # en -> docs/screenshot.png
tools/screenshot.sh ru     # ru -> docs/screenshot-ru.png
```

The script sets `LANGUAGE`/`LC_ALL` for the app (gettext and GTK) and renders
the window to PNG with GTK itself (`tools/screenshot.py`), so it needs no
screenshot tool and works on Wayland. It must run in a graphical session; the
window shows your real GPU names and PCI addresses.
