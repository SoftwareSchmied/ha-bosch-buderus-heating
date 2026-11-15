"""Temperature levels of a heating circuit (0.9.2).

The heating and setback levels (since 0.9.0) gain the level rules and the
coupled limits; the away temperature and the cooling level are new. Only
these four level names are operated, as in the manufacturer's app. The
shapes follow ``fixtures/contracts/icom/temperature-levels.json``; every
value is constructed. Nothing here confirms behaviour on a real
installation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.circuit_control_writes import (
    circuit_control_diagnostics,
)
from custom_components.bosch_buderus_heating.circuit_controls import (
    AWAY_TEMPERATURE_POLICY,
    COOLING_LEVEL_POLICY,
    HEATING_LEVEL_POLICY,
    unknown_level_count,
)
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceError,
    ResourceNotFound,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.circuit_control_helpers import (
    AWAY,
    AWAY_LEVEL,
    COMFORT,
    CONTROL_TYPE,
    COOLING_LEVEL,
    ECO,
    ROOT,
    Cloud,
    coordinator_for,
    fixture,
    number_entity,
    number_paths,
    put_reading,
    variant,
    with_flags,
)

FIXTURE = "icom/temperature-levels.json"
COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
LEVEL_GUARDS = {COMFORT, ECO, CONTROL_TYPE}


def _setup(hass, key: str = "V1"):
    coordinator = coordinator_for(hass, variant(FIXTURE, key))
    return coordinator, Cloud(coordinator)


# Away temperature and cooling level ---------------------------------------------


@pytest.mark.parametrize(("key", "path"), [("V2", AWAY), ("V2b", AWAY_LEVEL)])
async def test_away_temperature_writes_once(hass, key, path):
    coordinator, cloud = _setup(hass, key)
    entity = number_entity(hass, coordinator, path)
    description = entity.entity_description
    assert description.write_policy is AWAY_TEMPERATURE_POLICY
    assert description.translation_key == "away_temperature"
    assert description.entity_registry_enabled_default
    assert description.entity_category is EntityCategory.CONFIG
    await entity.async_set_native_value(16)
    assert cloud.puts == [(path, 16.0)]
    # The other levels and the control type were read again with it.
    assert set(cloud.bulk_calls[0]) == {path, *LEVEL_GUARDS}
    assert entity.native_value == 16


async def test_cooling_level_writes_once(hass):
    coordinator, cloud = _setup(hass, "V3")
    entity = number_entity(hass, coordinator, COOLING_LEVEL)
    assert entity.entity_description.write_policy is COOLING_LEVEL_POLICY
    assert entity.entity_description.translation_key == "cooling_temperature"
    assert not entity.entity_description.entity_registry_enabled_default
    await entity.async_set_native_value(23.5)
    assert cloud.puts == [(COOLING_LEVEL, 23.5)]
    assert set(cloud.bulk_calls[0]) == {COOLING_LEVEL, CONTROL_TYPE}


def test_unknown_level_stays_a_reading_and_is_counted(hass):
    resources = variant(FIXTURE, "V4")
    party = ROOT + "/temperatureLevels/party"
    assert assess_control(resources[party]).rejection_reason == (
        "no_scalar_control_policy"
    )
    assert party not in number_paths(resources)
    sensors = {item.key for item in build_sensor_descriptions(resources)}
    assert any("party" in key for key in sensors)
    assert unknown_level_count(resources) == 1
    coordinator = coordinator_for(hass, resources)
    report = circuit_control_diagnostics(coordinator)
    assert report["unknown_temperature_levels"] == 1
    assert "party" not in json.dumps(report)


def test_unknown_cooling_level_is_counted():
    assert unknown_level_count([ROOT + "/cooling/temperatureLevels/x", COOLING_LEVEL]) == 1
    assert unknown_level_count([COMFORT, ECO, AWAY_LEVEL, ROOT + "/x"]) == 0


# Coupled limits ---------------------------------------------------------------


async def test_setback_level_within_the_current_limits(hass):
    """eco 20 fits while the heating level is 21 (eco max 20.5)."""
    coordinator, cloud = _setup(hass)
    await number_entity(hass, coordinator, ECO).async_set_native_value(20)
    assert cloud.puts == [(ECO, 20.0)]
    assert set(cloud.bulk_calls[0]) == {ECO, *LEVEL_GUARDS}


async def test_changed_heating_level_refuses_the_setback_write(hass):
    """The heating level changed in the app: the limits of eco moved too."""
    coordinator, cloud = _setup(hass)
    cloud.change(COMFORT, value=20.0)
    cloud.change(ECO, metadata=replace(cloud.state[ECO].metadata, maximum=19.5))
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, ECO).async_set_native_value(20)
    assert error.value.translation_key == "write_validation_failed"
    assert cloud.puts == []
    # The fresh readings are shown at once.
    assert coordinator.data[COMFORT].resource.value == 20.0
    assert coordinator.data[ECO].resource.metadata.maximum == 19.5


@pytest.mark.parametrize(("path", "value"), [(ECO, 21), (COMFORT, 19)])
async def test_values_beyond_the_coupled_limits_send_nothing(hass, path, value):
    coordinator, cloud = _setup(hass)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(path, value, HEATING_LEVEL_POLICY)
    assert cloud.puts == []


async def test_without_step_the_away_grid_is_half_a_degree(hass):
    coordinator, cloud = _setup(hass, "V6")
    entity = number_entity(hass, coordinator, AWAY)
    assert entity.native_step == 0.5
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(16.3)
    await entity.async_set_native_value(16.5)
    assert cloud.puts == [(AWAY, 16.5)]


def test_level_without_a_value_has_no_entity():
    resources = variant(FIXTURE, "V8")
    assert not resources[AWAY].has_value
    assert AWAY not in number_paths(resources)


# Discovery ------------------------------------------------------------------------


def _client(tree: dict[str, object]) -> AsyncMock:
    client = AsyncMock()

    def answer(gateway, path):
        item = tree.get(path, 404)
        if isinstance(item, int):
            return BatchItemResult(gateway, path, item, error=ResourceError(path, item))
        return BatchItemResult(gateway, path, 200, item)

    client.get_resources_bulk.side_effect = lambda gateway, chunk: tuple(
        answer(gateway, path) for path in chunk
    )

    async def single(gateway, path, **_kwargs):
        item = tree.get(path, 404)
        if isinstance(item, int):
            if item >= 500:
                raise ServiceUnavailable(item)
            raise ResourceNotFound(path, item)
        return item

    client.get_resource.side_effect = single
    return client


async def test_a_missing_or_failing_level_leaves_the_others():
    """404 on one listed level, 5xx on another: the rest is discovered and
    the discovery completes (as the app skips a 404)."""
    data = fixture(FIXTURE)
    raw = {item["id"]: item for item in data["base"]}
    raw[ROOT + "/temperatureLevels"] = {
        "id": ROOT + "/temperatureLevels",
        "type": "refEnum",
        "references": [
            {"id": COMFORT},
            {"id": ECO},
            {"id": AWAY_LEVEL},
            {"id": ROOT + "/temperatureLevels/party"},
        ],
    }
    tree: dict[str, object] = {
        path: parse_resource(item) for path, item in raw.items()
    }
    tree[AWAY_LEVEL] = 404
    tree[ROOT + "/temperatureLevels/party"] = 503
    tree["/heatingCircuits"] = parse_resource(
        {"id": "/heatingCircuits", "type": "refEnum", "references": [{"id": ROOT}]}
    )
    tree[ROOT] = parse_resource(
        {
            "id": ROOT,
            "type": "refEnum",
            "references": [{"id": ROOT + "/temperatureLevels"}, {"id": CONTROL_TYPE}],
        }
    )
    report = DiscoveryDiagnostics()
    discovered = await async_discover_resources(
        _client(tree), "gateway", roots=("/heatingCircuits",), diagnostics=report
    )
    assert {COMFORT, ECO} <= set(discovered)
    assert AWAY_LEVEL not in discovered
    assert report.completed


# Room controller, constant values and flags ----------------------------------------


@pytest.mark.parametrize("path", [AWAY, COMFORT, ECO])
async def test_room_controller_refuses_every_level(hass, path):
    coordinator, cloud = _setup(hass, "V7")
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, path).async_set_native_value(20)
    assert error.value.translation_key == "circuit_room_controller"
    assert cloud.puts == []


@pytest.mark.parametrize("path", [AWAY, COMFORT])
async def test_constant_values_refuse_every_level(hass, path):
    coordinator, cloud = _setup(hass, "V2")
    constant = replace(coordinator.resources[CONTROL_TYPE], value="constants")
    put_reading(coordinator, constant)
    cloud.state[CONTROL_TYPE] = constant
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, path).async_set_native_value(21.5)
    assert error.value.translation_key == "circuit_constant_control"
    assert cloud.puts == []


async def test_unused_level_refuses_and_unavailable_has_no_entity(hass):
    resources = variant(FIXTURE, "V2")
    resources[AWAY] = with_flags(resources[AWAY], used=False, available=True)
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, AWAY).async_set_native_value(16)
    assert error.value.translation_key == "control_unused"
    assert cloud.puts == []
    resources[AWAY] = with_flags(resources[AWAY], used=True, available=False)
    assert AWAY not in number_paths(resources)


# Failure paths with real write cycles ---------------------------------------------


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ResourceNotFound(AWAY, 404), "write_failed"),
        (ServiceUnavailable(503), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
    ],
)
async def test_failed_write_is_never_repeated(hass, failure, key):
    coordinator, cloud = _setup(hass, "V2")
    cloud.put_error = failure
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, AWAY).async_set_native_value(16)
    assert error.value.translation_key == key
    assert cloud.puts == [(AWAY, 16.0)]


async def test_read_back_with_the_old_value_is_not_confirmed(hass):
    coordinator, cloud = _setup(hass, "V2")
    cloud.stuck[AWAY] = 15.0
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, AWAY).async_set_native_value(16)
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(AWAY, 16.0)]


async def test_read_back_timeout_is_not_confirmed(hass):
    coordinator, cloud = _setup(hass, "V2")
    cloud.readback_error = ServiceUnavailable(504)
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, AWAY).async_set_native_value(16)
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(AWAY, 16.0)]


# Existing heating and setback levels -------------------------------------------------


def test_heating_and_setback_levels_keep_their_identity(hass):
    coordinator, _ = _setup(hass)
    for path, key in ((COMFORT, "heating_temperature"), (ECO, "reduced_temperature")):
        entity = number_entity(hass, coordinator, path)
        description = entity.entity_description
        assert description.write_policy is HEATING_LEVEL_POLICY
        assert description.translation_key == key
        assert description.entity_category is EntityCategory.CONFIG
        assert description.entity_registry_enabled_default
        assert description.icon == "mdi:thermometer"
        suffix = path.strip("/").replace("/", ":")
        assert entity.unique_id == f"gateway:{suffix}:control"


# Translations ----------------------------------------------------------------------


def test_names_and_errors_in_every_language():
    for path in sorted((COMPONENT / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("away_temperature", "cooling_temperature"):
            assert data["entity"]["number"][key]["name"], (path.name, key)
        assert data["exceptions"]["circuit_constant_control"]["message"]
    for path in sorted((COMPONENT / "i18n").glob("*.json")):
        if path.stem in ("de", "en"):
            continue
        texts = json.loads(path.read_text(encoding="utf-8"))["texts"]
        assert texts["Cooling temperature"], path.name
