"""The dialogs call a BooleanSelector a switch, which is how Home Assistant shows it.

Texts that tell people to tick a box or set a check mark do not match what they
see. The texts that refer to a confirmation switch are checked in every language.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
LANGUAGES = sorted(path.stem for path in (COMPONENT / "translations").glob("*.json"))

# The texts in the config and options flows that point at a confirmation switch.
KEYS = (
    ("error", "local_unlock_confirm_required"),
    ("error", "local_certificate_unconfirmed"),
    ("error", "local_token_end_unconfirmed"),
    ("error", "connection_remove_unconfirmed"),
    ("step", "delete_holiday", "description"),
    ("step", "connection_remove", "description"),
    ("step", "local_token_revoke", "description"),
)
# The words of a checkbox, by language: what the old texts used.
OLD_WORDS = {
    "en": r"\btick|untick|\bbox\b|unchecked|checkbox",
    "de": r"Häkchen|Kästchen|ankreuz",
    "bg": r"отметк",
    "cs": r"zaškrt|políčk",
    "da": r"kryds|flueben|uafkrydset|umarkeret",
    "el": r"πλαίσι|σήμανση",
    "es": r"casilla|sin marcar",
    "fi": r"ruutu|ruudu|valintaruut",
    "fr": r"\bcases?\b|cocher|décoch",
    "ga": r"\btic\b|ticbhosca|bhosca",
    "hr": r"okvir|kvačic|neoznačen",
    "it": r"casella|deselezionat|spunta",
    "nb": r"boks|hake|kryss",
    "nl": r"vakje|vink|uitgevinkt",
    "pl": r"zaznacz|niezaznaczon|pole\b|polem\b",
    "pt": r"caixa(?! de diálogo)|desmarcad|assinale|marque",
    "sk": r"políčk|zaškrt|začiark",
    "sl": r"neoznačen|polje\b|poljem\b|označite",
    "sv": r"rutan|kryssa|avmarker",
}


def _text(data: dict, section: str, key: tuple[str, ...]) -> str:
    value = data[section]
    for part in key:
        value = value[part]
    return value


def test_every_language_is_checked() -> None:
    assert set(OLD_WORDS) == set(LANGUAGES)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_texts_speak_of_a_switch_not_a_checkbox(language: str) -> None:
    data = json.loads(
        (COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )
    pattern = re.compile(OLD_WORDS[language], re.IGNORECASE)
    checked = 0
    for section in ("config", "options"):
        for key in KEYS:
            try:
                text = _text(data, section, key)
            except KeyError:
                # Some texts exist in one flow only.
                continue
            checked += 1
            assert not pattern.search(text), (language, section, key, text)
    # Both flows share two error texts; the other five belong to the options flow.
    assert checked == 9


def test_the_first_switch_text_is_the_same_in_both_flows() -> None:
    for language in LANGUAGES:
        data = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text(
                encoding="utf-8"
            )
        )
        for name in ("local_unlock_confirm_required", "local_certificate_unconfirmed"):
            assert data["config"]["error"][name] == data["options"]["error"][name]
