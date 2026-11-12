# Build & install helpers for gpu-switcher (see DESIGN.md).
SHELL := /bin/bash

# The top entry of debian/changelog is the single source of truth for the
# package version (dpkg-buildpackage and tools/build-deb.sh read the very same
# entry), so it is also what the man page header is rendered from — see
# tools/render-man.sh.
CHANGELOG := debian/changelog

# The languages the project ships are the .po files sitting in po/: adding a
# translation means adding a file, not editing this build (or the packaging
# scripts, or the tests - they all derive the set from po/).
LANGUAGES := $(notdir $(patsubst po/gpu-switcher-%.po,%,$(wildcard po/gpu-switcher-*.po)))

# The interpreter the checks run on: .venv/bin/python when the contributor created
# the venv CONTRIBUTING describes (created with --system-site-packages, so the
# system python3-gi stays importable), otherwise the system python3, which is what
# CI runs. The tests import gi, so an interpreter that cannot see the system
# dist-packages cannot run this project at all.
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

# Whether the coverage gate can run at all: coverage must be importable by the
# interpreter the tests run on.
COVERAGE_IMPORT_CHECK := $(PYTHON) -c 'import coverage'

# Run the unit tests (stdlib unittest; tests live in tests/).
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

# Lint and check formatting (ruff; config in pyproject.toml).
lint:
	ruff check src tests
	ruff format --check src tests

