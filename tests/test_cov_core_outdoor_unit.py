"""Outdoor unit software sensor: restore, refresh, source choice and the
failure shapes of the module lists (0.9.2, N07)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import outdoor_unit_software as ous
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.outdoor_unit_software import (
    MAX_OUTDOOR_UNITS,
    OutdoorUnitInputs,
    from_cloud,
    from_data,
    from_local,
    outdoor_unit_software_unique_id,
    track_outdoor_unit_software_sensor,
)
from tests.ha_test_helpers import mock_restore_cache_with_extra_data
from tests.test_heat_generator import (
    GATEWAY,
    NOW,
    OUTDOOR,
    OWN_CLOUD,
    _platform,
    coordinator_for,
    local_module,
    local_system,
    set_cloud,
    shown,
)

UNIQUE_ID = outdoor_unit_software_unique_id(GATEWAY)
OUTDOOR_ROW = {
    "ModuleSerialNumber": "SERIAL-ODU",
    "Tok": "8",
    "Id": "65281",
    "Ver": "9.16.0-dab9f463",
    "ProductName": OUTDOOR,
    "ModuleHwIdentStr": "XCU_SRH",
}


def _tracked(coordinator) -> list:
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_outdoor_unit_software_sensor(entry, coordinator, added.extend)
    return added


async def _added(hass, coordinator):
    (entity,) = _tracked(coordinator)
    await _platform(hass, coordinator.config_entry).async_add_entities([entity])
    await hass.async_block_till_done()
    return entity


def _register(hass, coordinator) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor", DOMAIN, UNIQUE_ID, config_entry=coordinator.config_entry
        )
        .entity_id
    )


def _local_row(version="9.16.0-dab9f463", **extra):
    module = local_module(OUTDOOR, "XCU_SRH")
    module["Ver"] = version
    module.update(extra)
    return module


# ----------------------------------------------------------- data shapes


def test_a_cloud_list_is_bounded_in_rows_and_units() -> None:
    many = [
        {**OUTDOOR_ROW, "Ver": f"9.{number}.0", "ProductName": f"WLW-10 MB AR {number}"}
        for number in range(MAX_OUTDOOR_UNITS + 4)
    ]
    units = from_cloud(many)
    assert units is not None
    assert len(units.units) == MAX_OUTDOOR_UNITS
    # Rows past the row limit are never looked at, so an outdoor unit that
    # only appears there is not found.
    late = [{"ModuleHwIdentStr": "X", "Ver": "1"}] * ous.MAX_ROWS + [OUTDOOR_ROW]
    assert from_cloud(late) is None


def test_a_local_list_is_bounded_in_rows() -> None:
    other = SimpleNamespace(
        product_name="Other", hardware="HW", version="1", serial_number="1"
    )
    late = [other] * ous.MAX_ROWS + [
        SimpleNamespace(
            product_name=OUTDOOR,
            hardware="XCU_SRH",
            version="9.16.0",
            serial_number="1",
            product_serial=None,
            token="t",
        )
    ]
    assert from_local(late) is None


def test_the_local_list_never_shows_a_serial_as_version() -> None:
    module = SimpleNamespace(
        product_name=OUTDOOR,
        hardware="XCU_SRH",
        version="SN-12345",
        serial_number="SN-12345",
        product_serial=None,
        token="t",
    )
    assert from_local([module]) is None


def test_a_name_that_is_an_identifier_or_a_number_is_not_used() -> None:
    for name in ("12345678901", "SERIAL-ODU"):
        units = from_cloud([{**OUTDOOR_ROW, "ProductName": name}])
        # No usable name: the hardware type names the unit.
        assert units is not None
        assert units.units[0].product_name == "XCU_SRH"


@pytest.mark.parametrize(
    "data",
    [
        {"source": "cloud", "units": "text"},
        {"source": "cloud", "units": ["not a list"]},
        {"source": "cloud", "units": [["a", "XCU_SRH", "9.1"], ["b", "c"]]},
        {"source": "cloud", "units": [["", "XCU_SRH", "9.1"]]},
        {"source": "cloud", "units": [[OUTDOOR, "", "9.1"]]},
        {"source": "cloud", "units": [[OUTDOOR, "XCU_SRH", "no digit"]]},
        {"source": "cloud", "units": []},
        [],
    ],
)
def test_faulty_kept_data_is_ignored(data) -> None:
    assert from_data(data) is None


def test_kept_data_is_bounded_and_keeps_its_source() -> None:
    rows = [[f"Unit {n}", "XCU_SRH", f"9.{n}"] for n in range(MAX_OUTDOOR_UNITS + 3)]
    units = from_data({"source": "local", "units": rows})
    assert units is not None
    assert units.source == "local"
    assert len(units.units) == MAX_OUTDOOR_UNITS
    assert units.observed_at is None


def test_the_stored_wrapper_survives_a_round_trip_and_garbage() -> None:
    stored = ous._Stored({"source": "cloud", "units": []})
    assert stored.as_dict() == {"outdoor_units": {"source": "cloud", "units": []}}
    assert ous._Stored.from_dict(stored.as_dict()) == stored
    assert ous._Stored.from_dict({"outdoor_units": "bad"}).data is None
    assert ous._Stored.from_dict({}).data is None


# ----------------------------------------------------------- the inputs


async def test_the_local_input_takes_the_newest_completed_module_list(
    hass: HomeAssistant,
) -> None:
    older = local_system([_local_row("9.15.0")], observed_at=NOW - timedelta(hours=1))
    newer = local_system([_local_row("9.16.0")], observed_at=NOW)
    pending = local_system([_local_row("9.99.9")], status="pending")
    other = SimpleNamespace(
        request=SimpleNamespace(path="/heatSources/x"), data=None, observed_at=NOW
    )
    coordinator = coordinator_for(hass, local=[older, pending, newer, other])
    units = OutdoorUnitInputs(coordinator).local()
    assert units is not None
    assert units.state == "9.16.0"
    assert units.source == "local"
    assert units.observed_at == NOW
    await coordinator.async_shutdown()


async def test_no_local_answer_gives_no_local_input(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, local=[])
    assert OutdoorUnitInputs(coordinator).local() is None
    coordinator.local_selection = None
    assert OutdoorUnitInputs(coordinator).local() is None
    await coordinator.async_shutdown()


async def test_a_binding_without_a_state_is_skipped(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, local=[])
    coordinator.local_selection = SimpleNamespace(local_states=lambda: (None,))
    assert OutdoorUnitInputs(coordinator).local() is None
    await coordinator.async_shutdown()


async def test_the_newer_of_cloud_and_local_wins(hass: HomeAssistant) -> None:
    cloud_rows = [{**OUTDOOR_ROW, "Ver": "9.10.0"}]
    local = local_system([_local_row("9.20.0")], observed_at=NOW + timedelta(hours=1))
    coordinator = coordinator_for(hass, cloud_rows, local=[local])
    current = OutdoorUnitInputs(coordinator).current()
    assert current is not None
    assert (current.source, current.state) == ("local", "9.20.0")
    # A cloud answer received later wins.
    set_cloud(coordinator, cloud_rows, NOW + timedelta(hours=2))
    current = OutdoorUnitInputs(coordinator).current()
    assert current is not None
    assert (current.source, current.state) == ("cloud", "9.10.0")
    await coordinator.async_shutdown()


# ----------------------------------------------------------- the entity


async def test_the_entity_follows_the_answers_and_keeps_the_last_version(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    entity = await _added(hass, coordinator)
    state = shown(hass, entity)
    assert state.state == "9.16.0-dab9f463"
    assert state.attributes["product_name"] == OUTDOOR
    assert state.attributes["source"] == "cloud"
    # An unchanged answer writes nothing new.
    assert entity._refresh() is False
    # A new version is taken at the next update.
    changed = [{**OUTDOOR_ROW, "Ver": "9.17.1"}]
    set_cloud(coordinator, changed, NOW + timedelta(hours=1))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert shown(hass, entity).state == "9.17.1"
    # Without a current answer the last version stays and the sensor is
    # available; it never asks for anything itself.
    set_cloud(coordinator, [], NOW + timedelta(hours=2))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert shown(hass, entity).state == "9.17.1"
    assert entity.available is True
    await entity.async_update()
    assert entity.extra_restore_state_data.as_dict() == {
        "outdoor_units": {
            "units": [[OUTDOOR, "XCU_SRH", "9.17.1"]],
            "source": "cloud",
        }
    }
    await coordinator.async_shutdown()


async def test_a_registered_entity_without_an_answer_is_unknown(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_for(hass)
    _register(hass, coordinator)
    entity = await _added(hass, coordinator)
    assert shown(hass, entity).state == "unknown"
    assert entity.native_value is None
    assert entity.extra_state_attributes is None
    assert entity.extra_restore_state_data.as_dict() == {"outdoor_units": None}
    await coordinator.async_shutdown()


async def test_the_kept_version_is_restored_after_a_restart(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_for(hass)
    entity_id = _register(hass, coordinator)
    kept = {
        "outdoor_units": {
            "units": [[OUTDOOR, "XCU_SRH", "9.16.0-dab9f463"]],
            "source": "cloud",
        }
    }
    mock_restore_cache_with_extra_data(hass, [(State(entity_id, "9.16.0"), kept)])
    entity = await _added(hass, coordinator)
    state = shown(hass, entity)
    assert state.state == "9.16.0-dab9f463"
    assert state.attributes["source"] == "cloud"
    await coordinator.async_shutdown()


async def test_a_damaged_kept_version_is_dropped(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass)
    entity_id = _register(hass, coordinator)
    mock_restore_cache_with_extra_data(
        hass,
        [(State(entity_id, "x"), {"outdoor_units": {"source": "cloud", "units": 5}})],
    )
    entity = await _added(hass, coordinator)
    assert shown(hass, entity).state == "unknown"
    await coordinator.async_shutdown()


async def test_the_device_is_the_system_device(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    (entity,) = _tracked(coordinator)
    info = entity.device_info
    assert info is not None
    assert any(GATEWAY in str(value) for value in info.get("identifiers", ()))
    await coordinator.async_shutdown()


# ------------------------------------------------------------- tracking


async def test_no_entity_without_a_valid_model_table(
    hass: HomeAssistant, monkeypatch
) -> None:
    monkeypatch.setattr(ous.heat_generator, "TABLE_AVAILABLE", False)
    coordinator = coordinator_for(hass, OWN_CLOUD)
    assert _tracked(coordinator) == []
    await coordinator.async_shutdown()


async def test_a_room_controller_never_gets_the_sensor(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    with patch.object(ous, "is_rrc_plant", return_value=True):
        assert _tracked(coordinator) == []
    await coordinator.async_shutdown()


async def test_the_entity_appears_with_the_first_outdoor_unit(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_for(hass, [])
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_outdoor_unit_software_sensor(entry, coordinator, added.extend)
    assert added == []
    entry.async_on_unload.assert_called_once()
    set_cloud(coordinator, OWN_CLOUD)
    coordinator.async_update_listeners()
    assert len(added) == 1
    # Only once.
    coordinator.async_update_listeners()
    assert len(added) == 1
    await coordinator.async_shutdown()
