"""Unit tests for the gettext setup in i18n.py.

Pins the locale variable precedence (LANGUAGE > LC_ALL > LC_MESSAGES >
LANG > en), the catalog search order (installed location before the source
tree), catalog completeness (every msgid has a translation in every shipped
locale), and the reference-compiler guards: catalog consistency
(`msgfmt -c`) and readability of a msgfmt-built catalog by CPython's
gettext.
"""

import gettext
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale. LANGUAGE is cleared so it cannot override the LC_ALL pin.
os.environ["LC_ALL"] = "en"
os.environ.pop("LANGUAGE", None)

from gpu_switcher import i18n


@contextmanager
def _locale_env(**variables: Any) -> Generator[None, None, None]:
    """Set locale variables for the test; a None value removes the variable.

    The previous environment is restored on exit.
    """
    saved = {key: os.environ.get(key) for key in variables}
    for key, value in variables.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class RequestedLanguage(unittest.TestCase):
    def test_requested_language_prefers_lc_all_over_lc_messages_and_lang(self) -> None:
        with _locale_env(
            LC_ALL="ru_RU.UTF-8", LC_MESSAGES="de_DE.UTF-8", LANG="en_US.UTF-8"
        ):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_requested_language_honors_language_variable(self) -> None:
        # LANGUAGE is a colon-separated preference list; its first valid
        # entry wins, ahead of LANG (the hand-rolled lookup ignored it).
        with _locale_env(
            LANGUAGE="ru:en", LC_ALL=None, LC_MESSAGES=None, LANG="en_US.UTF-8"
        ):
            self.assertEqual(i18n._requested_language(), "ru")

    def test_requested_language_uses_lc_messages_when_lc_all_unset(self) -> None:
        with _locale_env(LC_ALL=None, LC_MESSAGES="ru_RU.UTF-8", LANG="en_US.UTF-8"):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_requested_language_falls_back_to_english_when_no_locale_set(self) -> None:
        with _locale_env(LC_ALL=None, LC_MESSAGES=None, LANG=None):
            self.assertEqual(i18n._requested_language(), "en")


class Localedirs(unittest.TestCase):
    def test_localedirs_lists_installed_dir_before_source_tree(self) -> None:
        installed = "/usr/share/gpu-switcher/locale"
        src = os.path.abspath(
            os.path.join(os.path.dirname(i18n.__file__), "..", "..", "data", "locale")
        )
        dirs = i18n._localedirs()
        self.assertEqual(dirs[0], installed)
        if os.path.isdir(src):
            self.assertEqual(dirs, [installed, src])
        else:
            self.assertEqual(len(dirs), 1)


def _po(name: str) -> str:
    return os.path.join(os.path.dirname(i18n.__file__), "..", "..", "po", name)


def _unquote(value: str) -> str:
    """Strip the surrounding quotes of a .po string fragment."""
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"')
    return value


def _po_entries(path: str) -> list[tuple[str, str, bool]]:
    """Parse a flat .po file into (msgid, msgstr, fuzzy) entries.

    Minimal parser for the flat catalogs shipped here: quoted values may
    continue over multiple lines, `#, fuzzy` marks the following entry,
    and the header (empty msgid) is skipped.
    """
    entries = []
    msgid = ""
    msgstr = ""
    fuzzy = False
    pending_fuzzy = False
    mode = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("#"):
                if "fuzzy" in line:
                    pending_fuzzy = True
                continue
            if line.startswith("msgid "):
                if msgid:
                    entries.append((msgid, msgstr, fuzzy))
                msgid = _unquote(line[len("msgid ") :])
                msgstr = ""
                fuzzy = pending_fuzzy
                pending_fuzzy = False
                mode = "id"
            elif line.startswith("msgstr "):
                msgstr = _unquote(line[len("msgstr ") :])
                mode = "str"
            elif line.startswith('"') and mode is not None:
                fragment = _unquote(line)
                if mode == "id":
                    msgid += fragment
                else:
                    msgstr += fragment
            # Unknown directives (e.g. msgctxt) are ignored.
    if msgid:
        entries.append((msgid, msgstr, fuzzy))
    return entries


class MsgfmtBuild(unittest.TestCase):
    """Catalog build guard: the tracked catalogs must pass the reference
    compiler's consistency check, and a msgfmt-built catalog must be
    readable by CPython's gettext."""

    def setUp(self) -> None:
        self._msgfmt = shutil.which("msgfmt")
        if self._msgfmt is None:
            self.skipTest("msgfmt (gettext-tools) is not installed")

    def test_msgfmt_checks_catalogs_are_consistent(self) -> None:
        # One msgfmt invocation per catalog: multiple .po files would be
        # parsed as one merged catalog and every msgid would duplicate.
        # -o /dev/null: GNU msgfmt's -c still writes the default
        # messages.mo to the CWD without an explicit output file.
        assert self._msgfmt is not None  # setUp skips when msgfmt is missing
        for name in ("gpu-switcher-en.po", "gpu-switcher-ru.po"):
            out = subprocess.run(
                [self._msgfmt, "-c", "-o", "/dev/null", _po(name)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_msgfmt_built_catalog_is_readable_by_gettext(self) -> None:
        # Language-agnostic probe (EN is the primary language): every
        # shipped catalog must be built by msgfmt and readable by
        # CPython's gettext. The en catalog is the identity catalog, so
        # the probe msgid must come back unchanged; the ru catalog must
        # translate it (gettext falls back to the msgid when a
        # translation is missing, so a fallback is a failure here). No
        # language-specific text is pinned.
        assert self._msgfmt is not None  # setUp skips when msgfmt is missing
        for name, must_translate in (
            ("gpu-switcher-en.po", False),
            ("gpu-switcher-ru.po", True),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                mo_path = os.path.join(tmp, "LC_MESSAGES", "gpu-switcher.mo")
                os.makedirs(os.path.dirname(mo_path))
                out = subprocess.run(
                    [self._msgfmt, "-o", mo_path, _po(name)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                with open(mo_path, "rb") as f:
                    catalog = gettext.GNUTranslations(f)
                value = catalog.gettext("In use now")
                if must_translate:
                    self.assertNotEqual(value, "In use now")
                else:
                    self.assertEqual(value, "In use now")


class CatalogCompleteness(unittest.TestCase):
    """Language-agnostic completeness guard: every msgid in every shipped
    catalog must carry a non-empty, non-fuzzy translation, so gettext
    can never silently fall back to the English msgid in a shipped
    locale. No translated text is pinned."""

    def test_every_string_has_a_translation_in_each_catalog(self) -> None:
        for name in ("gpu-switcher-en.po", "gpu-switcher-ru.po"):
            for msgid, msgstr, fuzzy in _po_entries(_po(name)):
                self.assertFalse(fuzzy, f"{name}: fuzzy entry: {msgid!r}")
                self.assertNotEqual(
                    msgstr, "", f"{name}: missing translation: {msgid!r}"
                )


if __name__ == "__main__":
    unittest.main()
