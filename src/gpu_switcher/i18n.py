"""gettext setup.

Catalogs are looked up in the installed location, then in the source tree;
without one the strings stay English (identity).

gettext picks the language from the environment (LANGUAGE, then LC_ALL /
LC_MESSAGES / LANG). _requested_language() returns the same first preference
as a single code, for the locale argument of the helper calls.
"""

import gettext
import os
from collections.abc import Callable

APP_NAME = "gpu-switcher"

# One locale's translation function: text in, text out. The app uses the one for
# its own environment; the helper builds one per call from the locale the caller
# supplies and passes it to the reply composition.
type Translator = Callable[[str], str]

# The catalog directory an installed system ships. A source checkout has none,
# so localedirs() also consults the tree next to this module — the helper uses
# the same lookup.
INSTALLED_LOCALE_DIR = "/usr/share/gpu-switcher/locale"


def localedirs() -> list[str]:
    """Catalog directories to consult: the installed tree, then this checkout's.

    Only these are ever consulted, and a language is combined with them as a
    path component — so a language supplied by someone else must already be a
    plain code (the helper checks that in normalize_locale).
    """
    dirs = [INSTALLED_LOCALE_DIR]
    src = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "locale")
    )
    if os.path.isdir(src):
        dirs.append(src)
    return dirs


def _requested_language() -> str:
    """The first preferred language as a single code.

    Follows gettext's order (LANGUAGE, then LC_ALL / LC_MESSAGES / LANG);
    "en" when none is set. The app passes it to the helper so the helper's
    replies are localized.
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


def _make_gettext() -> Translator:
    # languages=None: gettext resolves the language from the environment.
    # Try each directory in turn, then fall back to identity (English).
    for localedir in localedirs():
        try:
            return gettext.translation(
                APP_NAME, localedir, languages=None, fallback=False
            ).gettext
        except OSError:
            continue
    return gettext.translation(APP_NAME, None, languages=["en"], fallback=True).gettext


_ = _make_gettext()
