"""Unit tests for the gettext setup in i18n.py.

Covers locale variable precedence (LANGUAGE > LC_ALL > LC_MESSAGES > LANG >
en), the catalog search order (installed before source tree), catalog
completeness (every msgid translated in every shipped locale), `msgfmt -c`
consistency, that the tracked .mo files still match their .po sources, and that
CPython's gettext can read a msgfmt-built catalog.
"""

import ast
import gettext
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

# i18n binds gettext at import time: pin the locale before importing the app
# so English strings are asserted under any developer locale (LANGUAGE would
# override LC_ALL, so it is cleared).
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
        # LANGUAGE is a colon-separated preference list: its first valid entry
        # wins over LANG.
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
        installed = i18n.INSTALLED_LOCALE_DIR
        src = os.path.abspath(
            os.path.join(os.path.dirname(i18n.__file__), "..", "..", "data", "locale")
        )
        dirs = i18n.localedirs()
        self.assertEqual(dirs[0], installed)
        if os.path.isdir(src):
            self.assertEqual(dirs, [installed, src])
        else:
            self.assertEqual(len(dirs), 1)


def _po(name: str) -> str:
    return os.path.join(os.path.dirname(i18n.__file__), "..", "..", "po", name)


def _mo(language: str) -> str:
    return os.path.join(
        os.path.dirname(i18n.__file__),
        "..",
        "..",
        "data",
        "locale",
        language,
        "LC_MESSAGES",
        "gpu-switcher.mo",
    )


def _unquote(value: str) -> str:
    """Strip the surrounding quotes of a .po string fragment."""
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"')
    return value


def _po_text(value: str) -> str:
    """Decode the escape sequences this parser leaves in a .po string, so the
    value can be compared with what gettext actually stores."""
    return value.replace("\\n", "\n").replace("\\t", "\t")


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


def _po_languages() -> tuple[str, ...]:
    """The languages the project ships: the .po files in po/, nothing else.

    Tests must not keep their own copy of that list, or a new translation would
    pass while its catalog is never checked.
    """
    source_dir = os.path.join(os.path.dirname(i18n.__file__), "..", "..", "po")
    return tuple(
        sorted(
            name.removeprefix("gpu-switcher-").removesuffix(".po")
            for name in os.listdir(source_dir)
            if name.startswith("gpu-switcher-") and name.endswith(".po")
        )
    )


class CommittedCatalogs(unittest.TestCase):
    """The compiled catalogs under data/locale/ are tracked so the app runs from a
    checkout or an installed package with no build step at all. That is only true
    while they match their .po sources: translate a string, forget `make locale`,
    and the app keeps answering with the old wording forever.

    The comparison is semantic - what the committed catalog answers for each
    msgid the .po declares - rather than a byte-for-byte diff against a fresh
    msgfmt build, because msgfmt output bytes vary with the gettext version while
    these answers do not.
    """

    _LANGUAGES = _po_languages()

    def test_committed_catalog_answers_every_source_string_with_its_translation(
        self,
    ) -> None:
        for language in self._LANGUAGES:
            with self.subTest(language=language):
                entries = _po_entries(_po(f"gpu-switcher-{language}.po"))
                with open(_mo(language), "rb") as handle:
                    catalog = gettext.GNUTranslations(handle)
                for msgid, msgstr, _fuzzy in entries:
                    if not msgid:  # the metadata header entry
                        continue
                    self.assertEqual(
                        catalog.gettext(_po_text(msgid)),
                        _po_text(msgstr),
                        f"data/locale/{language} is stale for {msgid!r}: run make locale",
                    )

    def test_catalogs_exist_for_exactly_the_languages_the_sources_cover(self) -> None:
        # A language added to po/ without running make locale has no catalog to
        # load; a leftover data/locale entry outlives a dropped translation.
        sources = set(self._LANGUAGES)
        self.assertTrue(sources, "po/ holds no translations")
        shipped = {
            language for language in self._LANGUAGES if os.path.isfile(_mo(language))
        }
        self.assertEqual(shipped, sources)


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


def _translatable_msgids() -> set[str]:
    """Literal first arguments of _() / i18n._() / tr() calls in src/."""
    root = os.path.join(os.path.dirname(i18n.__file__))
    found: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(dirpath, filename), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                if isinstance(func, ast.Name):
                    name = func.id
                arg = node.args[0]
                if (
                    name in ("_", "tr")
                    and isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                ):
                    found.add(arg.value)
    return found


class CatalogCoversSource(unittest.TestCase):
    def test_every_translatable_string_in_source_is_in_each_catalog(self) -> None:
        used = _translatable_msgids()
        self.assertIn("In use now", used)  # the scan itself works
        for name in ("gpu-switcher-en.po", "gpu-switcher-ru.po"):
            known = {
                _po_text(msgid)  # the parser keeps .po escapes
                for msgid, _msgstr, _fuzzy in _po_entries(_po(name))
            }
            missing = sorted(used - known)
            self.assertEqual(missing, [], f"{name}: strings missing from catalog")


if __name__ == "__main__":
    unittest.main()
