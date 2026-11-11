"""R3: the synthetic iCom gateway fixture (shapes only, constructed values).

The paths, types, units, write flags, option counts and the 403/404 sets
follow the shapes of an iCom gateway with a boiler controller; every value is
constructed. Nothing here confirms behaviour on a real iCom gateway.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.button import build_button_descriptions
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.diagnostics import _gateway_diagnostics
from custom_components.bosch_buderus_heating.faults import is_fault_resource_path
from custom_components.bosch_buderus_heating.holidays import (
    icom_write_configuration,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from custom_components.bosch_buderus_heating.legacy_holidays import (
    SYSTEM_INFO_PATH,
    icom_components,
    icom_slots,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RateLimited,
    RequestTimeout,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    supports_entity,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.switch import build_switch_descriptions
from tests.ha_test_helpers import MockConfigEntry
from tests.test_coordinator import _snapshot
from tests.test_coordinator_outages import clock

FIXTURE = (
    Path(__file__).parent / "fixtures" / "contracts" / "icom" / "gateway-shapes.json"
)
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
RESOURCES = {item.path: item for item in map(parse_resource, DATA["resources"])}
SLOTS = tuple(f"/system/holidayModes/hm{n}" for n in range(1, 6))


def _keys(platform_builder) -> set[str]:
    return {
        getattr(item, "unique_key", None) or item.key
        for item in platform_builder(RESOURCES)
    }


def test_fixture_is_synthetic_without_identifiers() -> None:
    provenance = DATA["provenance"]
    assert provenance["kind"] == "synthetic"
    assert provenance["physical_device_verified"] is False
    assert DATA["gateway"] == {"device_type": "icom"}
    rendered = FIXTURE.read_text(encoding="utf-8")
    for private in ("Serial", "serialId", "@", 'uuid": "0', "macAddress"):
        assert private not in rendered
    assert len(RESOURCES) == len(DATA["resources"]) == 91
    forbidden, missing = (
        set(DATA["unavailable"]["403"]),
        set(DATA["unavailable"]["404"]),
    )
    assert not forbidden & missing
    assert not (forbidden | missing) & set(RESOURCES)
    # Every reference either answers or is in one of the two sets.
    for resource in RESOURCES.values():
        for reference in resource.references:
            assert reference.path in RESOURCES or reference.path in forbidden, (
                reference.path
            )


def test_holiday_slots_have_five_slots_and_the_app_order_shape() -> None:
    assert icom_slots(RESOURCES) == SLOTS
    # The slot parents and every delete answer 403, fixTemperature 404.
    for slot in SLOTS:
        assert slot in DATA["unavailable"]["403"]
        assert f"{slot}/delete" in DATA["unavailable"]["403"]
        assert f"{slot}/fixTemperature" in DATA["unavailable"]["404"]
        assert RESOURCES[f"{slot}/assignedTo"].metadata.resource_type == "arrayData"
    state = parse_holiday_state(RESOURCES)
    assert len(state.periods) == 1
    booked = icom_write_configuration(RESOURCES, SLOTS[0])
    assert booked is not None
    assert booked.can_delete is False
    assert booked.fix_temperature_min is None
    assert booked.assigned_to == ("hc1", "dhw1")
    # The first free slot is offered for a new holiday.
    free = parse_holiday_write_configuration(RESOURCES)
    assert free is not None and free.source_path == SLOTS[1]


def test_temporary_setpoint_is_read_and_manual_setpoint_is_missing() -> None:
    temporary = RESOURCES["/heatingCircuits/hc1/temporaryRoomSetpoint"]
    assert temporary.metadata.writable
    assert temporary.metadata.minimum is not None
    assert temporary.metadata.maximum is not None
    assert supports_entity(temporary)
    assert "/heatingCircuits/hc1/manualRoomSetpoint" in DATA["unavailable"]["404"]


def test_hot_water_charge_shapes_build_their_controls() -> None:
    numbers = _keys(build_number_descriptions)
    switches = _keys(build_switch_descriptions)
    selects = _keys(build_select_descriptions)
    sensors = _keys(build_sensor_descriptions)
    assert any("chargeDuration" in key for key in numbers)
    assert any("singleChargeSetpoint" in key for key in numbers)
    assert any(key.endswith("dhw1:charge") or "dhw1/charge" in key for key in switches)
    assert any("dhw1" in key and "operationMode" in key for key in selects)
    assert any("tdMode" in key for key in sensors | selects | switches)
    assert build_button_descriptions(RESOURCES) is not None


def test_faults_come_only_through_notifications() -> None:
    sources = {path for path in RESOURCES if is_fault_resource_path(path)}
    assert sources == {"/notifications"}
    assert "/heatSources/hs1/activefailure" in DATA["unavailable"]["404"]
    assert "/heatSources/hs1/failurelist" in DATA["unavailable"]["404"]


def test_components_and_marked_variants() -> None:
    components = icom_components(RESOURCES[SYSTEM_INFO_PATH])
    assert not components.hmi800_hp_light
    assert not components.hidden_circuits
    variants = DATA["variants"]
    assert all(variant["observed"] is False for variant in variants.values())
    hmi = icom_components(parse_resource(variants["hmi800_hp_light"]["system_info"]))
    assert hmi.hmi800_hp_light
    token = icom_components(parse_resource(variants["circuit_token_16"]["system_info"]))
    assert token.hidden_circuits == {"hc1"}
    assert icom_write_configuration(
        {
            **RESOURCES,
            SYSTEM_INFO_PATH: parse_resource(
                variants["hmi800_hp_light"]["system_info"]
            ),
        },
        SLOTS[0],
    ).heating_modes == ("ECO",)


def _coordinator(
    hass: HomeAssistant,
) -> tuple[BoschBuderusDataUpdateCoordinator, AsyncMock]:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("fixture-gateway", device_type="icom"), entry
    )
    leaves = tuple(
        path
        for path, item in RESOURCES.items()
        if item.has_value and not item.references and path != "/notifications"
    )
    coordinator.resources = dict(RESOURCES)
    coordinator.data = {path: _snapshot(item) for path, item in RESOURCES.items()}
    coordinator._paths_by_group = {PollGroup.FAST: leaves}
    return coordinator, client


async def test_transport_profile_outage_halves_batches_and_grows_the_pause(
    hass: HomeAssistant,
) -> None:
    """Case 6: timeouts, a rate limit after four failed cycles, then recovery."""
    profile = DATA["transport_profile"]
    coordinator, client = _coordinator(hass)
    leaves = coordinator._paths_by_group[PollGroup.FAST]
    assert len(leaves) > 30
    # The outage: every request runs into the timeout, single reads as well.
    client.get_resources_bulk.side_effect = RequestTimeout("timeout")
    client.get_resource.side_effect = RequestTimeout("timeout")
    first_batches: list[int] = []
    pauses: list[float] = []
    with clock() as now:
        for _ in range(profile["outage_failed_cycles_before_rate_limit"]):
            seen = client.get_resources_bulk.await_count
            await coordinator.async_refresh()
            first_batches.append(
                len(client.get_resources_bulk.await_args_list[seen].args[1])
            )
            if coordinator._circuit_open_until > now.now:
                pauses.append(coordinator._circuit_open_until - now.now)
                now.now = coordinator._circuit_open_until + 1
            else:
                now.now += 60
        client.get_resources_bulk.side_effect = RateLimited(retry_after=600)
        await coordinator.async_refresh()
        assert coordinator._cloud_backoff_until > now.now
        now.now = coordinator._cloud_backoff_until + 1

        def answer(gateway, paths):
            # The healthy answers carry the constructed fixture values.
            return tuple(
                BatchItemResult(gateway, path, 200, resource=RESOURCES[path])
                for path in paths
            )

        client.get_resources_bulk.side_effect = answer
        for _ in range(4):
            await coordinator.async_refresh()
            assert coordinator.last_update_success
            now.now += 60
        seen = client.get_resources_bulk.await_count
        await coordinator.async_refresh()
    assert first_batches == [30, 15, 10, 10]
    # Opened after the third failed cycle (5 min), longer at the fourth.
    assert [round(pause / 60) for pause in pauses] == [5, 10]
    # Three good cycles at 10, then 20 for the next step.
    assert {
        len(call.args[1]) for call in client.get_resources_bulk.await_args_list[seen:]
    } <= {20, len(leaves) % 20}
    report = _gateway_diagnostics(1, coordinator)["icom"]
    assert report["system_components"] == [
        {"id": "189", "token": "1"},
        {"id": "11", "token": "2"},
        {"id": "4", "token": "3"},
    ]
    assert report["bus"] == "EMS2_0"
    assert report["bus_hint"] is None
    assert "Gateway" not in repr(report)


@pytest.mark.parametrize("paths", [15, 30])
def test_healthy_timing_shape_favours_full_batches(paths) -> None:
    """Case 6: one batch costs about 1 s plus 0.07 s per path."""
    profile = DATA["transport_profile"]

    def seconds(size: int) -> float:
        return (
            profile["healthy_seconds_base"] + profile["healthy_seconds_per_path"] * size
        )

    batches = -(-30 // paths)
    # Smaller batches cost more time in a healthy cycle; they only help
    # after a timeout, which is why the size recovers after good cycles.
    assert batches * seconds(paths) >= seconds(30)
