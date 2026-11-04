# Build & install helpers for gpu-switcher (see DESIGN.md).
SHELL := /bin/bash

# Run the unit tests (stdlib unittest; tests live in tests/).
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

# Compile the gettext catalogs in the source tree (used by dev runs).
locale:
	mkdir -p data/locale/en/LC_MESSAGES data/locale/ru/LC_MESSAGES
	msgfmt -o data/locale/en/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-en.po
	msgfmt -o data/locale/ru/LC_MESSAGES/gpu-switcher.mo po/gpu-switcher-ru.po

# Build the .deb (lands in the parent directory).
package:
	./tools/build-deb.sh

# Install the freshly built .deb.
install: package
	sudo dpkg -i ../gpu-switcher_*.deb

uninstall:
	sudo dpkg -r gpu-switcher

.PHONY: test locale package install uninstall
