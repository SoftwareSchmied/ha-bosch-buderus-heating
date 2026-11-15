"""TP-06: the action ``set_switch_points``.

The action writes what the dialog may write: the same rules, one fresh
reading as its base, a second reading right before the single PUT, a
read-back, never a retry. Every test counts the PUT requests.
"""

from __future__ import annotations

import copy
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.ha_schema import validation as vol
from custom_components.bosch_buderus_heating.pointt import UnexpectedHttpStatus
from custom_components.bosch_buderus_heating.switch_point_action import (
    SERVICE_SCHEMA,
    SERVICE_SET_SWITCH_POINTS,
    async_set_switch_points,
    async_setup_actions,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from tests import test_dynamic_controls
from tests.program_kind_helpers import CountingGateway
from tests.program_kind_helpers import payloads as fixture_payloads
from tests.switch_program_helpers import DHW, MODE, PAYLOADS, PROGRAM_A, PROGRAM_B

COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
SCHEMA = cv.make_entity_service_schema(SERVICE_SCHEMA)
WEEK = [
    {"day": day, "time": time, "level": level}
    for day in ("mo", "tu", "we", "th", "fr", "sa", "su")
    for time, level in (("06:00", "comfort2"), ("22:00", "eco"))
]


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


@pytest.fixture
def gateway(environment):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    return CountingGateway(coordinator, client, copy.deepcopy(PAYLOADS))


def entity(gateway, path=PROGRAM_A, entity_id="sensor.time_program_a"):
    return SimpleNamespace(
        entity_id=entity_id,
        coordinator=gateway.coordinator,
        entity_description=SimpleNamespace(
            value_kind="switch_program", resource_path=path
        ),
    )


def call(**data):
    validated = SCHEMA({"entity_id": ["sensor.x"], **data})
    validated.pop("entity_id", None)
    return SimpleNamespace(data=validated, return_response=True)


async def run(gateway, *targets, **data):
    return await async_set_switch_points(
        list(targets) or [entity(gateway)], call(**data)
    )


async def test_valid_week_is_sent_once_and_answered(gateway):
    response = await run(gateway, switch_points=WEEK)
    assert len(gateway.raw_puts) == 1 and gateway.puts == 1
    path, body = gateway.raw_puts[0]
    assert path == PROGRAM_A
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "comfort2", "time": 360}
    answer = response["sensor.time_program_a"]
    assert answer["result"] == "sent" and answer["confirmed"] is True
    assert answer["previous"]["mo"][0] == {"time": "06:30", "level": "comfort2"}
    assert answer["switch_points"]["mo"] == [
        {"time": "06:00", "level": "comfort2"},
        {"time": "22:00", "level": "eco"},
    ]
    # The base is a fresh reading: the program is read twice before the PUT.
    assert gateway.gets.count(PROGRAM_A) >= 3


async def test_replace_days_keeps_the_other_days(gateway):
    before = copy.deepcopy(gateway.payloads[PROGRAM_A]["switchPoints"])
    await run(
        gateway,
        mode="replace_days",
        switch_points=[
            {"day": "mo", "time": "05:00", "level": "comfort2"},
            {"day": "tu", "time": "05:15", "level": "eco"},
        ],
    )
    ((_path, body),) = gateway.raw_puts
    assert [item for item in body if item["dayOfWeek"] == "Mo"] == [
        {"dayOfWeek": "Mo", "setpoint": "comfort2", "time": 300}
    ]
    assert [item for item in body if item["dayOfWeek"] not in ("Mo", "Tu")] == [
        {"dayOfWeek": item["dayOfWeek"], "setpoint": item["setpoint"], "time": item["time"]}
        for item in before
        if item["dayOfWeek"] not in ("Mo", "Tu")
    ]


@pytest.mark.parametrize(
    "point",
    [
        {"day": "xx", "time": "06:00", "level": "eco"},
        {"day": "mo", "time": "6:00 Uhr", "level": "eco"},
        {"day": "mo", "time": "06:00:30", "level": "eco"},
        {"day": "mo", "time": "06:00"},
        {"day": "mo", "time": "06:00", "level": "eco", "extra": 1},
    ],
)
async def test_schema_refuses_malformed_points(gateway, point):
    with pytest.raises(vol.Invalid):
        call(switch_points=[point])
    assert gateway.gets == [] and gateway.puts == 0


async def test_schema_refuses_unknown_fields_and_modes():
    with pytest.raises(vol.Invalid):
        call(switch_points=WEEK, mode="replace_all")
    with pytest.raises(vol.Invalid):
        call(switch_points=WEEK, something=1)


async def test_empty_list_is_refused(gateway):
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=[])
    assert err.value.translation_key == "switch_points_empty_week"
    assert gateway.puts == 0


