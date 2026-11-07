# Build & install helpers for gpu-switcher (see DESIGN.md).
SHELL := /bin/bash

# Run the unit tests (stdlib unittest; tests live in tests/).
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

# Lint and check formatting (ruff; config in pyproject.toml).
lint:
	ruff check src tests
	ruff format --check src tests

# Type-check (mypy; config in pyproject.toml). GTK widgets are declared with
# their concrete (Any) type in main.py since PyGObject ships no stubs.
typecheck:
	mypy

# Compile the gettext catalogs in the source tree (used by dev runs).
locale:
	mkdir -p data/locale/en/LC_MESSAGES data/locale/ru/LC_MESSAGES
	msgfmt -o data/locale/en/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-en.po
	msgfmt -o data/locale/ru/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-ru.po

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
	msgfmt -o data/locale/en/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-en.po
	msgfmt -o data/locale/ru/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-ru.po
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

.PHONY: test lint typecheck locale package install uninstall
