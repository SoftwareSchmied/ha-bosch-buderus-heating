"""The wait advice of the read check names no fixed duration.

A gateway collecting its system information needed about nine minutes in a
real case, so the text says "several minutes" in every language.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
TRANSLATIONS = sorted((COMPONENT / "translations").glob("*.json"))
# The earlier wording of each language: "about five minutes".
EARLIER = (
    "about five minutes",
    "etwa fünf Minuten",
    "около пет минути",
    "asi pět minut",
    "cirka fem minutter",
    "περίπου πέντε λεπτά",
    "unos cinco minutos",
    "noin viisi minuuttia",
    "environ cinq minutes",
    "thart ar chúig nóiméad",
    "oko pet minuta",
    "circa cinque minuti",
    "omtrent fem minutter",
    "ongeveer vijf minuten",
    "około pięciu minut",
    "cerca de cinco minutos",
    "asi päť minút",
    "približno pet minut",
    "ungefär fem minuter",
)


def _texts(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        data[section]["error"]["local_incomplete_system"]
        for section in ("config", "options")
    ]


def test_all_19_languages_are_checked() -> None:
    assert len(TRANSLATIONS) == 19


@pytest.mark.parametrize("path", [*TRANSLATIONS, COMPONENT / "strings.json"])
def test_no_fixed_duration_is_named(path: Path) -> None:
    for text in _texts(path):
        assert not any(earlier in text for earlier in EARLIER), (path.name, text)


def test_english_and_german_say_several_minutes() -> None:
    for text in _texts(COMPONENT / "translations" / "en.json"):
        assert "Wait several minutes, then" in text
    for text in _texts(COMPONENT / "translations" / "de.json"):
        assert "Warte einige Minuten und" in text