@pytest.mark.parametrize(
    ("points", "key", "placeholder"),
    [
        ([{"day": "mo", "time": "06:05", "level": "eco"}], "switch_points_invalid_time", "raster"),
        ([{"day": "mo", "time": "06:00", "level": "comfort1"}], "switch_points_level_not_allowed", "levels"),
        (
            [{"day": "mo", "time": f"{hour:02d}:00", "level": "eco"} for hour in range(7)],
            "switch_points_too_many_per_day",
            "per_day",
        ),
        (
            [
                {"day": "mo", "time": "06:00", "level": "eco"},
                {"day": "mo", "time": "06:00:00", "level": "comfort2"},
            ],
            "switch_points_duplicate_time",
            None,
        ),
    ],
)
async def test_rules_refuse_with_their_own_key(gateway, points, key, placeholder):
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=points)
    assert err.value.translation_key == key
    if placeholder:
        assert placeholder in err.value.translation_placeholders
    assert gateway.puts == 0


async def test_too_many_in_all_is_refused(gateway):
    gateway.change(PROGRAM_A, maxNbOfSwitchPoints=14)
    gateway.refresh()
    points = [*WEEK, {"day": "mo", "time": "12:00", "level": "eco"}]
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=points)
    assert err.value.translation_key == "switch_points_too_many_in_all"
    assert err.value.translation_placeholders["total"] == "14"
    assert gateway.puts == 0


async def test_expected_mismatch_sends_nothing(gateway):
    with pytest.raises(ServiceValidationError) as err:
        await run(
            gateway,
            switch_points=WEEK,
            expected=[{"day": "mo", "time": "06:00", "level": "eco"}],
        )
    assert err.value.translation_key == "switch_points_program_changed"
    assert gateway.puts == 0


async def test_matching_expected_allows_the_write(gateway):
    current = [
        {
            "day": item["dayOfWeek"].lower(),
            "time": f"{item['time'] // 60:02d}:{item['time'] % 60:02d}",
            "level": item["setpoint"],
        }
        for item in gateway.payloads[PROGRAM_A]["switchPoints"]
    ]
    await run(gateway, switch_points=WEEK, expected=current)
    assert gateway.puts == 1


async def test_validate_only_never_sends(gateway):
    response = await run(gateway, switch_points=WEEK, validate_only=True)
    assert response["sensor.time_program_a"]["result"] == "validated"
    assert gateway.puts == 0


async def test_same_week_is_not_sent(gateway):
    current = [
        {
            "day": item["dayOfWeek"].lower(),
            "time": f"{item['time'] // 60:02d}:{item['time'] % 60:02d}",
            "level": item["setpoint"],
        }
        for item in gateway.payloads[PROGRAM_A]["switchPoints"]
    ]
    response = await run(gateway, switch_points=current)
    answer = response["sensor.time_program_a"]
    assert answer["result"] == "unchanged" and answer["confirmed"] is True
    assert gateway.puts == 0


async def test_change_between_the_two_readings_sends_nothing(gateway):
    reads = {"count": 0}
    original = gateway.get

    async def get(gateway_id, path, **kwargs):
        if path == PROGRAM_A:
            reads["count"] += 1
            if reads["count"] == 2:
                # Changed in the app right after the first reading.
                gateway.change(
                    PROGRAM_A,
                    switchPoints=[{"dayOfWeek": "Mo", "time": 0, "setpoint": "eco"}],
                )
        return await original(gateway_id, path, **kwargs)

    gateway.client.get_resource.side_effect = get
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == "switch_points_program_changed"
    assert gateway.puts == 0


async def test_unreadable_or_absolute_program_type_is_refused(gateway):
    gateway.change(MODE, value="absolute")
    gateway.refresh()
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == "switch_points_not_level_program"
    assert gateway.puts == 0


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"used": False}, "switch_points_unused"),
        ({"writeable": 0}, "switch_points_not_writable"),
        ({"available": "false"}, "switch_points_not_writable"),
    ],
)
async def test_flags_lock_the_program(gateway, change, key):
    gateway.change(PROGRAM_A, **change)
    gateway.refresh()
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == key
    assert gateway.puts == 0


