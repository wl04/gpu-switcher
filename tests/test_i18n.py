"""Unit tests for the gettext setup in i18n.py.

Pins the locale variable precedence (LC_ALL > LC_MESSAGES > LANG > en)
and the catalog search order (installed location before the source tree).
"""
import os
import unittest
from contextlib import contextmanager

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
    def test_RequestedLanguage_PrefersLcAllOverLcMessagesAndLang(self):
        with _locale_env(LC_ALL="ru_RU.UTF-8", LC_MESSAGES="de_DE.UTF-8",
                         LANG="en_US.UTF-8"):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_RequestedLanguage_UsesLcMessages_WhenLcAllUnset(self):
        with _locale_env(LC_ALL=None, LC_MESSAGES="ru_RU.UTF-8",
                         LANG="en_US.UTF-8"):
            self.assertEqual(i18n._requested_language(), "ru_RU.UTF-8")

    def test_RequestedLanguage_FallsBackToEnglish_WhenNoLocaleSet(self):
        with _locale_env(LC_ALL=None, LC_MESSAGES=None, LANG=None):
            self.assertEqual(i18n._requested_language(), "en")


class Localedirs(unittest.TestCase):
    def test_Localedirs_ListsInstalledDirBeforeSourceTree(self):
        installed = "/usr/share/gpu-switcher/locale"
        src = os.path.abspath(os.path.join(os.path.dirname(i18n.__file__),
                                           "..", "..", "data", "locale"))
        dirs = i18n._localedirs()
        self.assertEqual(dirs[0], installed)
        if os.path.isdir(src):
            self.assertEqual(dirs, [installed, src])
        else:
            self.assertEqual(len(dirs), 1)


if __name__ == "__main__":
    unittest.main()
