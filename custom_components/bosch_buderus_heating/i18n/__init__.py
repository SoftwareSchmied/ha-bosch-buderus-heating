"""Language of the texts the integration builds itself.

Home Assistant translates the dialogs and the entity names with a
translation key from ``translations/<code>.json``. Names composed at runtime,
notification texts and status lines are built here instead. German and
English stay next to the code that uses them; every other language has a
catalog in this package, keyed by the English text, so a missing entry falls
back to English. See ``docs/translations.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from . import da, fi, nb, sv

# The languages with a complete set of texts. Any other language shows English.
LANGUAGES: Final = ("en", "de", "sv", "nb", "fi", "da")
# Tags that name a supported language differently.
_ALIASES: Final = MappingProxyType({"no": "nb"})
CATALOGS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"sv": sv.TEXTS, "nb": nb.TEXTS, "fi": fi.TEXTS, "da": da.TEXTS}
)


def language_code(language: object) -> str:
    """Supported base language of a language tag such as ``sv-FI``, else ``en``."""
    tag = str(language or "").strip().casefold().replace("_", "-")
    base = tag.split("-", 1)[0]
    base = _ALIASES.get(base, base)
    return base if base in LANGUAGES else "en"


def localize(language: object, german: str, english: str) -> str:
    """The text in the given language, falling back to English."""
    code = language_code(language)
    if code == "de":
        return german
    if code == "en":
        return english
    return CATALOGS[code].get(english, english)


def localize_all(
    language: object, german: Mapping[str, str], english: Mapping[str, str]
) -> dict[str, str]:
    """A whole text table in the given language, keyed like the English one."""
    return {
        key: localize(language, german.get(key, text), text)
        for key, text in english.items()
    }