@pytest.mark.parametrize(
    ("status", "key"),
    [(400, "switch_points_rejected"), (403, "switch_points_rejected")],
)
async def test_refused_put_is_not_repeated(gateway, status, key):
    gateway.client.put_resource_raw_json.side_effect = UnexpectedHttpStatus(status)
    with pytest.raises(HomeAssistantError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == key
    assert gateway.client.put_resource_raw_json.await_count == 1


async def test_unconfirmed_write_is_not_repeated(gateway):
    gateway.ignore_writes = True
    with pytest.raises(HomeAssistantError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == "switch_points_not_confirmed"
    assert len(gateway.raw_puts) == 1


async def test_server_error_on_read_back_is_not_confirmed(gateway):
    original = gateway.get
    sent = {"done": False}

    async def put(gateway_id, path, body):
        sent["done"] = True
        await gateway.put_raw(gateway_id, path, body)

    async def get(gateway_id, path, **kwargs):
        if sent["done"]:
            raise UnexpectedHttpStatus(500)
        return await original(gateway_id, path, **kwargs)

    gateway.client.put_resource_raw_json.side_effect = put
    gateway.client.get_resource.side_effect = get
    with pytest.raises(HomeAssistantError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key == "switch_points_not_confirmed"
    assert gateway.client.put_resource_raw_json.await_count == 1


async def test_cloud_pause_sends_nothing(gateway):
    gateway.coordinator._cloud_backoff_until = monotonic() + 600
    with pytest.raises(HomeAssistantError) as err:
        await run(gateway, switch_points=WEEK)
    assert err.value.translation_key.startswith("command_cloud_")
    assert gateway.gets == [] and gateway.puts == 0


async def test_protection_pause_sends_nothing(gateway):
    gateway.coordinator._circuit_open_until = monotonic() + 600
    with pytest.raises(HomeAssistantError):
        await run(gateway, switch_points=WEEK)
    assert gateway.puts == 0


async def test_two_targets_are_written_one_after_the_other(gateway):
    dhw_week = [
        {"day": day, "time": "06:00", "level": "high"}
        for day in ("mo", "tu", "we", "th", "fr", "sa", "su")
    ]
    gateway.change(DHW, writeable=1)
    gateway.refresh()
    await run(
        gateway,
        entity(gateway, PROGRAM_A),
        entity(gateway, PROGRAM_B, "sensor.time_program_b"),
        switch_points=WEEK,
    )
    assert [path for path, _body in gateway.raw_puts] == [PROGRAM_A, PROGRAM_B]
    del dhw_week


async def test_one_failed_target_keeps_the_other(gateway):
    gateway.change(PROGRAM_B, used=False)
    gateway.refresh()
    with pytest.raises(ServiceValidationError) as err:
        await run(
            gateway,
            entity(gateway, PROGRAM_A),
            entity(gateway, PROGRAM_B, "sensor.time_program_b"),
            switch_points=WEEK,
        )
    assert err.value.translation_key == "switch_points_unused"
    assert [path for path, _body in gateway.raw_puts] == [PROGRAM_A]


async def test_foreign_or_missing_targets_are_refused(gateway):
    other = SimpleNamespace(
        entity_id="sensor.outdoor",
        coordinator=gateway.coordinator,
        entity_description=SimpleNamespace(value_kind="value", resource_path="/x"),
    )
    with pytest.raises(ServiceValidationError) as err:
        await run(gateway, entity(gateway), other, switch_points=WEEK)
    assert err.value.translation_key == "not_a_program_entity"
    with pytest.raises(ServiceValidationError) as err:
        await async_set_switch_points([], call(switch_points=WEEK))
    assert err.value.translation_key == "switch_points_no_target"
    assert gateway.puts == 0


async def test_answer_holds_no_path_or_identifier(gateway):
    response = await run(gateway, switch_points=WEEK)
    text = repr(response)
    assert "/heatingCircuits" not in text
    assert "synthetic-gateway" not in text
    assert set(response["sensor.time_program_a"]["switch_points"]) <= {
        "mo", "tu", "we", "th", "fr", "sa", "su"
    }


async def test_absolute_program_takes_numbers(environment):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    items = fixture_payloads("connectkey/switch-programs-absolute.json")
    gateway = CountingGateway(coordinator, client, items)
    target = entity(gateway, "/heatingCircuits/hc1/switchPrograms/B")
    await run(
        gateway,
        target,
        switch_points=[{"day": "mo", "time": "06:00", "level": "21.5"}],
        mode="replace_days",
    )
    ((_path, body),) = gateway.raw_puts
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "21.5", "time": 360}
    with pytest.raises(ServiceValidationError) as err:
        await run(
            gateway,
            target,
            switch_points=[{"day": "mo", "time": "06:00", "level": "21.2"}],
            mode="replace_days",
        )
    assert err.value.translation_key == "switch_points_value_not_allowed"
    assert err.value.translation_placeholders == {
        "minimum": "5",
        "maximum": "30",
        "step": "0.5",
    }


async def test_action_is_registered_with_an_optional_response(hass):
    async_setup_actions(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_SWITCH_POINTS)
    assert (
        hass.services.supports_response(DOMAIN, SERVICE_SET_SWITCH_POINTS)
        is SupportsResponse.OPTIONAL
    )


def test_services_yaml_matches_the_schema_and_translations():
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    fields = services[SERVICE_SET_SWITCH_POINTS]["fields"]
    schema_keys = {str(key) for key in SERVICE_SCHEMA}
    assert set(fields) == schema_keys
    assert services[SERVICE_SET_SWITCH_POINTS]["target"]["entity"] == {
        "integration": DOMAIN,
        "domain": "sensor",
    }
    for path in [COMPONENT / "strings.json", *sorted((COMPONENT / "translations").glob("*.json"))]:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        texts = data["services"][SERVICE_SET_SWITCH_POINTS]
        assert set(texts["fields"]) == schema_keys, path
        assert texts["name"] and texts["description"], path
        assert set(data["selector"]["switch_points_mode"]["options"]) == {
            "replace_week",
            "replace_days",
        }, path
