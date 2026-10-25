"""gettext setup.

Looks for compiled catalogs in the installed location first, then in the
source tree, and falls back to English (identity) when no catalog exists.
"""
import gettext
import os

APP_NAME = "gpu-switcher"


def _localedirs():
    dirs = ["/usr/share/gpu-switcher/locale"]
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "locale"))
    if os.path.isdir(src):
        dirs.append(src)
    return dirs


def _requested_language():
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value
    return "en"


def _make_gettext():
    # gettext.translation takes a single directory string, so try each search
    # directory in turn (installed location first, then the source tree) and
    # fall back to English (identity) when no catalog exists.
    langs = [_requested_language(), "en"]
    for localedir in _localedirs():
        try:
            return gettext.translation(APP_NAME, localedir, languages=langs,
                                      fallback=False).gettext
        except OSError:
            continue
    return gettext.translation(APP_NAME, None, languages=["en"],
                              fallback=True).gettext


_ = _make_gettext()
