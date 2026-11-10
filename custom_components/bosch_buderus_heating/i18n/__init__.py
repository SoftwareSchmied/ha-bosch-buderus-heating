"""Language of the texts the integration builds itself.

Home Assistant translates the dialogs and the entity names with a
translation key from ``translations/<code>.json``. Names composed at runtime,
notification texts and status lines are built here instead. German and
English stay next to the code that uses them. Every language has a data file
``i18n/<code>.json`` here: ``dialogs`` holds the texts of the controls and
holiday dialogs, and ``texts`` the translations of the built texts, keyed by
the English text. A missing entry falls back to English, so adding a
language only means adding its data files. See ``docs/translations.md``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

# The source languages, maintained next to the code.
SOURCE_LANGUAGES: Final = ("en", "de")
# Tags that name a language with a data file differently. Nynorsk readers get
# Bokmål rather than English.
ALIASES: Final[Mapping[str, str]] = MappingProxyType({"no": "nb", "nn": "nb"})


@dataclass(frozen=True, slots=True)
class Catalogs:
    """The languages found in one directory of data files."""

    languages: tuple[str, ...]
    texts: Mapping[str, Mapping[str, str]]
    dialogs: Mapping[str, Mapping[str, Mapping[str, str]]]


def load_catalogs(directory: Path) -> Catalogs:
    """Read every ``<code>.json`` data file of a directory."""
    texts: dict[str, Mapping[str, str]] = {}
    dialogs: dict[str, Mapping[str, Mapping[str, str]]] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        code = path.stem
        dialogs[code] = MappingProxyType(
            {
                name: MappingProxyType(dict(entries))
                for name, entries in data.get("dialogs", {}).items()
            }
        )
        if code not in SOURCE_LANGUAGES:
            texts[code] = MappingProxyType(dict(data.get("texts", {})))
    others = sorted(code for code in dialogs if code not in SOURCE_LANGUAGES)
    return Catalogs(
        (*SOURCE_LANGUAGES, *others),
        MappingProxyType(texts),
        MappingProxyType(dialogs),
    )


# Read once when the integration is imported, which Home Assistant does
# outside the event loop.
_CATALOGS: Final = load_catalogs(Path(__file__).parent)
LANGUAGES: Final = _CATALOGS.languages
CATALOGS: Final = _CATALOGS.texts


def resolve_language(language: object, languages: tuple[str, ...]) -> str:
    """The best of ``languages`` for a tag such as ``sv-FI`` or ``pt_BR``."""
    tag = str(language or "").strip().casefold().replace("_", "-")
    base = tag.split("-", 1)[0]
    known = {code.casefold(): code for code in languages}
    for candidate in (tag, ALIASES.get(tag), base, ALIASES.get(base)):
        if candidate and candidate in known:
            return known[candidate]
    return "en"


def language_code(language: object) -> str:
    """Supported language of a language tag, ``en`` if there is none."""
    return resolve_language(language, LANGUAGES)


def localize(language: object, german: str, english: str) -> str:
    """The text in the given language, falling back to English."""
    code = language_code(language)
    if code == "de":
        return german
    return CATALOGS.get(code, {}).get(english, english)


def localize_all(
    language: object, german: Mapping[str, str], english: Mapping[str, str]
) -> dict[str, str]:
    """A whole text table in the given language, keyed like the English one."""
    return {
        key: localize(language, german.get(key, text), text)
        for key, text in english.items()
    }


def dialog_config(name: str) -> dict[str, object]:
    """Texts of one dialog in every language, for the browser to choose from."""
    return {
        "texts": {
            code: dict(dialogs[name])
            for code, dialogs in _CATALOGS.dialogs.items()
            if name in dialogs
        },
        "aliases": dict(ALIASES),
    }
