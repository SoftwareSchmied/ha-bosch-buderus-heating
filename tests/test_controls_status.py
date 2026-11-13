"""D53: the controls dialog confirms only what it actually wrote."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_heating_controls import TARGET, flow, selector

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"


@pytest.mark.parametrize("changes", [{}, {TARGET: 19.0}])
async def test_apply_without_a_change_writes_nothing_and_says_so(
    hass, enable_custom_integrations, changes
):
    options, coordinator, _entity, _ = await flow(hass)
    current = coordinator.data[TARGET].resource.value
    changes = {TARGET: current} if changes else {}
    result = await options.async_step_operate({"controls": {"changes": changes}})
    assert result["step_id"] == "operate" and not result["errors"]
    assert selector(result).status == "unchanged"
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_a_written_change_is_still_confirmed(hass, enable_custom_integrations):
    options, coordinator, _entity, _ = await flow(hass)
    result = await options.async_step_operate({"controls": {"changes": {TARGET: 20}}})
    assert selector(result).status == "saved"
    coordinator._write_service.async_write_number.assert_awaited_once()


def test_the_dialog_texts_have_a_singular_and_an_unchanged_status() -> None:
    for path in sorted((PACKAGE / "i18n").glob("*.json")):
        dialogs = json.loads(path.read_text("utf-8"))["dialogs"]
        controls = dialogs["controls"]
        assert "{count}" in controls["unsavedOne"], path.stem
        assert controls["unchanged"].strip(), path.stem
        assert dialogs["holiday"]["reopen"] == controls["reopen"], path.stem
    english = json.loads((PACKAGE / "i18n" / "en.json").read_text("utf-8"))
    assert english["dialogs"]["controls"]["unsavedOne"] == "{count} unsaved change"
    german = json.loads((PACKAGE / "i18n" / "de.json").read_text("utf-8"))
    assert german["dialogs"]["controls"]["unsavedOne"] == (
        "{count} ungespeicherte Änderung"
    )


def test_the_scripts_pick_the_singular_and_name_a_lost_flow() -> None:
    controls = (PACKAGE / "www" / "controls-dialog.js").read_text("utf-8")
    assert "Intl.PluralRules" in controls
    assert re.search(r'status === "unchanged"', controls)
    assert re.search(r'flowGone\(err\)\s*\?\s*this\._text\("reopen"\)', controls)
    holiday = (PACKAGE / "www" / "holiday-dialog.js").read_text("utf-8")
    assert re.search(r'status_code === 404[^;]*"reopen"', holiday, re.S)
