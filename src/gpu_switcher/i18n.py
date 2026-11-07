"""gettext setup.

Looks for compiled catalogs in the installed location first, then in the
source tree, and falls back to English (identity) when no catalog exists.

The active language is resolved by gettext itself: with languages=None it
honors the environment ordering (LANGUAGE, a colon-separated preference
list, first, then LC_ALL / LC_MESSAGES / LANG). _requested_language() reads
the same first preference as a single code, for callers (the helper's
polkit dialog) that need one locale string.
"""

import gettext
import os
from collections.abc import Callable

APP_NAME = "gpu-switcher"


def _localedirs() -> list[str]:
    dirs = ["/usr/share/gpu-switcher/locale"]
    src = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "locale")
    )
    if os.path.isdir(src):
        dirs.append(src)
    return dirs


def _requested_language() -> str:
    """The first preferred language as a single code.

    Honors LANGUAGE (colon-separated list) before LC_ALL / LC_MESSAGES /
    LANG, mirroring gettext's own ordering; "en" when none is set. This is
    the locale the app passes to the helper (localized polkit dialog).
    """
    language = os.environ.get("LANGUAGE")
    if language:
        for entry in language.split(":"):
            entry = entry.strip()
            if entry and entry != "C":
                return entry
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value
    return "en"


def _make_gettext() -> Callable[[str], str]:
    # languages=None lets gettext resolve the active language from the
    # environment (LANGUAGE, then LC_ALL / LC_MESSAGES / LANG). Try each
    # search directory in turn (installed location first, then the source
    # tree) and fall back to English (identity) when no catalog exists.
    for localedir in _localedirs():
        try:
            return gettext.translation(
                APP_NAME, localedir, languages=None, fallback=False
            ).gettext
        except OSError:
            continue
    return gettext.translation(APP_NAME, None, languages=["en"], fallback=True).gettext


_ = _make_gettext()
