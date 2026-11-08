"""Season settings use the gateway's own contract and confirmed writes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import _control_diagnostics
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    RateLimited,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.ha_test_helpers import MockConfigEntry

PATH = "/system/seasonOptimizer/mode"
STATUS_PATH = "/system/globalSeasonOptimizer/currentMode"
OPTIONS = ("off", "automatic", "forcedHeat")


def _resource(value: str = "off", options: tuple[str, ...] = OPTIONS) -> Resource:
    return Resource(
        path=PATH,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue", writable=True, allowed_values=options
        ),
    )


def _select(
    hass: HomeAssistant, resource: Resource, gateway: str = "gateway-one"
) -> BoschBuderusSelect:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=gateway)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway(gateway, device_type="K40"), entry
    )
    coordinator.update_interval = None
    coordinator.resources = {PATH: resource}
    coordinator.data = {PATH: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.last_update_success = True
    return BoschBuderusSelect(
        coordinator, build_select_descriptions(coordinator.resources)[0]
    )


@pytest.mark.parametrize(
    ("language", "name"),
    [("de", "Saisonbetrieb"), ("en", "Season mode")],
)
def test_season_select_is_central_and_does_not_invent_cooling(
    hass: HomeAssistant, language: str, name: str
) -> None:
    hass.config.language = language
    entity = _select(hass, _resource("forcedHeat", tuple(reversed(OPTIONS))))

    assert entity.available
    assert entity.options == ["off", "automatic", "forced_heat"]
    assert entity.current_option == "forced_heat"
    assert entity.name.endswith(name)
    assert entity.unique_id == "gateway-one:system:seasonOptimizer:mode:control"
    assert entity.device_info["identifiers"] == {(DOMAIN, "gateway-one")}
    assert entity.entity_description.translation_key == "season_switching"
    assert entity.entity_description.entity_registry_enabled_default
    assert poll_group(_resource()) is PollGroup.CONTROL


@pytest.mark.parametrize(
    "resource",
    [
        replace(_resource(), path=STATUS_PATH),
        replace(_resource(), path="/heatingCircuits/hc1/suWiSwitchMode"),
        replace(_resource(), path="/system/seasonOptimizer/mode/other"),
        replace(_resource(), metadata=replace(_resource().metadata, writable=False)),
        replace(
            _resource(),
            metadata=replace(_resource().metadata, resource_type="floatValue"),
        ),
        _resource(options=()),
        _resource(options=("off", "")),
        _resource(options=("off", "invalid\noption")),
    ],
)
def test_season_select_requires_exact_write_contract(resource: Resource) -> None:
    assert build_select_descriptions({resource.path: resource}) == ()
    assert not _control_diagnostics(resource)["eligible"]


@pytest.mark.parametrize(
    ("option", "raw"),
    [("off", "off"), ("automatic", "automatic"), ("forced_heat", "forcedHeat")],
)
async def test_season_write_targets_one_gateway_and_waits_for_readback(
    hass: HomeAssistant, option: str, raw: str
) -> None:
    entity = _select(hass, _resource("automatic" if raw == "off" else "off"))
    other_gateway = _select(hass, _resource(), "gateway-two")
    status = replace(
        _resource(),
        path=STATUS_PATH,
        metadata=replace(_resource().metadata, writable=False),
    )
    dhw = replace(_resource("eco"), path="/dhwCircuits/dhw1/operationMode")
    entity.coordinator.data[STATUS_PATH] = ResourceSnapshot(
        status, True, datetime.now(UTC)
    )
    entity.coordinator.data[dhw.path] = ResourceSnapshot(dhw, True, datetime.now(UTC))
    before_option = entity.current_option
    client = AsyncMock()
    client.put_resource_value.return_value = None

    async def readback(gateway_id: str, path: str) -> Resource:
        assert (gateway_id, path) == ("gateway-one", PATH)
        assert entity.current_option == before_option
        return _resource(raw)

    client.get_resource.side_effect = readback
    entity.coordinator._write_service = WriteService(client, sleep=AsyncMock())

    await entity.async_select_option(option)

    assert entity.current_option == option
    client.put_resource_value.assert_awaited_once_with("gateway-one", PATH, raw)
    client.get_resource.assert_awaited_once_with("gateway-one", PATH)
    assert other_gateway.current_option == "off"
    assert entity.coordinator.data[STATUS_PATH].resource is status
    assert entity.coordinator.data[dhw.path].resource is dhw


async def test_season_options_follow_live_metadata_and_preserve_identity(
    hass: HomeAssistant,
) -> None:
    entity = _select(hass, _resource())
    client = AsyncMock()
    entity.coordinator._write_service = WriteService(client, sleep=AsyncMock())
    identity = entity.unique_id
    cooling = _resource("automatic", (*OPTIONS, "forcedCool"))
    entity.coordinator.data[PATH] = ResourceSnapshot(cooling, True, datetime.now(UTC))
    assert entity.options == ["off", "automatic", "forced_heat", "forced_cool"]

    reduced = _resource("automatic", ("off", "automatic"))
    entity.coordinator.data[PATH] = ResourceSnapshot(reduced, True, datetime.now(UTC))
    assert entity.options == ["off", "automatic"]
    assert entity.unique_id == identity
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("forced_cool")
    client.put_resource_value.assert_not_awaited()
    client.get_resource.assert_not_awaited()


@pytest.mark.parametrize("blocked", ["permission", "stale", "missing", "rate_limit"])
async def test_season_writes_are_blocked_when_resource_or_gateway_is_not_ready(
    hass: HomeAssistant, blocked: str
) -> None:
    entity = _select(hass, _resource())
    client = AsyncMock()
    entity.coordinator._write_service = WriteService(client, sleep=AsyncMock())
    if blocked == "permission":
        snapshot = entity.coordinator.data[PATH]
        revoked = replace(
            snapshot.resource,
            metadata=replace(snapshot.resource.metadata, writable=False),
        )
        entity.coordinator.data[PATH] = replace(snapshot, resource=revoked)
    elif blocked == "stale":
        entity.coordinator.data[PATH] = replace(
            entity.coordinator.data[PATH], freshness=Freshness.STALE
        )
    elif blocked == "missing":
        entity.coordinator.data = {}
    else:
        entity.coordinator._cloud_backoff_until = float("inf")

    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("forced_heat")
    client.put_resource_value.assert_not_awaited()
    client.get_resource.assert_not_awaited()


async def test_unconfirmed_season_write_does_not_claim_success(
    hass: HomeAssistant,
) -> None:
    entity = _select(hass, _resource())
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = _resource()
    entity.coordinator._write_service = WriteService(client, sleep=AsyncMock())

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_select_option("forced_heat")

    assert error.value.translation_key == "write_not_confirmed"
    assert entity.current_option == "off"
    client.put_resource_value.assert_awaited_once_with(
        "gateway-one", PATH, "forcedHeat"
    )
    assert client.get_resource.await_count == 3


async def test_rate_limited_season_write_pauses_further_writes(
    hass: HomeAssistant,
) -> None:
    entity = _select(hass, _resource())
    client = AsyncMock()
    client.put_resource_value.side_effect = RateLimited(120)
    entity.coordinator._write_service = WriteService(client, sleep=AsyncMock())

    for _ in range(2):
        with pytest.raises(HomeAssistantError) as error:
            await entity.async_select_option("forced_heat")
        assert error.value.translation_key == "write_rate_limited"

    client.put_resource_value.assert_awaited_once()
    client.get_resource.assert_not_awaited()
    assert entity.current_option == "off"


def test_season_diagnostics_disclose_known_options_without_current_values() -> None:
    resource = _resource("private-current-value", (*OPTIONS, "private-option"))
    report = _control_diagnostics(resource)
    assert report["eligible"]
    assert report["platform"] == "select"
    assert report["advertised_known_options"] == ["automatic", "forcedHeat", "off"]
    assert report["unrecognized_option_count"] == 1
    assert "private" not in json.dumps(report)
    assert build_select_descriptions({PATH: resource})[0].options == [
        "off",
        "automatic",
        "forced_heat",
        "private-option",
    ]


@pytest.mark.parametrize("value", ["off", "heating", "cooling"])
def test_actual_season_status_is_read_only_and_retains_its_identity(
    hass: HomeAssistant, value: str
) -> None:
    resource = replace(
        _resource(value, ("off", "heating", "cooling")),
        path=STATUS_PATH,
        metadata=replace(_resource().metadata, writable=False),
    )
    entity = _select(hass, _resource("automatic"))
    entity.coordinator.data[STATUS_PATH] = ResourceSnapshot(
        resource, True, datetime.now(UTC)
    )
    description = build_sensor_descriptions({STATUS_PATH: resource})[0]
    status = BoschBuderusSensor(entity.coordinator, description)
    assert status.native_value == value
    assert status.unique_id == "gateway-one:system:globalSeasonOptimizer:currentMode"
    assert status.entity_description.translation_key == "season_optimizer_mode"
    assert not status.entity_description.entity_registry_enabled_default
    assert build_select_descriptions({STATUS_PATH: resource}) == ()
    assert entity.current_option == "automatic"