# shellcheck over the shell that actually ships or runs the build: the tools a
# maintainer types and the maintainer scripts, which run as root on every install
# and under dash, not bash. Info-level style notes are tolerated; warnings and
# above fail the build.
lint-shell:
	shellcheck --severity=warning tools/*.sh debian/postinst debian/postrm

# Type-check (mypy; config in pyproject.toml).
typecheck:
	mypy

# Run the suite under coverage and enforce the floor set in pyproject.toml.
# The floor is a ratchet: raise it when coverage goes up, never lower it to turn a
# red build green.
# coverage must be importable by the same python3 that runs the tests: they import
# gi from the system dist-packages, so an isolated tool venv cannot run them. The
# check names the missing tool instead of leaving a ModuleNotFoundError as the
# gate's output.
coverage:
	@$(COVERAGE_IMPORT_CHECK) 2>/dev/null || { \
	  echo "error: coverage is not importable by $(PYTHON) - install the pinned" >&2; \
	  echo "       dev tools into that interpreter (see CONTRIBUTING)" >&2; \
	  exit 1; \
	}
	PYTHONPATH=src $(PYTHON) -m coverage run -m unittest discover -s tests
	$(PYTHON) -m coverage report

# Compile the gettext catalogs in the source tree (used by dev runs).
locale:
	@test -n "$(LANGUAGES)" || { echo "error: no po/gpu-switcher-<lang>.po found" >&2; exit 1; }
	for lang in $(LANGUAGES); do \
	  mkdir -p data/locale/$$lang/LC_MESSAGES; \
	  msgfmt -o data/locale/$$lang/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-$$lang.po; \
	done

# Render the man page header (@VERSION@ / @DATE@ in man/gpu-switcher.1) into
# build/. Both packaging paths install that file: debian/gpu-switcher.manpages
# for the dh path, tools/build-deb.sh for the plain one.
man: build/man/gpu-switcher.1

build/man/gpu-switcher.1: man/gpu-switcher.1 $(CHANGELOG)
	./tools/render-man.sh $@ man/gpu-switcher.1 $(CHANGELOG)

clean:
	rm -rf build

# Build the .deb (lands in the parent directory).
package:
	./tools/build-deb.sh

# Stage the installed files into $(DESTDIR). DESTDIR is required: pass the
# packaging root, e.g. `make install DESTDIR=debian/gpu-switcher` (dh path)
# or `make install DESTDIR=$$(mktemp -d)` (tools/build-deb.sh). This is the
# single source of truth for the package file list; both build paths call
# it, so they stay in sync.
install:
	@test -n "$(DESTDIR)" || { echo "error: install requires DESTDIR=…"; exit 1; }
	@test -n "$(LANGUAGES)" || { echo "error: no po/gpu-switcher-<lang>.po found" >&2; exit 1; }
	for lang in $(LANGUAGES); do \
	  msgfmt -o data/locale/$$lang/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-$$lang.po; \
	  install -D -m 0644 data/locale/$$lang/LC_MESSAGES/gpu-switcher.mo \
	    $(DESTDIR)/usr/share/gpu-switcher/locale/$$lang/LC_MESSAGES/gpu-switcher.mo; \
	done
	install -D -m 0755 src/wrapper/gpu-switcher $(DESTDIR)/usr/bin/gpu-switcher
	# The whole package (core/ and helper/ subpackages) goes to the standard
	# dist-packages location; /usr/bin/python3 finds it without a sys.path hack.
	for f in $$(find src/gpu_switcher -name '*.py' | sort); do \
	  install -D -m 0644 $$f $(DESTDIR)/usr/lib/python3/dist-packages/$${f#src/}; \
	done
	install -D -m 0644 data/gpu-switcher.desktop $(DESTDIR)/usr/share/applications/com.github.wl04.gpu-switcher.desktop
	install -D -m 0644 data/icons/gpu-switcher.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/com.github.wl04.gpu-switcher.svg
	install -D -m 0644 data/icons/symbolic/gpu-switcher-symbolic.svg $(DESTDIR)/usr/share/icons/hicolor/symbolic/apps/com.github.wl04.gpu-switcher-symbolic.svg
	install -D -m 0644 data/locale/en/LC_MESSAGES/gpu-switcher.mo $(DESTDIR)/usr/share/gpu-switcher/locale/en/LC_MESSAGES/gpu-switcher.mo
	install -D -m 0644 data/locale/ru/LC_MESSAGES/gpu-switcher.mo $(DESTDIR)/usr/share/gpu-switcher/locale/ru/LC_MESSAGES/gpu-switcher.mo
	install -D -m 0644 data/polkit/gpu-switcher.policy $(DESTDIR)/usr/share/polkit-1/actions/gpu-switcher.policy
	# D-Bus activation: service file (name <service>.service) plus the
	# system-bus policy that lets root own the helper's name.
	install -D -m 0644 data/dbus/com.github.wl04.gpu_switcher.Helper.service $(DESTDIR)/usr/share/dbus-1/system-services/com.github.wl04.gpu_switcher.Helper.service
	install -D -m 0644 data/dbus/system.d/com.github.wl04.gpu_switcher.Helper.conf $(DESTDIR)/usr/share/dbus-1/system.d/com.github.wl04.gpu_switcher.Helper.conf
	# systemd unit for the helper (D-Bus activation via SystemdService=).
	install -D -m 0644 debian/gpu-switcher-helper.service $(DESTDIR)/usr/lib/systemd/system/gpu-switcher-helper.service
	# Debian docs: copyright stays plain text (dh_installdocs/dh_compress and
	# the plain build handle the changelog and man page).
	install -D -m 0644 debian/copyright $(DESTDIR)/usr/share/doc/gpu-switcher/copyright
	# Guard: every .py file under src/gpu_switcher must have been installed.
	for f in $$(find src/gpu_switcher -name '*.py'); do \
	  [ -f "$(DESTDIR)/usr/lib/python3/dist-packages/$${f#src/}" ] || { \
	    echo "error: $$f is missing from the install" >&2; exit 1; \
	  }; \
	done

# Remove the installed package (the managed udev rule is left behind on
# purpose: it is the user's primary-GPU choice, not package data).
uninstall:
	sudo apt remove gpu-switcher

.PHONY: test lint lint-shell typecheck coverage locale man clean package install uninstall
