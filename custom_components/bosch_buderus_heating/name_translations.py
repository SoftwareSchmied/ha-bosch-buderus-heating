"""Parts of names the integration composes, from its translation files.

Most entity names are composed at runtime (for example "Heating circuit 1, supply
temperature" or a statistic's name), so Home Assistant cannot
translate them with an entity translation key alone. The parts listed here
are nevertheless kept as translation keys in ``strings.json`` and
``translations/<code>.json`` (``entity.sensor.<key>.name``), in every
language, German and English included, instead of German and English text
next to the code. They are read once on import, which Home Assistant does
outside the event loop, and follow the Home Assistant system language like
every other composed name. A missing language falls back to English.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .i18n import resolve_language

_DIRECTORY: Final = Path(__file__).with_name("translations")
# The keys of the composed name parts; every one of them starts like this.
PREFIXES: Final = ("name_part_",)


def _names(path: Path) -> Mapping[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sensors = data.get("entity", {}).get("sensor", {})
    return MappingProxyType(
        {
            key: entry["name"]
            for key, entry in sensors.items()
            if key.startswith(PREFIXES) and isinstance(entry.get("name"), str)
        }
    )


def load_names(directory: Path = _DIRECTORY) -> Mapping[str, Mapping[str, str]]:
    """The name parts of every translation file of a directory, by language."""
    return MappingProxyType(
        {path.stem: _names(path) for path in sorted(directory.glob("*.json"))}
    )


NAMES: Final = load_names()
LANGUAGES: Final = tuple(NAMES)


def name_part(language: object, key: str, **placeholders: object) -> str:
    """The name part ``key`` in the given language, English as fallback."""
    code = resolve_language(language, LANGUAGES)
    text = NAMES.get(code, {}).get(key) or NAMES["en"][key]
    return text.format(**placeholders) if placeholders else text
