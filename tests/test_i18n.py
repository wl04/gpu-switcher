"""Unit tests for the gettext setup in i18n.py.

Pins the locale variable precedence (LC_ALL > LC_MESSAGES > LANG > en),
the catalog search order (installed location before the source tree), and
the reference-compiler guards: catalog consistency (`msgfmt -c`) and
readability of a msgfmt-built catalog by CPython's gettext.
"""
import gettext
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager

# gpu_switcher.i18n binds its gettext function at import time, so the
# locale must be pinned before the app import: the assertions below
# compare English catalog strings and must hold under any developer
# locale.
os.environ["LC_ALL"] = "en"

from gpu_switcher import i18n


@contextmanager
def _locale_env(**variables):
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
    def test_requested_language_prefers_lc_all_over_lc_messages_and_lang(self):
        with _locale_env(LC_ALL="ru_RU.UTF-8", LC_MESSAGES="de_DE.UTF-8",
                         LANG="en_US.UTF-8"):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_requested_language_uses_lc_messages_when_lc_all_unset(self):
        with _locale_env(LC_ALL=None, LC_MESSAGES="ru_RU.UTF-8",
                         LANG="en_US.UTF-8"):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_requested_language_falls_back_to_english_when_no_locale_set(self):
        with _locale_env(LC_ALL=None, LC_MESSAGES=None, LANG=None):
            self.assertEqual(i18n._requested_language(), "en")


class Localedirs(unittest.TestCase):
    def test_localedirs_lists_installed_dir_before_source_tree(self):
        installed = "/usr/share/gpu-switcher/locale"
        src = os.path.abspath(os.path.join(os.path.dirname(i18n.__file__),
                                           "..", "..", "data", "locale"))
        dirs = i18n._localedirs()
        self.assertEqual(dirs[0], installed)
        if os.path.isdir(src):
            self.assertEqual(dirs, [installed, src])
        else:
            self.assertEqual(len(dirs), 1)


class MsgfmtBuild(unittest.TestCase):
    """Catalog build guard: the tracked catalogs must pass the reference
    compiler's consistency check, and a msgfmt-built catalog must be
    readable by CPython's gettext."""

    def setUp(self):
        self._msgfmt = shutil.which("msgfmt")
        if self._msgfmt is None:
            self.skipTest("msgfmt (gettext-tools) is not installed")

    def _po(self, name: str) -> str:
        return os.path.join(os.path.dirname(i18n.__file__), "..", "..",
                            "po", name)

    def test_msgfmt_checks_catalogs_are_consistent(self):
        # One msgfmt invocation per catalog: multiple .po files would be
        # parsed as one merged catalog and every msgid would duplicate.
        # -o /dev/null: GNU msgfmt's -c still writes the default
        # messages.mo to the CWD without an explicit output file.
        for name in ("gpu-switcher-en.po", "gpu-switcher-ru.po"):
            out = subprocess.run([self._msgfmt, "-c", "-o", "/dev/null",
                                 self._po(name)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_msgfmt_built_catalog_is_readable_by_gettext(self):
        ru_po = self._po("gpu-switcher-ru.po")
        with tempfile.TemporaryDirectory() as tmp:
            mo_path = os.path.join(tmp, "LC_MESSAGES", "gpu-switcher.mo")
            os.makedirs(os.path.dirname(mo_path))
            out = subprocess.run([self._msgfmt, "-o", mo_path, ru_po],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(mo_path, "rb") as f:
                catalog = gettext.GNUTranslations(f)
            self.assertEqual(catalog.gettext("In use now"),
                             "Используется сейчас")
            self.assertEqual(catalog.gettext("Log out"), "Выйти")


if __name__ == "__main__":
    unittest.main()
