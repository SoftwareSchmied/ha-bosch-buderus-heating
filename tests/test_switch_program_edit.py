"""Z2: changing an EMS/iCom weekly switch program.

The rules run before anything is sent; the transaction reads the program
again, sends the whole week once as a raw array and reads it back. Every
test counts the PUT requests.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import write_window
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    switch_program_write_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    InvalidPayload,
    PointTClient,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    RetryPolicy,
    ServiceUnavailable,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    InvalidBatchEnvelope,
)
from custom_components.bosch_buderus_heating.switch_program_edit import (
    allowed_levels,
    copy_day,
    edit_issue,
    editable_program,
    encode_week,
    fingerprint,
    points_per_day,
    program_mode_path,
    replace_day,
    same_points,
    week_issue,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    SwitchPoint,
    assess_switch_program,
)
from custom_components.bosch_buderus_heating.writes import ControlUnused
from tests import test_dynamic_controls
from tests.switch_program_helpers import (
    DHW,
    MODE,
    PROGRAM_A,
    Gateway,
    payload,
    resource,
)
from tests.test_pointt_transport_and_client import serve

HEATING = resource(payload(PROGRAM_A))
READINGS = {path: resource(payload(path)) for path in (MODE,)} | {
    f"/heatingCircuits/hc1/temperatureLevels/{key}": resource(
        payload(f"/heatingCircuits/hc1/temperatureLevels/{key}")
    )
    for key in ("comfort2", "eco")
}


def program():
    shown = editable_program(HEATING, READINGS, program_mode=READINGS[MODE])
    assert shown is not None
    return shown


def moved(points, day=5, old=480, new=495):
    """The week with one point of a day moved by 15 minutes."""
    return tuple(
        SwitchPoint(point.day, new, point.level)
        if (point.day, point.minute) == (day, old)
        else point
        for point in points
    )


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


@pytest.fixture
def gateway(environment):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    return Gateway(coordinator, client)


# The rules ------------------------------------------------------------------


def test_the_shown_program_carries_its_reported_limits_and_levels():
    shown = program()
    assert (shown.max_switch_points, shown.max_switch_points_per_day) == (42, 6)
    assert shown.time_raster == 15
    assert shown.levels == ("comfort2", "eco")
    assert len(shown.points) == 14
    assert [point.minute for point in shown.day(5)] == [480, 1350]


@pytest.mark.parametrize(
    ("change", "token"),
    [
        (lambda week: (), "empty_week"),
        (lambda week: (*week, SwitchPoint(7, 0, "eco")), "invalid_day"),
        (lambda week: (*week, SwitchPoint(0, 1440, "eco")), "invalid_time"),
        (lambda week: (*week, SwitchPoint(0, -15, "eco")), "invalid_time"),
        (lambda week: (*week, (0, 600, "eco")), "invalid_day"),
        (lambda week: (*week, SwitchPoint(0, 605, "eco")), "off_raster"),
        (lambda week: (*week, SwitchPoint(0, 390, "eco")), "duplicate_time"),
        (
            lambda week: (
                *week,
                *(SwitchPoint(0, minute, "eco") for minute in (0, 15, 30, 45, 60)),
            ),
            "too_many_per_day",
        ),
        (lambda week: (*week, SwitchPoint(0, 600, "comfort1")), "level_not_allowed"),
        (lambda week: (*week, SwitchPoint(0, 600, 3)), "level_not_allowed"),
    ],
)
def test_each_rule_refuses_a_week(change, token):
    assert week_issue(change(program().points), program()) == token


def test_the_limit_of_the_whole_week():
    shown = replace(program(), max_switch_points=14)
    assert week_issue(shown.points, shown) is None
    more = (*shown.points, SwitchPoint(0, 600, "eco"))
    assert week_issue(more, shown) == "too_many_in_all"


@pytest.mark.parametrize("minute", [0, 1425])
def test_midnight_and_the_last_raster_minute_are_valid(minute):
    week = (*program().points, SwitchPoint(2, minute, "eco"))
    assert week_issue(week, program()) is None


def test_copying_a_day_replaces_the_targets_and_keeps_the_limit():
    shown = program()
    week = copy_day(shown.points, 5, [0, 1, 5])
    assert [p.minute for p in week if p.day == 0] == [480, 1350]
    assert [p.minute for p in week if p.day == 1] == [480, 1350]
    assert [p.minute for p in week if p.day == 2] == [390, 1320]
    assert week_issue(week, shown) is None
    # Copying a fuller day over the week can break the total limit.
    tight = replace(shown, max_switch_points=15)
    fuller = replace_day(
        shown.points,
        0,
        [SwitchPoint(0, minute, "eco") for minute in (0, 300, 600)],
    )
    assert week_issue(fuller, tight) is None
    assert week_issue(copy_day(fuller, 0, [1]), tight) == "too_many_in_all"


def test_the_wire_body_is_the_sorted_week_in_the_app_field_order():
    body = encode_week(reversed(program().points))
    assert body[0] == {"dayOfWeek": "Mo", "setpoint": "comfort2", "time": 390}
    assert [list(item) for item in body] == [["dayOfWeek", "setpoint", "time"]] * 14
    assert [item["dayOfWeek"] for item in body][-2:] == ["Su", "Su"]


@pytest.mark.parametrize(
    ("change", "issue"),
    [
        ({"writeable": 0}, "not_writable"),
        ({"available": False}, "unavailable"),
        ({"switchPoints": []}, "no_switch_points"),
        ({"switchPointTimeRaster": 7}, "invalid_limits"),
    ],
)
def test_programs_that_cannot_be_changed_here(change, issue):
    item = payload(PROGRAM_A) | change
    assert edit_issue(resource(item), READINGS) == issue
    assert editable_program(resource(item), READINGS) is None


def test_the_program_type_must_be_a_level_program():
    absolute = replace(READINGS[MODE], value="absolute")
    assert edit_issue(HEATING, READINGS, program_mode=absolute) == ("not_level_program")
    for value in ("level", "levels"):
        mode = replace(READINGS[MODE], value=value)
        assert edit_issue(HEATING, READINGS, program_mode=mode) is None


def test_hot_water_levels_come_only_from_the_listed_levels():
    dhw = resource(payload(DHW))
    listed = {
        f"/dhwCircuits/dhw1/temperatureLevels/{key}": dhw for key in ("high", "low")
    }
    # "off" is used but not listed: no change is offered.
    assert edit_issue(dhw, listed) == "level_not_listed"
    listed["/dhwCircuits/dhw1/temperatureLevels/off"] = dhw
    assert allowed_levels(DHW, assess_switch_program(dhw), listed) == (
        "high",
        "low",
        "off",
    )
    assert edit_issue(dhw, {}) == "levels_unknown"
    # Another circuit's levels never count.
    other = {"/dhwCircuits/dhw2/temperatureLevels/off": dhw}
    assert edit_issue(dhw, {**other, **dict(list(listed.items())[:2])}) == (
        "level_not_listed"
    )


def test_heating_levels_are_eco_and_comfort2_and_listed_used_ones():
    item = payload(PROGRAM_A)
    item["switchPoints"][0]["setpoint"] = "comfort1"
    unusual = resource(item)
    assert edit_issue(unusual, READINGS, program_mode=READINGS[MODE]) == (
        "level_not_listed"
    )
    listed = {**READINGS, "/heatingCircuits/hc1/temperatureLevels/comfort1": HEATING}
    levels = allowed_levels(PROGRAM_A, assess_switch_program(unusual), listed)
    assert set(levels) == {"comfort1", "comfort2", "eco"}
    assert edit_issue(unusual, listed, program_mode=READINGS[MODE]) is None


def test_fingerprint_ignores_the_order_but_not_the_content():
    item = payload(PROGRAM_A)
    item["switchPoints"].reverse()
    assert fingerprint(resource(item)) == fingerprint(HEATING)
    item["switchPoints"][0]["time"] += 15
    assert fingerprint(resource(item)) != fingerprint(HEATING)
    assert fingerprint(resource(payload(PROGRAM_A) | {"writeable": 0})) != (
        fingerprint(HEATING)
    )
    assert points_per_day(HEATING) == [2] * 7
    assert same_points(program().points, reversed(program().points))


# The service and the coordinator ---------------------------------------------


async def test_one_put_of_the_whole_week_is_confirmed_and_shown_at_once(gateway):
    coordinator = gateway.coordinator
    week = moved(program().points)
    confirmed = await coordinator.async_write_switch_program(HEATING, week)
    assert len(gateway.raw_puts) == 1
    path, body = gateway.raw_puts[0]
    assert path == PROGRAM_A
    assert body == encode_week(week)
    assert same_points(assess_switch_program(confirmed).points, week)
    snapshot = coordinator.data[PROGRAM_A]
    assert snapshot.resource == confirmed
    assert snapshot.freshness is Freshness.FRESH
    counters = coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["writes_sent"] == 1 and counters["writes_confirmed"] == 1
    assert gateway.value_puts == []


async def test_unchanged_week_reads_but_sends_nothing(gateway):
    await gateway.coordinator.async_write_switch_program(HEATING, program().points)
    # The program and its type are read again; nothing is sent.
    assert gateway.raw_puts == [] and gateway.gets == [PROGRAM_A, MODE]


async def test_a_change_in_the_app_meanwhile_sends_nothing(gateway):
    item = gateway.payloads[PROGRAM_A]
    item["switchPoints"][0]["time"] = 405
    with pytest.raises(WriteValidationError):
        await gateway.coordinator.async_write_switch_program(
            HEATING, moved(program().points)
        )
    assert gateway.raw_puts == []
    counters = gateway.coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["rejection_reasons"] == {"program_changed": 1}


async def test_a_shown_program_that_changed_in_ha_meanwhile_sends_nothing(gateway):
    newer = resource(payload(PROGRAM_A) | {"maxNbOfSwitchPoints": 41})
    with pytest.raises(WriteValidationError):
        await gateway.coordinator.async_write_switch_program(
            newer, moved(program().points)
        )
    assert gateway.raw_puts == [] and gateway.gets == []


async def test_an_invalid_week_is_refused_before_any_request(gateway):
    week = (*program().points, SwitchPoint(0, 605, "eco"))
    with pytest.raises(WriteValidationError) as error:
        await gateway.coordinator.async_write_switch_program(HEATING, week)
    assert error.value.reason == "program_off_raster"
    assert gateway.raw_puts == [] and gateway.gets == []


async def test_an_unused_program_is_never_written(gateway):
    coordinator = gateway.coordinator
    gateway.change(PROGRAM_A, used="false")
    shown = gateway.read(PROGRAM_A)
    assert shown.metadata.used is False
    coordinator.data[PROGRAM_A] = replace(coordinator.data[PROGRAM_A], resource=shown)
    with pytest.raises(ControlUnused):
        await coordinator.async_write_switch_program(shown, moved(program().points))
    assert gateway.raw_puts == []


@pytest.mark.parametrize("attempt", [1, 3])
async def test_a_late_read_back_still_confirms(gateway, attempt):
    original = gateway.get
    reads = 0

    async def get(gw, path, **kwargs):
        nonlocal reads
        result = await original(gw, path, **kwargs)
        if gateway.raw_puts:
            reads += 1
            if reads < attempt:
                return resource(payload(PROGRAM_A))
        return result

    gateway.client.get_resource.side_effect = get
    await gateway.coordinator.async_write_switch_program(
        HEATING, moved(program().points)
    )
    assert len(gateway.raw_puts) == 1 and reads == attempt


async def test_no_confirmation_leaves_the_reading_not_current_and_a_late_check(
    gateway,
):
    coordinator = gateway.coordinator

    async def ignore(gw, path, body):
        gateway.raw_puts.append((path, body))

    gateway.client.put_resource_raw_json.side_effect = ignore
    week = moved(program().points)
    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write_switch_program(HEATING, week)
    assert len(gateway.raw_puts) == 1
    assert coordinator.data[PROGRAM_A].freshness is Freshness.STALE
    assert PROGRAM_A in coordinator._urgent_reads
    stats = coordinator.switch_program_write_stats
    assert stats.snapshot("heating")["writes_unconfirmed"] == 1
    # A later regular reading that shows the week confirms it late.
    gateway.payloads[PROGRAM_A]["switchPoints"] = encode_week(week)
    stats.observe(gateway.read(PROGRAM_A), 0.0)
    assert stats.snapshot("heating")["late_confirmed"] == 1


@pytest.mark.parametrize("status", [400, 409, 422])
async def test_a_refusal_of_the_gateway_is_final_and_not_read_back(gateway, status):
    gateway.client.put_resource_raw_json.side_effect = UnexpectedHttpStatus(status)
    with pytest.raises(UnexpectedHttpStatus):
        await gateway.coordinator.async_write_switch_program(
            HEATING, moved(program().points)
        )
    assert gateway.gets == [PROGRAM_A, MODE]
    assert gateway.coordinator.data[PROGRAM_A].freshness is Freshness.FRESH
    counters = gateway.coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["rejection_reasons"] == {f"device_rejected_{status}": 1}


@pytest.mark.parametrize(
    "error", [ServiceUnavailable(502), RequestTimeout("lost"), InvalidPayload("x")]
)
async def test_an_unknown_outcome_is_resolved_by_reading_never_by_resending(
    gateway, error
):
    original = gateway.put_raw

    async def applied_then_lost(gw, path, body):
        await original(gw, path, body)
        raise error

    gateway.client.put_resource_raw_json.side_effect = applied_then_lost
    await gateway.coordinator.async_write_switch_program(
        HEATING, moved(program().points)
    )
    assert len(gateway.raw_puts) == 1
    assert gateway.coordinator.data[PROGRAM_A].freshness is Freshness.FRESH


async def test_a_rate_limit_is_kept_and_nothing_is_sent_meanwhile(gateway):
    coordinator = gateway.coordinator
    gateway.client.put_resource_raw_json.side_effect = RateLimited(120)
    with pytest.raises(RateLimited):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    gateway.client.put_resource_raw_json.side_effect = gateway.put_raw
    with pytest.raises(RateLimited):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert gateway.raw_puts == []


async def test_the_breaker_and_a_missing_reading_send_nothing(gateway):
    coordinator = gateway.coordinator
    coordinator._circuit_open_until = coordinator_module.monotonic() + 60
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    coordinator._circuit_open_until = 0.0
    coordinator.data[PROGRAM_A] = replace(
        coordinator.data[PROGRAM_A], freshness=Freshness.STALE
    )
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert gateway.raw_puts == [] and gateway.gets == []


async def test_the_lock_and_the_deadline_apply(gateway):
    coordinator = gateway.coordinator

    async def hang(*_args):
        await asyncio.Event().wait()

    gateway.client.put_resource_raw_json.side_effect = hang
    with (
        patch.object(write_window, "send_budget", return_value=0.0),
        patch.object(coordinator_module, "WRITE_STEP_MARGIN_SECONDS", 0.0),
        patch.object(coordinator, "write_read_back_delays", return_value=(0.0,)),
        patch.object(
            coordinator_module,
            "WRITE_TRANSACTION_DEADLINE",
            coordinator_module.timedelta(seconds=0.05),
        ),
        pytest.raises(WriteNotConfirmed),
    ):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert coordinator.data[PROGRAM_A].freshness is Freshness.STALE
    assert not coordinator._update_lock.locked()
    counters = coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["writes_sent"] == 1 and counters["writes_unconfirmed"] == 1


async def test_a_hot_water_program_counts_for_its_own_bucket(gateway):
    coordinator = gateway.coordinator
    dhw = coordinator.data[DHW].resource
    week = tuple(
        SwitchPoint(point.day, 360, point.level)
        if point.minute == 345 and point.day == 6
        else point
        for point in assess_switch_program(dhw).points
    )
    await coordinator.async_write_switch_program(dhw, week)
    assert gateway.raw_puts[0][0] == DHW
    assert coordinator.switch_program_write_stats.snapshot("dhw")["writes_sent"] == 1


async def test_diagnostics_have_counters_and_counts_but_no_times(gateway):
    coordinator = gateway.coordinator
    await coordinator.async_write_switch_program(HEATING, moved(program().points))
    report = switch_program_write_diagnostics(coordinator)
    assert report["heating"]["writes_confirmed"] == 1
    programs = report["programs"]
    assert programs[PROGRAM_A] == {
        "editable": True,
        "edit_issue": None,
        "points_per_day": [2] * 7,
    }
    text = repr(report)
    for marker in ("495", "1350", "comfort2", "Home office", "08:15"):
        assert marker not in text


# The client call --------------------------------------------------------------


@pytest.mark.usefixtures("socket_enabled")
async def test_the_client_sends_a_raw_array_once() -> None:
    requests: list[object] = []

    async def handle(request: web.Request) -> web.Response:
        requests.append((request.path, await request.json()))
        return web.Response(status=503 if len(requests) > 1 else 204)

    route = [("PUT", "/gateways/{id}/resource/{tail:.*}", handle)]
    body = encode_week(program().points)
    no_wait = RetryPolicy(attempts=3, base_delay=0, maximum_delay=0)
    async with serve(route) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url, retry_policy=no_wait)
        assert await client.put_resource_raw_json("gw", PROGRAM_A, body) is None
        with pytest.raises(ServiceUnavailable):
            await client.put_resource_raw_json("gw", PROGRAM_A, body)
        with pytest.raises(ValueError):
            await client.put_resource_raw_json("gw", PROGRAM_A, {"value": body})  # type: ignore[arg-type]
    assert requests == [
        ("/gateways/gw/resource/heatingCircuits/hc1/switchPrograms/A", body),
        ("/gateways/gw/resource/heatingCircuits/hc1/switchPrograms/A", body),
    ]


async def test_a_slow_check_before_the_program_write_sends_nothing(gateway):
    """D25: the fresh read leaves too little time for the PUT and its
    read-back; nothing is sent, nothing is stale, nothing is counted."""
    from custom_components.bosch_buderus_heating.writes import WriteBusy

    coordinator = gateway.coordinator
    clock = [1000.0]
    original = gateway.get

    async def slow(gateway_id, path, **kwargs):
        clock[0] += 45.0
        return await original(gateway_id, path, **kwargs)

    gateway.client.get_resource.side_effect = slow
    with (
        patch.object(write_window, "_now", lambda: clock[0]),
        pytest.raises(WriteBusy),
    ):
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert gateway.raw_puts == []
    assert coordinator.data[PROGRAM_A].freshness is Freshness.FRESH
    counters = coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["writes_sent"] == 0 and counters["writes_unconfirmed"] == 0
    assert not coordinator._update_lock.locked()


# Program type, use and unreadable answers ----------------------------------


def test_a_program_whose_type_is_not_current_is_not_offered():
    """The circuit reports its program type; without a current reading
    of it the program is not offered (no longer fail-open)."""
    assert MODE in READINGS
    assert edit_issue(HEATING, READINGS) == "program_mode_unknown"
    assert editable_program(HEATING, READINGS) is None
    # A circuit that does not report one keeps its program.
    without = {key: value for key, value in READINGS.items() if key != MODE}
    assert edit_issue(HEATING, without) is None
    # A reading of another path never counts as the program type.
    other = replace(READINGS[MODE], path="/heatingCircuits/hc2/switchProgramMode")
    assert edit_issue(HEATING, READINGS, program_mode=other) == ("program_mode_unknown")
    # Hot water has no program type.
    dhw = resource(payload(DHW))
    assert program_mode_path(DHW) is None
    assert edit_issue(dhw, READINGS) != "program_mode_unknown"


def test_a_program_the_gateway_does_not_use_is_not_offered():
    """Consistent with the write, which refuses ``used: false``."""
    item = payload(PROGRAM_A) | {"used": "false"}
    assert edit_issue(resource(item), READINGS, program_mode=READINGS[MODE]) == (
        "not_used"
    )
    assert editable_program(resource(item), READINGS) is None


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ({"value": "absolute"}, "program_mode_not_level"),
        (RequestTimeout("lost"), "program_mode_unknown"),
        (ServiceUnavailable(503), "program_mode_unknown"),
    ],
)
async def test_the_program_type_is_read_again_before_the_put(gateway, mode, reason):
    """An absolute program, or one whose type cannot be read now, is
    refused before sending, also when it changed after it was shown."""
    original = gateway.get

    async def get(gw, path, **kwargs):
        if path == MODE and isinstance(mode, Exception):
            raise mode
        return await original(gw, path, **kwargs)

    if isinstance(mode, dict):
        gateway.change(MODE, **mode)
    gateway.client.get_resource.side_effect = get
    coordinator = gateway.coordinator
    with pytest.raises(WriteValidationError) as caught:
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert caught.value.reason == reason
    assert gateway.raw_puts == []
    assert coordinator.data[PROGRAM_A].freshness is Freshness.FRESH
    counters = coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["writes_sent"] == 0
    assert counters["rejection_reasons"] == {reason: 1}


@pytest.mark.parametrize("error", [RateLimited(60), AuthenticationError("expired")])
async def test_a_program_type_read_keeps_a_pause_or_sign_in(gateway, error):
    original = gateway.get

    async def get(gw, path, **kwargs):
        if path == MODE:
            raise error
        return await original(gw, path, **kwargs)

    gateway.client.get_resource.side_effect = get
    # Starting the real reauthentication flow in the background would race the
    # test teardown; the synthetic entry intentionally has no sign-in data.
    with (
        patch.object(gateway.coordinator._config_entry, "async_start_reauth") as reauth,
        pytest.raises(type(error)),
    ):
        await gateway.coordinator.async_write_switch_program(
            HEATING, moved(program().points)
        )
    assert reauth.call_count == int(isinstance(error, AuthenticationError))
    assert gateway.raw_puts == []


@pytest.mark.parametrize(
    "error",
    [
        ProtocolError("answer too large"),
        InvalidBatchEnvelope("unexpected envelope"),
    ],
)
async def test_an_unreadable_answer_to_the_put_is_resolved_by_reading(gateway, error):
    """A 2xx answer that cannot be read is no refusal; the week may be
    stored, so only reading decides, and nothing is sent again."""
    original = gateway.put_raw

    async def applied(gw, path, body):
        await original(gw, path, body)
        raise error

    gateway.client.put_resource_raw_json.side_effect = applied
    coordinator = gateway.coordinator
    confirmed = await coordinator.async_write_switch_program(
        HEATING, moved(program().points)
    )
    assert len(gateway.raw_puts) == 1
    assert confirmed.path == PROGRAM_A
    counters = coordinator.switch_program_write_stats.snapshot("heating")
    assert counters["writes_confirmed"] == 1
    assert counters["writes_rejected"] == 0


async def test_the_breaker_has_its_own_refusal(gateway):
    coordinator = gateway.coordinator
    coordinator._circuit_open_until = coordinator_module.monotonic() + 60
    with pytest.raises(WriteValidationError) as caught:
        await coordinator.async_write_switch_program(HEATING, moved(program().points))
    assert caught.value.reason == "circuit_breaker"
    assert gateway.raw_puts == [] and gateway.gets == []
