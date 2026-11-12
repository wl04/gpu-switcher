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

  `--system-site-packages` is required, not a convenience: the tests import `gi`
  from the system dist-packages. `make` uses `.venv/bin/python` when it exists and
  the system `python3` otherwise (that is what CI runs).
- A gate whose tool is missing fails with the name of the tool, and
  `tools/release.sh` checks all of them before it touches `debian/changelog`.
- gettext catalogs (`po/`) compile with `msgfmt` (build-time `gettext`
  dependency).

## Checks

Run before every commit:

- `make test` — unit tests. Plain stdlib `unittest` (no test-framework
  dependencies); test names follow `Operation_Behavior_Context`.
- `make lint` — ruff lint and format check.
- `make lint-shell` — shellcheck over `tools/*.sh` and the maintainer scripts,
  which run as root and under `/bin/sh`. `shellcheck` comes from the pinned
  `shellcheck-py` wheel, so the gate is versioned like the Python tools.
- `make typecheck` — mypy over `src/` and `tests/`; `disallow_untyped_defs` is
  enforced for the pure modules (`core/*`, `cli`, `decisions`, `selection`,
  `state`, `gpu_state`, `i18n`, `detect`) and the test modules listed in
  `pyproject.toml`. The helper package and the GTK UI are checked for type errors
  only: PyGObject ships no stubs, so a Gio/Gtk annotation there documents the
  contract rather than constrains it.
- `make coverage` — the same tests under coverage, failing below the floor in
  `pyproject.toml`. Treat the floor as a ratchet: raise it when coverage goes up,
  never lower it to make a red build green.
- `make locale` — compile the gettext catalogs into the source tree.
- `make package` — build the `.deb`.

CI (`.github/workflows/ci.yml`) runs the tests under that coverage floor, lint,
typecheck, both `.deb` builds with lintian, and `systemd-analyze verify` on the
systemd unit.

The window tests are gated by `GPU_SWITCHER_GUI_TESTS=1` and skipped without it:
they assemble the real window, so they need a display, and a developer's live
session is not where a test belongs. CI runs them on a virtual one:
`xvfb-run -a` with the flag set. Locally:
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
whether that directory is still writable. Edit the unit and the probe follows it:
a sandbox change that breaks the helper has to fail here rather than on someone's
machine. The probe never writes to the rule directory — it asks with `access(2)`;
where it does write, and why, is in the script's header.

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

`tools/release.sh <version> <changelog-bullets-file> [--dry-run]` checks the gate
tools, runs the tests, lint, shellcheck, type and coverage checks and the catalog
compile, builds and lints the `.deb`, and only then prepends the changelog entry
(the version's source of truth; urgency from `$URGENCY`, default `low`), syncs
`pyproject.toml` and `gpu_switcher.__version__`, commits, tags, pushes both and
creates the GitHub release on that tag. A run that fails before the commit puts
those three files back, so an aborted release leaves no half-version in the tree.

- Keep the bullets file outside the repository (`../release-notes/`): the package
  build runs `make clean`, which removes `build/`. Each bullet is one line
  starting with `  * `.
- The tag `v<version>` is annotated and carries the sha256 of the `.deb` the
  release published, so a checkout can tell which artifact a release shipped.
  `gh release create` would make the tag on the server, where the local
  repository never has it. Earlier releases carry lightweight tags.
- A version whose tag already exists, locally or on `origin`, is refused: the tag
  is the claim that a version was released.
- Run with `--dry-run` first; the working tree must be clean.

## Conventions

- Keep commits small and focused; behaviour changes come with a test that
  fails first.
- Callback contracts crossing the app/helper boundary are named aliases:
  `Translator` (i18n), `HelperDone` and `PowerDone` (bus), `StepDone`,
  `RefreshDone`, `OkDone` and `Cancellable` (helper/udev), `AuthDone`
  (helper/auth), `RuleWriteDone` (helper/rule_store). A new reply field or step
  extends the alias instead of restating a `Callable[...]` at each call site.
- Code and identifiers are in English; user-facing strings go through gettext
  (`po/`).

## Adding a language

The languages the project ships are the `po/gpu-switcher-<lang>.po` files: the
Makefile targets, both `.deb` build paths and the catalog tests derive the
language set from that directory, so a new translation is one file and a compile.

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
