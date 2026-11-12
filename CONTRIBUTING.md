# Contributing

Thanks for helping out. This page covers building, testing and releasing
`gpu-switcher`; for the architecture (polkit, D-Bus helper, detection) read
[DESIGN.md](DESIGN.md).

## Setup

- Plain Python 3 (>= 3.12) + PyGObject (GTK 4); there is nothing to compile.
- Dev tools (not package dependencies) are pinned in `requirements-dev.txt`.
  Create the venv the checks use and install them into it:

  ```sh
  uv venv --system-site-packages .venv
  uv pip install --python .venv/bin/python -r requirements-dev.txt
  ```

  `--system-site-packages` is not a convenience here: the tests import `gi`, which
  lives in the system dist-packages, so an interpreter that cannot see them cannot
  run this project. `make` picks `.venv/bin/python` automatically when it exists,
  and the system `python3` otherwise (that is what CI uses).
- A gate whose tool is missing fails with the name of the tool.
- gettext catalogs (`po/`) compile with `msgfmt` (build-time `gettext`
  dependency).

## Checks

Run before every commit:

- `make test` — unit tests. Plain stdlib `unittest` (no test-framework
  dependencies); test names follow `Operation_Behavior_Context`.
- `make lint` — ruff lint and format check.
- `make lint-shell` — shellcheck over `tools/*.sh` and the maintainer scripts,
  which run as root and under `/bin/sh`. The `shellcheck` command comes from the
  pinned `shellcheck-py` wheel in `requirements-dev.txt`, so the gate is versioned
  like the Python tools instead of whatever the distro happens to ship.
- `make typecheck` — mypy over `src/` and `tests/`; `disallow_untyped_defs`
  is enforced for the core modules and tests (the helper package and the GTK
  UI are typed for errors but not yet for untyped defs).
- `make coverage` — the same tests under coverage, failing below the floor in
  `pyproject.toml`. Treat the floor as a ratchet: raise it when coverage goes up,
  never lower it to make a red build green.
- `make locale` — compile the gettext catalogs into the source tree.
- `make package` — build the `.deb`.

CI (`.github/workflows/ci.yml`) runs the tests under that coverage floor, lint,
typecheck, both `.deb` builds with lintian, and `systemd-analyze verify` on the
systemd unit.

The window tests are gated by `GPU_SWITCHER_GUI_TESTS=1` and skipped without it.
They assemble the real window, so they need a display - and in a normal shell the
only display available is the developer's live session, which is not where a test
belongs. CI runs them on a virtual one: `xvfb-run -a` with the flag set. Locally:
`GPU_SWITCHER_GUI_TESTS=1 xvfb-run -a make test` (needs `xvfb` installed).

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

The write test targets a file in `/etc`, not in `/tmp` or `/var/tmp`:
`PrivateTmp=yes` replaces those with private *writable* mounts that sit outside
the `ProtectSystem` fence, so writing there succeeds exactly when the sandbox is
healthy. `/etc` is what `ProtectSystem=strict` is supposed to close while
`ReadWritePaths` keeps one directory open, which is the pair being verified. The
probe also reloads systemd before starting its unit and reads only this
activation's status and journal lines, so a run whose unit never started reports
"nothing was measured" instead of quoting an earlier run's numbers.

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

## Adding a language

The languages the project ships are the `po/gpu-switcher-<lang>.po` files. The Makefile
targets, both `.deb` build paths and the catalog tests all derive the language set from
that directory, so a new translation is one file and a compile - not an edit of four
lists that each keep their own copy of the set:

1. Add `po/gpu-switcher-<lang>.po`: copy `po/gpu-switcher-en.po`, keep the header entry
   (the empty `msgid ""`), and fill every `msgstr`.
2. Run `make locale`. The compiled catalog lands in `data/locale/<lang>/LC_MESSAGES/`,
   and `make package` / `tools/build-deb.sh` install it.
3. Run `make test`. The catalog guards then cover the new language on their own: every
   msgid needs a non-empty, non-fuzzy translation; every string the code asks for must
   be present in every catalog; and the committed `.mo` must answer exactly what its
   `.po` declares (that last one is why the `.po` and the regenerated `.mo` are
   committed together).

Nothing under `src/` lists languages: gettext resolves the catalog from
`LANGUAGE`/`LC_ALL` and falls back to English, so an untranslated string stays English
instead of breaking the UI. A language with a `.po` but no compiled catalog fails the
suite rather than shipping silently.

`tools/screenshot.sh <lang>` renders the window in that language, which is how
`docs/screenshot-ru.png` is produced.

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
