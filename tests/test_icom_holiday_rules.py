"""I2: iCom safeguards; holiday pre-check, component rules, units, climate.

No system with a HMI800 HP Light or with token 16 has been observed; the
component rules are expected, untested on a real device.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating import legacy_holiday_writes
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.diagnostics import _gateway_diagnostics
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitClimate,
)
from custom_components.bosch_buderus_heating.holiday_writes import (
    holiday_resources_from_snapshots,
)
from custom_components.bosch_buderus_heating.holidays import (
    icom_write_configuration,
    icom_write_values,
)
from custom_components.bosch_buderus_heating.icom_diagnostics import BUS_HINT
from custom_components.bosch_buderus_heating.legacy_holidays import (
    SYSTEM_INFO_PATH,
    icom_components,
    icom_temperature,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import MockConfigEntry
from tests.test_dynamic_controls import snapshots
from tests.test_legacy_holidays import FREE, RESOURCES, SLOT, environment, wire

__all__ = ["environment"]


def _info(*items: dict[str, object]) -> Resource:
    return parse_resource(
        {"id": SYSTEM_INFO_PATH, "type": "systeminfo", "values": list(items)}
    )


HMI800 = _info(
    {"Id": "189", "Tok": "1", "ProductSerialNumber": "private-serial"},
    {"Id": "240", "Tok": "5", "ProductName": "private-name"},
)
TOKEN_16 = _info({"Id": "189", "Tok": "16"}, {"Id": "7", "Tok": "25"})


def _with_info(environment, info: Resource) -> dict[str, Resource]:
    state = wire(environment)
    coordinator, _client, _ = environment
    state[SYSTEM_INFO_PATH] = info
    coordinator.resources[SYSTEM_INFO_PATH] = info
    coordinator.data = snapshots(*state.values())
    return state


def test_components_keep_only_bounded_type_numbers() -> None:
    components = icom_components(
        _info(
            {"Id": "240", "Tok": "5", "ProductSerialNumber": "private"},
            {"Id": 189, "Tok": 1},
            {"Id": True, "Tok": "1"},
            {"Id": "not-a-number", "Tok": "2"},
            {"Id": "16", "Tok": "x" * 8},
            "not an object",
        )
    )
    assert components.items == (("240", "5"), ("189", "1"), ("16", None))
    assert components.hmi800_hp_light
    assert not components.hidden_circuits
    other = Resource(path="/heatSources/info", values=({"Id": "240", "Tok": "1"},))
    assert not icom_components(other).items
    assert not icom_components(None).hmi800_hp_light


def test_token_16_hides_only_the_circuits_of_tokens_24_to_27() -> None:
    components = icom_components(
        _info(
            {"Tok": "16", "Id": "1"}, {"Tok": "24", "Id": "2"}, {"Tok": "27", "Id": "3"}
        )
    )
    assert components.hidden_circuits == {"hc1", "hc4"}
    assert not icom_components(_info({"Tok": "24", "Id": "2"})).hidden_circuits


def test_hmi800_keeps_circuits_and_heating_mode_of_the_slot() -> None:
    plain = icom_write_configuration(RESOURCES, SLOT)
    resources = {**RESOURCES, SYSTEM_INFO_PATH: HMI800}
    rule = icom_write_configuration(resources, SLOT)
    assert plain is not None and rule is not None
    assert plain.heating_modes == ("OFF", "FIX_TEMPERATURE", "ECO")
    assert rule.heating_modes == ("FIX_TEMPERATURE",)
    assert rule.assigned_to == ("hc2", "dhw1")
    assert rule.dhw_modes == plain.dhw_modes
    # A free slot without circuits cannot keep any: no new holiday there.
    assert icom_write_configuration(RESOURCES, FREE) is not None
    assert icom_write_configuration(resources, FREE) is None


def test_token_16_removes_new_circuits_but_keeps_an_assigned_one() -> None:
    resources = {
        **RESOURCES,
        SYSTEM_INFO_PATH: _info({"Id": "1", "Tok": "16"}, {"Id": "2", "Tok": "25"}),
    }
    free = icom_write_configuration(resources, FREE)
    assigned = icom_write_configuration(resources, SLOT)
    assert free is not None and free.assigned_to == ("dhw1",)
    assert assigned is not None and assigned.assigned_to == ("hc2", "dhw1")


def test_component_list_reaches_the_holiday_rules_from_the_snapshots() -> None:
    resources = holiday_resources_from_snapshots(snapshots(*RESOURCES.values(), HMI800))
    assert resources[SYSTEM_INFO_PATH] == HMI800


@pytest.mark.parametrize(
    ("unit", "accepted"), [("C", True), ("°C", True), ("F", False), (None, False)]
)
def test_fix_temperature_accepts_degrees_celsius_with_the_sign(unit, accepted) -> None:
    resource = Resource(
        path=f"{SLOT}/fixTemperature",
        value=17.5,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=unit),
    )
    assert (icom_temperature(resource) == 17.5) is accepted


async def test_hmi800_change_of_circuits_or_heating_mode_sends_nothing(
    environment,
) -> None:
    state = _with_info(environment, HMI800)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    for changes in ({"heating_mode": "ECO"}, {"assigned_to": ("hc2",)}):
        with pytest.raises(WriteValidationError):
            await coordinator.async_update_holiday(
                2, replace(baseline, dhw_mode="LOW", **changes), expected=baseline
            )
    client.put_resource_value.assert_not_awaited()
    client.put_icom_holiday_assignments.assert_not_awaited()
    # Dates and the hot water mode stay changeable.
    await coordinator.async_update_holiday(
        2,
        replace(baseline, dhw_mode="LOW", end_date="2030-10-30"),
        expected=baseline,
    )
    assert [call.args[1] for call in client.put_resource_value.await_args_list] == [
        f"{SLOT}/dhwMode",
        f"{SLOT}/startStop",
    ]


async def test_hmi800_rule_also_holds_in_the_transaction_itself(environment) -> None:
    """Even an unrestricted choice never writes hcMode with a HMI800 HP Light."""
    state = _with_info(environment, HMI800)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)

    def unrestricted(resources, source_path):
        plain = {k: v for k, v in resources.items() if k != SYSTEM_INFO_PATH}
        return icom_write_configuration(plain, source_path)

    with (
        patch.object(legacy_holiday_writes, "icom_write_configuration", unrestricted),
        pytest.raises(WriteValidationError, match="keeps the holiday"),
    ):
        await coordinator.async_update_holiday(
            2, replace(baseline, dhw_mode="LOW", heating_mode="ECO"), expected=baseline
        )
    client.put_resource_value.assert_not_awaited()


async def test_read_only_field_is_found_before_the_first_put(environment) -> None:
    """I2: no dhwMode is sent when a later field of the change is read-only."""
    state = wire(environment)
    coordinator, client, _ = environment
    hc_mode = f"{SLOT}/hcMode"
    state[hc_mode] = replace(
        state[hc_mode], metadata=replace(state[hc_mode].metadata, writable=False)
    )
    coordinator.data = snapshots(*state.values())
    baseline = icom_write_values(state, SLOT)

    def offered(resources, source_path):
        # A choice offered for a field that the slot reports as read-only.
        writable = {
            path: replace(item, metadata=replace(item.metadata, writable=True))
            for path, item in resources.items()
        }
        return icom_write_configuration(writable, source_path)

    with (
        patch.object(legacy_holiday_writes, "icom_write_configuration", offered),
        pytest.raises(WriteValidationError, match="not writable"),
    ):
        await coordinator.async_update_holiday(
            2, replace(baseline, dhw_mode="LOW", heating_mode="ECO"), expected=baseline
        )
    client.put_resource_value.assert_not_awaited()
    client.put_icom_holiday_assignments.assert_not_awaited()


async def test_flag_withdrawn_by_a_read_back_still_stops_the_next_step(
    environment,
) -> None:
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    original = client.put_resource_value.side_effect

    async def put(gateway, path, value):
        await original(gateway, path, value)
        hc_mode = f"{SLOT}/hcMode"
        state[hc_mode] = replace(
            state[hc_mode], metadata=replace(state[hc_mode].metadata, writable=False)
        )

    client.put_resource_value.side_effect = put
    # The read-back sees the other field changed: a conflict ends it at once.
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_update_holiday(
            2, replace(baseline, dhw_mode="LOW", heating_mode="ECO"), expected=baseline
        )
    assert client.put_resource_value.await_count == 1


def _icom_coordinator(
    hass: HomeAssistant, device_type: str, *resources: Resource
) -> BoschBuderusDataUpdateCoordinator:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("private-gateway", device_type=device_type), entry
    )
    coordinator.resources = {item.path: item for item in resources}
    coordinator.data = snapshots(*resources)
    return coordinator


@pytest.mark.parametrize(
    ("bus", "shown", "hint"),
    [
        ("EMS2_0", "EMS2_0", None),
        ("CAN", "CAN", BUS_HINT),
        ("EMS1_0", "EMS1_0", BUS_HINT),
        ("No_Bus", "No_Bus", BUS_HINT),
        ("private-value", "other", None),
    ],
)
def test_icom_diagnostics_show_components_bus_and_profile(
    hass: HomeAssistant, bus, shown, hint
) -> None:
    bus_resource = Resource(path="/system/bus", value=bus, has_value=True)
    coordinator = _icom_coordinator(hass, "icom", HMI800, bus_resource)
    report = _gateway_diagnostics(1, coordinator)["icom"]
    assert report["system_components"] == [
        {"id": "189", "token": "1"},
        {"id": "240", "token": "5"},
    ]
    assert report["bus"] == shown
    assert report["bus_hint"] == hint
    assert report["holiday_rules"] == {
        "status": "expected_untested",
        "hmi800_hp_light": True,
        "hidden_circuits": [],
    }
    profile = report["load_profile"]
    assert profile["bulk_size"] == 30
    assert profile["read_back_delays"] == [1.0, 2.0, 3.0, 4.0]
    assert profile["circuit_pause_minutes"] == [5, 10, 20, 30]
    rendered = repr(report)
    for private in ("private-serial", "private-name", "private-value"):
        assert private not in rendered


@pytest.mark.parametrize("bus", [["EMS1_0"], {"bus": "CAN"}, 3])
def test_icom_diagnostics_survive_a_bus_that_is_no_string(
    hass: HomeAssistant, bus
) -> None:
    bus_resource = Resource(path="/system/bus", value=bus, has_value=True)
    coordinator = _icom_coordinator(hass, "icom", HMI800, bus_resource)
    report = _gateway_diagnostics(1, coordinator)["icom"]
    assert report["bus"] == "other"
    assert report["bus_hint"] is None


def test_the_bus_hint_names_no_app() -> None:
    assert "app" not in BUS_HINT.split("_")
    assert BUS_HINT == "bus_possibly_unsupported_reported_values_only"


def test_other_gateways_keep_their_diagnostics(hass: HomeAssistant) -> None:
    coordinator = _icom_coordinator(hass, "MX300", HMI800)
    assert "icom" not in _gateway_diagnostics(1, coordinator)


def _climate_resources(control_type: str) -> tuple[Resource, ...]:
    root = "/heatingCircuits/hc1"
    return (
        parse_resource(
            {
                "id": f"{root}/operationMode",
                "type": "stringValue",
                "value": "manual",
                "allowedValues": ["auto", "manual", "off"],
                "writeable": 1,
            }
        ),
        parse_resource(
            {
                "id": f"{root}/manualRoomSetpoint",
                "type": "floatValue",
                "value": 21,
                "unitOfMeasure": "C",
                "minValue": 5,
                "maxValue": 30,
                "stepSize": 0.5,
                "writeable": 1,
            }
        ),
        parse_resource(
            {
                "id": f"{root}/controlType",
                "type": "stringValue",
                "value": control_type,
                "allowedValues": ["outdoorTemperature", "constants"],
                "writeable": 1,
            }
        ),
    )


@pytest.mark.parametrize("device_type", ["icom", "MX300"])
async def test_constant_control_type_makes_the_climate_read_only(
    hass: HomeAssistant, device_type
) -> None:
    coordinator = _icom_coordinator(hass, device_type, *_climate_resources("constants"))
    coordinator.last_update_success = True
    climate = HeatingCircuitClimate(coordinator, "/heatingCircuits/hc1")
    climate.hass = hass
    assert climate.available
    assert climate.hvac_mode is HVACMode.HEAT
    assert climate.hvac_modes == [HVACMode.HEAT]
    assert climate.supported_features == ClimateEntityFeature(0)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=22)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_hvac_mode(HVACMode.HEAT)
    # Refused before the fresh guard read of a write: no request at all.
    coordinator.client.get_resources_bulk.assert_not_awaited()
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.client.put_resource_value.assert_not_awaited()


async def test_other_control_types_keep_the_climate_controls(
    hass: HomeAssistant,
) -> None:
    coordinator = _icom_coordinator(
        hass, "icom", *_climate_resources("outdoorTemperature")
    )
    coordinator.last_update_success = True
    climate = HeatingCircuitClimate(coordinator, "/heatingCircuits/hc1")
    climate.hass = hass
    assert set(climate.hvac_modes) == {HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF}
    assert climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE


async def test_the_read_only_lock_needs_a_reading_to_end(hass: HomeAssistant) -> None:
    """A stale or failed ``controlType`` reading never lifts the
    lock silently; only a current reading of another control type does."""
    from dataclasses import replace

    from custom_components.bosch_buderus_heating.coordinator import Freshness

    coordinator = _icom_coordinator(hass, "icom", *_climate_resources("constants"))
    coordinator.last_update_success = True
    climate = HeatingCircuitClimate(coordinator, "/heatingCircuits/hc1")
    climate.hass = hass
    path = "/heatingCircuits/hc1/controlType"
    coordinator.data[path] = replace(
        coordinator.data[path], freshness=Freshness.STALE, available=False
    )
    assert climate.supported_features == ClimateEntityFeature(0)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=22)
    # Only known from discovery (no snapshot): the stored value decides.
    del coordinator.data[path]
    assert climate.supported_features == ClimateEntityFeature(0)
    # A current reading of another control type ends the lock.
    other = _climate_resources("outdoorTemperature")[2]
    coordinator.resources[path] = other
    coordinator.data[path] = replace(
        coordinator.data["/heatingCircuits/hc1/operationMode"], resource=other
    )
    assert climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    coordinator.client.put_resource_value.assert_not_awaited()
