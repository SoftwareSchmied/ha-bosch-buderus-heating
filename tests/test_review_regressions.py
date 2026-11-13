"""Independent synthetic regression probes for the release review."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating.bacon.models import (
    CONF_BACON_IDS,
    BaconError,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.pointt import BatchItemResult, Gateway
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_bacon_dhw import configured, responding
from tests.test_bacon_ha import entry, flow
from tests.test_bacon_session import emit
from tests.test_rrc import SELECTION, WEEK, wire
from tests.test_rrc import environment as environment

ROOT = "custom_components.bosch_buderus_heating"


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_water_heater_accepts_valid_fahrenheit_target(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    config = entry(hass)
    account, heater, data = configured()
    data["metadata"]["shadows"]["state"]["tempSetpoint"]["step"] = 1
    emit(account, "topics/meta", data["metadata"])
    writes = responding(account, data)

    async def wait():
        await asyncio.Event().wait()

    with (
        patch(ROOT + ".BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done()
        entity_id = er.async_get(hass).async_get_entity_id(
            "water_heater", DOMAIN, heater.unique_id
        )
        try:
            await hass.services.async_call(
                "water_heater",
                "set_temperature",
                {"entity_id": entity_id, "temperature": 141.8},
                blocking=True,
            )
            assert writes == [{"tempSetpoint": 61}]
        finally:
            await hass.config_entries.async_unload(config.entry_id)
            await hass.async_block_till_done()


async def test_heating_only_reauth_survives_optional_service_outage(hass):
    config = flow(hass)
    existing = entry(hass, ["pointt"])
    hass.config_entries.async_update_entry(
        existing, data={**existing.data, CONF_BACON_IDS: []}
    )
    config._reauth_entry = existing
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("pointt"),)),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(side_effect=BaconError("service_unavailable")),
        ),
        patch.object(
            config,
            "_update_and_restart",
            return_value={"reason": "reauth_successful"},
        ) as completed,
    ):
        result = await config._async_discover_gateways()
    assert result.get("reason") == "reauth_successful", result
    completed.assert_called_once()


@pytest.mark.parametrize("editing", [False, True])
async def test_cancelled_rrc_write_invalidates_unconfirmed_state(environment, editing):
    coordinator, client, _ = environment
    state = wire(environment)
    original_put = client.put_resource_value.side_effect
    applied = asyncio.Event()

    async def interrupted(*args):
        await original_put(*args)
        applied.set()
        await asyncio.Event().wait()

    client.put_resource_value.side_effect = interrupted
    path = WEEK if editing else SELECTION
    operation = (
        coordinator.async_edit_rrc_program(state[WEEK], 0, 365, 20.5)
        if editing
        else coordinator.async_select_rrc_program(SELECTION, 7)
    )
    task = asyncio.create_task(operation)
    await applied.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state[path] != coordinator.data[path].resource
    assert coordinator.data[path].available is False
    assert client.put_resource_value.await_count == 1
    coordinator._paths_by_group = {PollGroup.CONTROL: tuple(state)}
    coordinator._next_update = {PollGroup.CONTROL: 0}

    async def bulk(gateway, paths):
        return tuple(
            BatchItemResult(gateway, selected, 200, resource=state[selected])
            for selected in paths
        )

    client.get_resources_bulk.side_effect = bulk
    updated = await coordinator._async_update_data_locked()
    coordinator.async_set_updated_data(updated)
    assert coordinator.data[path].available
    assert coordinator.data[path].resource == state[path]
    assert client.put_resource_value.await_count == 1


def test_partial_energy_metadata_does_not_change_physical_value(hass):
    from datetime import UTC, datetime

    from homeassistant.util.unit_conversion import EnergyConverter

    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
    from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
    from tests.test_sensor import _sensor

    payload = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "unitOfMeasure": "Wh",
        "values": [{"compressor": 1000, "eheater": 0}],
    }
    original = parse_resource(payload)
    entity = _sensor(hass, original, value_key="compressor")
    assert entity.native_unit_of_measurement == "kWh" and entity.native_value == 1
    partial = parse_resource({k: v for k, v in payload.items() if k != "type"})
    entity.coordinator.data[original.path] = ResourceSnapshot(
        partial, True, datetime.now(UTC)
    )
    assert (
        entity.native_value is None
        or EnergyConverter.convert(
            entity.native_value, entity.native_unit_of_measurement, "kWh"
        )
        == 1
    ), (entity.native_value, entity.native_unit_of_measurement)
