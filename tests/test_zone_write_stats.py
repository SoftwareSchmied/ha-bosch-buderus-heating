"""Counters of the changes made through a zone's climate (synthetic).

The changes run through the real write path (the cloud stand-in of the write
tests); nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.pointt import RequestTimeout
from custom_components.bosch_buderus_heating.zone_climate import (
    zone_climate_diagnostics,
)
from custom_components.bosch_buderus_heating.zone_write_stats import (
    COUNTERS,
    ZoneWriteStats,
    counted_write,
    zone_write_stats,
)
from tests.test_zone_climate import (
    MANUAL,
    MODE,
    TEMPORARY,
    Z1,
    button,
    changed,
    climate,
)
from tests.test_zone_climate import zone_plant as plant  # noqa: F401


def writes(coordinator, root=Z1):
    report = zone_climate_diagnostics(coordinator)
    (entry,) = (item for item in report if item["zone"] == root.rsplit("/", 1)[1])
    return entry["writes"]


async def test_a_zone_without_changes_reports_zeros(hass, plant):  # noqa: F811
    coordinator, _ = plant
    for entry in zone_climate_diagnostics(coordinator):
        assert entry["writes"] == {
            **{counter: 0 for counter in COUNTERS},
            "last_step": None,
            "rejected_reasons": {},
        }


async def test_confirmed_steps_are_counted_per_step(hass, plant):  # noqa: F811
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual"), (MANUAL, 22)]
    counts = writes(coordinator)
    assert counts["writes_sent"] == 2
    assert counts["writes_confirmed"] == 2
    assert counts["writes_rejected"] == 0
    assert counts["last_step"] == {"kind": "target", "result": "confirmed"}
    # Another zone is not touched.
    assert writes(coordinator, "/zones/zone2")["writes_sent"] == 0


async def test_a_refused_change_is_counted_and_sends_nothing(hass, plant):  # noqa: F811
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    changed(coordinator, cloud, MODE, value="off")
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []
    counts = writes(coordinator)
    assert counts["writes_rejected"] == 1
    assert counts["writes_sent"] == 0
    assert counts["rejected_reasons"] == {"zone_climate_command": 1}
    assert counts["last_step"] == {"kind": "target", "result": "rejected"}


async def test_an_unconfirmed_step_is_counted_as_sent(hass, plant):  # noqa: F811
    coordinator, cloud = plant
    entity = climate(hass, coordinator)

    async def ignored(gateway, path, value):
        cloud.puts.append((path, value))  # the gateway keeps the old mode

    coordinator.client.put_resource_value.side_effect = ignored
    with pytest.raises(HomeAssistantError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    counts = writes(coordinator)
    assert counts["writes_unconfirmed"] == 1
    assert counts["writes_sent"] == 1
    assert counts["writes_confirmed"] == 0
    assert counts["last_step"] == {"kind": "mode", "result": "unconfirmed"}


async def test_a_failed_request_is_counted_apart(hass, plant):  # noqa: F811
    coordinator, cloud = plant
    entity = climate(hass, coordinator)

    async def broken(gateway, path, value):
        cloud.puts.append((path, value))
        raise RequestTimeout("lost response")

    # The read-back cannot show the value either.
    cloud.readback_error = RequestTimeout("still lost")
    coordinator.client.put_resource_value.side_effect = broken
    with pytest.raises(HomeAssistantError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    counts = writes(coordinator)
    assert counts["writes_confirmed"] == 0
    assert counts["writes_unconfirmed"] + counts["writes_failed"] == 1
    assert len(cloud.puts) == 1  # never sent twice


async def test_the_clear_button_is_counted_as_its_own_step(hass, plant):  # noqa: F811
    coordinator, cloud = plant
    entity = button(hass, coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_press()
    assert writes(coordinator)["last_step"] == {"kind": "clear", "result": "rejected"}
    assert writes(coordinator)["rejected_reasons"] == {
        "zone_temporary_setpoint_not_active": 1
    }
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    await entity.async_press()
    counts = writes(coordinator)
    assert counts["writes_confirmed"] == 1
    assert counts["last_step"] == {"kind": "clear", "result": "confirmed"}


async def test_the_report_has_no_values_names_or_paths(hass, plant):  # noqa: F811
    coordinator, _cloud = plant
    await climate(hass, coordinator).async_set_temperature(temperature=22.5)
    text = json.dumps(zone_climate_diagnostics(coordinator))
    for secret in (
        "22.5",
        "temporaryRoomSetpoint",
        "operationMode",
        "/zones",
        "Zone 1",
    ):
        assert secret not in text


async def test_unknown_reasons_are_never_reported():
    stats = ZoneWriteStats()
    stats.record("/zones/zone1", "mode", "rejected", "some free text 21.5 C")
    assert stats.snapshot("/zones/zone1")["rejected_reasons"] == {}
    assert stats.snapshot("/zones/zone1")["writes_rejected"] == 1


async def test_counters_belong_to_one_coordinator(hass, plant):  # noqa: F811
    coordinator, _ = plant
    assert zone_write_stats(coordinator) is zone_write_stats(coordinator)
    other = object.__new__(type(coordinator))
    assert zone_write_stats(other) is not zone_write_stats(coordinator)


def test_a_cancelled_step_counts_nothing():
    import asyncio

    class Owner:
        pass

    owner = Owner()
    with (
        pytest.raises(asyncio.CancelledError),
        counted_write(owner, "/zones/zone1", "mode"),
    ):
        raise asyncio.CancelledError
    assert zone_write_stats(owner).snapshot("/zones/zone1")["writes_confirmed"] == 0
    assert zone_write_stats(owner).snapshot("/zones/zone1")["last_step"] is None
