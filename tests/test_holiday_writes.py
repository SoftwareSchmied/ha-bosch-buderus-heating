"""Tests for validated PointT holiday calendar writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_buderus_heating.holiday_writes import (
    HolidayWriteService,
    configure_holiday_values,
    create_holiday_values,
    holiday_resources_from_snapshots,
    update_holiday_values,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_LIST_PATH,
    HOLIDAY_TIMEZONE_PATH,
    HolidayNameCodec,
    HolidayPeriod,
    HolidayWriteConfiguration,
    HolidayWriteValues,
    parse_holiday_state,
)
from custom_components.bosch_buderus_heating.pointt import (
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)


def _configuration(*, date_time_mode: str = "dateTime") -> HolidayWriteConfiguration:
    return HolidayWriteConfiguration(
        date_time_mode=date_time_mode,
        assigned_to=("hc1", "dhw1"),
        heating_mode="FIX_TEMPERATURE",
        dhw_mode="OFF",
        ventilation_mode=None,
        thermal_disinfection="ON",
        fix_temperature=17.0,
        name_codec=HolidayNameCodec("BASE64", "UTF8", 32),
        heating_modes=("OFF", "ECO", "FIX_TEMPERATURE", "SATURDAY"),
        dhw_modes=("OFF", "ECO", "LOW", "HIGH", "OFF_TD", "SATURDAY"),
        ventilation_modes=("OFF", "MIN", "RED", "NOM", "MAX", "DEM"),
        thermal_disinfection_modes=("ON", "OFF"),
        fix_temperature_min=10.0,
        fix_temperature_max=25.0,
    )


def _values() -> HolidayWriteValues:
    return HolidayWriteValues(
        start_date="2030-08-01T08:00:00",
        end_date="2030-08-08T18:00:00",
        heating_mode="FIX_TEMPERATURE",
        dhw_mode="OFF",
        ventilation_mode=None,
        assigned_to=("hc1", "dhw1"),
        name="VGVzdA==",
        thermal_disinfection="ON",
        fix_temperature=17.0,
    )


def _resource(holiday_id: int, values: HolidayWriteValues) -> Resource:
    return Resource(
        path=HOLIDAY_LIST_PATH,
        value=[{"id": holiday_id, **values.as_payload()}],
        has_value=True,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"values": None},
        {"value": [{"id": 7}]},
        {"value": [{"id": 7, "startDate": "bad", "endDate": "bad"}]},
        {"value": [None]},
        {"value": {"periods": []}},
        {"value": [{"id": 8, **_values().as_payload()}] * 65},
        {"value": [{"id": 8, **_values().as_payload()}] * 2},
    ],
)
async def test_delete_requires_a_complete_valid_list(payload: dict) -> None:
    from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource

    client = AsyncMock()
    client.get_resource.return_value = parse_resource(payload, path=HOLIDAY_LIST_PATH)
    service = HolidayWriteService(client, sleep=AsyncMock())
    with pytest.raises(WriteNotConfirmed):
        await service.async_delete(
            "gateway-one",
            {HOLIDAY_LIST_PATH: _resource(7, _values())},
            7,
            fallback_timezone="UTC",
        )
    client.delete_holiday_period.assert_awaited_once()
    assert client.get_resource.await_count == 3


async def test_delete_accepts_explicit_empty_values_list() -> None:
    client = AsyncMock()
    client.get_resource.return_value = Resource(path=HOLIDAY_LIST_PATH, has_values=True)
    await HolidayWriteService(client, sleep=AsyncMock()).async_delete(
        "gateway-one",
        {HOLIDAY_LIST_PATH: _resource(7, _values())},
        7,
        fallback_timezone="UTC",
    )
    client.get_resource.assert_awaited_once()


def test_service_rejects_invalid_confirmation_configuration() -> None:
    with pytest.raises(ValueError, match="negative"):
        HolidayWriteService(AsyncMock(), read_back_delay=-1)
    with pytest.raises(ValueError, match="positive"):
        HolidayWriteService(AsyncMock(), read_back_attempts=0)


async def test_create_is_sent_once_and_confirmed_by_a_new_id() -> None:
    client = AsyncMock()
    client.get_resource.return_value = _resource(2, _values())
    sleep = AsyncMock()
    service = HolidayWriteService(client, sleep=sleep, read_back_delay=0)
    previous = _resource(1, _values())
    client.get_resource.side_effect = [previous, client.get_resource.return_value]

    result = await service.async_create(
        "gateway-one",
        {HOLIDAY_LIST_PATH: previous},
        _values(),
        fallback_timezone="UTC",
    )

    assert result == client.get_resource.return_value
    client.create_holiday_period.assert_awaited_once_with(
        "gateway-one", _values().as_payload()
    )
    assert (
        client.get_resource.await_args_list
        == [call("gateway-one", HOLIDAY_LIST_PATH)] * 2
    )


async def test_timed_out_create_is_not_repeated() -> None:
    client = AsyncMock()
    client.create_holiday_period.side_effect = RequestTimeout()
    client.get_resource.return_value = _resource(1, _values())
    client.get_resource.side_effect = [
        Resource(path=HOLIDAY_LIST_PATH, has_values=True),
        client.get_resource.return_value,
    ]
    service = HolidayWriteService(client, sleep=AsyncMock(), read_back_delay=0)

    await service.async_create("gateway-one", {}, _values(), fallback_timezone="UTC")

    assert client.create_holiday_period.await_count == 1


async def test_update_and_delete_require_matching_read_back() -> None:
    client = AsyncMock()
    service = HolidayWriteService(client, sleep=AsyncMock(), read_back_delay=0)
    existing = _resource(7, _values())
    client.get_resource.return_value = existing

    await service.async_update(
        "gateway-one",
        {HOLIDAY_LIST_PATH: existing},
        7,
        _values(),
        fallback_timezone="UTC",
    )
    client.update_holiday_period.assert_awaited_once_with(
        "gateway-one", 7, _values().as_payload()
    )

    client.get_resource.return_value = Resource(
        path=HOLIDAY_LIST_PATH, value=[], has_value=True
    )
    await service.async_delete(
        "gateway-one", {HOLIDAY_LIST_PATH: existing}, 7, fallback_timezone="UTC"
    )
    client.delete_holiday_period.assert_awaited_once_with("gateway-one", 7)


async def test_unconfirmed_write_fails_after_bounded_read_back() -> None:
    client = AsyncMock()
    client.get_resource.return_value = Resource(
        path=HOLIDAY_LIST_PATH, value=[], has_value=True
    )
    client.get_resource.side_effect = [
        _resource(7, _values()),
        client.get_resource.return_value,
        client.get_resource.return_value,
    ]
    service = HolidayWriteService(
        client, sleep=AsyncMock(), read_back_delay=0, read_back_attempts=2
    )

    with pytest.raises(WriteNotConfirmed):
        await service.async_update(
            "gateway-one",
            {HOLIDAY_LIST_PATH: _resource(7, _values())},
            7,
            _values(),
            fallback_timezone="UTC",
        )

    assert client.update_holiday_period.await_count == 1
    assert client.get_resource.await_count == 3


async def test_temporary_read_back_errors_are_bounded() -> None:
    client = AsyncMock()
    client.get_resource.side_effect = [
        Resource(path=HOLIDAY_LIST_PATH, has_values=True),
        ServiceUnavailable(),
        RequestTimeout(),
    ]
    service = HolidayWriteService(
        client, sleep=AsyncMock(), read_back_delay=0, read_back_attempts=2
    )

    with pytest.raises(WriteNotConfirmed, match="timed out"):
        await service.async_create(
            "gateway-one", {}, _values(), fallback_timezone="UTC"
        )

    assert client.create_holiday_period.await_count == 1
    assert client.get_resource.await_count == 3


async def test_create_does_not_confirm_a_preexisting_uncached_period() -> None:
    client = AsyncMock()
    client.get_resource.return_value = _resource(7, _values())
    client.create_holiday_period.side_effect = RequestTimeout()
    with pytest.raises(WriteNotConfirmed):
        await HolidayWriteService(client, sleep=AsyncMock()).async_create(
            "gateway-one", {}, _values(), fallback_timezone="UTC"
        )
    assert client.mock_calls[0] == call.get_resource("gateway-one", HOLIDAY_LIST_PATH)
    client.create_holiday_period.assert_awaited_once()


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_invalid_live_holiday_list_prevents_mutation(operation: str) -> None:
    client = AsyncMock()
    client.get_resource.return_value = Resource(path=HOLIDAY_LIST_PATH)
    service = HolidayWriteService(client, sleep=AsyncMock())
    resources = {HOLIDAY_LIST_PATH: _resource(7, _values())}
    with pytest.raises(WriteValidationError, match="incomplete"):
        if operation == "create":
            await service.async_create(
                "gateway-one", resources, _values(), fallback_timezone="UTC"
            )
        else:
            await service.async_update(
                "gateway-one", resources, 7, _values(), fallback_timezone="UTC"
            )
    client.create_holiday_period.assert_not_awaited()
    client.update_holiday_period.assert_not_awaited()


async def test_date_edit_preserves_live_modes_and_temperature() -> None:
    client = AsyncMock()
    baseline = _values()
    desired = replace(baseline, start_date="2030-08-02T08:00:00")
    live = replace(baseline, fix_temperature=21.0, dhw_mode="HIGH")
    merged = replace(live, start_date=desired.start_date)
    client.get_resource.side_effect = [_resource(7, live), _resource(7, merged)]
    await HolidayWriteService(client, sleep=AsyncMock()).async_update(
        "gateway-one",
        {HOLIDAY_LIST_PATH: _resource(7, baseline)},
        7,
        desired,
        expected=baseline,
        fallback_timezone="UTC",
    )
    client.update_holiday_period.assert_awaited_once_with(
        "gateway-one", 7, merged.as_payload()
    )
    assert client.mock_calls[0] == call.get_resource("gateway-one", HOLIDAY_LIST_PATH)


async def test_options_edit_preserves_calendar_change_while_waiting_for_lock() -> None:
    client = AsyncMock()
    baseline = _values()
    desired = replace(baseline, dhw_mode="HIGH")
    live = replace(baseline, start_date="2030-08-02T08:00:00")
    merged = replace(live, dhw_mode="HIGH")
    client.get_resource.side_effect = [_resource(7, live), _resource(7, merged)]
    await HolidayWriteService(client, sleep=AsyncMock()).async_update(
        "gateway-one",
        {HOLIDAY_LIST_PATH: _resource(7, live)},
        7,
        desired,
        expected=baseline,
        fallback_timezone="UTC",
    )
    client.update_holiday_period.assert_awaited_once_with(
        "gateway-one", 7, merged.as_payload()
    )


@pytest.mark.parametrize(
    "live",
    [
        replace(_values(), fix_temperature=22.0),
        None,
    ],
)
async def test_changed_or_removed_holiday_rejects_conflicting_update(live) -> None:
    client = AsyncMock()
    client.get_resource.return_value = (
        _resource(7, live)
        if live
        else Resource(path=HOLIDAY_LIST_PATH, has_values=True)
    )
    with pytest.raises(WriteValidationError):
        await HolidayWriteService(client, sleep=AsyncMock()).async_update(
            "gateway-one",
            {HOLIDAY_LIST_PATH: _resource(7, _values())},
            7,
            replace(_values(), fix_temperature=20.0),
            fallback_timezone="UTC",
        )
    client.update_holiday_period.assert_not_awaited()


async def test_merging_independent_date_edits_cannot_reverse_the_timespan() -> None:
    client = AsyncMock()
    client.get_resource.return_value = _resource(
        7, replace(_values(), end_date="2030-08-03T08:00:00")
    )
    with pytest.raises(WriteValidationError, match="timespan"):
        await HolidayWriteService(client, sleep=AsyncMock()).async_update(
            "gateway-one",
            {HOLIDAY_LIST_PATH: _resource(7, _values())},
            7,
            replace(_values(), start_date="2030-08-04T08:00:00"),
            fallback_timezone="UTC",
        )
    client.update_holiday_period.assert_not_awaited()


async def test_update_and_delete_reject_an_id_missing_from_current_list() -> None:
    client = AsyncMock()
    service = HolidayWriteService(client, sleep=AsyncMock(), read_back_delay=0)
    empty = Resource(path=HOLIDAY_LIST_PATH, value=[], has_value=True)

    with pytest.raises(WriteValidationError, match="not present"):
        await service.async_update(
            "gateway-one",
            {HOLIDAY_LIST_PATH: empty},
            7,
            _values(),
            fallback_timezone="UTC",
        )
    with pytest.raises(WriteValidationError, match="not present"):
        await service.async_delete(
            "gateway-one",
            {HOLIDAY_LIST_PATH: empty},
            7,
            fallback_timezone="UTC",
        )
    client.update_holiday_period.assert_not_awaited()
    client.delete_holiday_period.assert_not_awaited()


def test_create_uses_app_defaults_base64_and_pointt_midnight_end() -> None:
    timezone = ZoneInfo("Europe/Berlin")

    values = create_holiday_values(
        datetime(2030, 8, 1, 8, 15, tzinfo=timezone),
        datetime(2030, 8, 9, 0, 0, tzinfo=timezone),
        "Summer holiday",
        _configuration(),
        timezone,
    )

    assert values.start_date == "2030-08-01T08:15:00"
    assert values.end_date == "2030-08-08T24:00:00"
    assert values.name == "U3VtbWVyIGhvbGlkYXk="
    assert values.assigned_to == ("hc1", "dhw1")
    assert values.heating_mode == "FIX_TEMPERATURE"
    assert values.dhw_mode == "OFF"


def test_all_day_calendar_end_is_converted_to_inclusive_pointt_date() -> None:
    values = create_holiday_values(
        date(2030, 8, 1),
        date(2030, 8, 9),
        "Holiday",
        _configuration(date_time_mode="date"),
        UTC,
    )

    assert values.start_date == "2030-08-01"
    assert values.end_date == "2030-08-08"


def test_all_day_only_gateway_rejects_non_midnight_datetime() -> None:
    with pytest.raises(WriteValidationError, match="all-day"):
        create_holiday_values(
            datetime(2030, 8, 1, 8, tzinfo=UTC),
            datetime(2030, 8, 2, 8, tzinfo=UTC),
            "Holiday",
            _configuration(date_time_mode="date"),
            UTC,
        )


@pytest.mark.parametrize(
    ("start", "end", "start_offset", "end_offset"),
    [
        (
            datetime(2027, 3, 27, 23, tzinfo=ZoneInfo("Europe/Berlin")),
            datetime(2027, 3, 28, 4, tzinfo=ZoneInfo("Europe/Berlin")),
            3600,
            7200,
        ),
        (
            datetime(2027, 10, 30, 23, tzinfo=ZoneInfo("Europe/Berlin")),
            datetime(2027, 10, 31, 4, tzinfo=ZoneInfo("Europe/Berlin")),
            7200,
            3600,
        ),
    ],
)
def test_holiday_round_trip_preserves_wall_time_across_dst_changes(
    start: datetime,
    end: datetime,
    start_offset: int,
    end_offset: int,
) -> None:
    timezone = ZoneInfo("Europe/Berlin")
    values = create_holiday_values(start, end, "DST test", _configuration(), timezone)
    resources = {
        HOLIDAY_LIST_PATH: _resource(7, values),
        HOLIDAY_TIMEZONE_PATH: Resource(
            path=HOLIDAY_TIMEZONE_PATH,
            value="Europe/Berlin",
            has_value=True,
        ),
    }

    period = parse_holiday_state(resources).periods[0]

    assert period.start.hour == start.hour
    assert period.end.hour == end.hour
    assert period.start.utcoffset() is not None
    assert period.end.utcoffset() is not None
    assert period.start.utcoffset().total_seconds() == start_offset
    assert period.end.utcoffset().total_seconds() == end_offset


def test_unknown_date_mode_is_rejected() -> None:
    with pytest.raises(WriteValidationError, match="Unsupported"):
        create_holiday_values(
            date(2030, 8, 1),
            date(2030, 8, 2),
            "Holiday",
            _configuration(date_time_mode="vendor-mode"),
            UTC,
        )


def test_update_preserves_all_non_calendar_fields() -> None:
    current = _values()
    period = HolidayPeriod(
        start=datetime(2030, 8, 1, 8, tzinfo=UTC),
        end=datetime(2030, 8, 8, 18, tzinfo=UTC),
        identifier="7",
        name="Test",
        write_values=current,
    )

    updated = update_holiday_values(
        period,
        datetime(2030, 8, 2, 9, tzinfo=UTC),
        datetime(2030, 8, 9, 19, tzinfo=UTC),
        "Test",
        _configuration(),
        UTC,
    )

    assert updated == HolidayWriteValues(
        start_date="2030-08-02T09:00:00",
        end_date="2030-08-09T19:00:00",
        heating_mode=current.heating_mode,
        dhw_mode=current.dhw_mode,
        ventilation_mode=current.ventilation_mode,
        assigned_to=current.assigned_to,
        name=current.name,
        thermal_disinfection=current.thermal_disinfection,
        fix_temperature=current.fix_temperature,
    )


def test_detail_configuration_changes_only_selected_supported_fields() -> None:
    current = replace(
        _values(), ventilation_mode="NOM", assigned_to=("hc1", "dhw1", "vent1")
    )
    period = HolidayPeriod(
        start=datetime(2030, 8, 1, 8, tzinfo=UTC),
        end=datetime(2030, 8, 8, 18, tzinfo=UTC),
        identifier="7",
        write_values=current,
    )

    configured = configure_holiday_values(
        period,
        replace(_configuration(), assigned_to=("hc1", "dhw1", "vent1")),
        assigned_to=["hc1", "vent1"],
        heating_mode="ECO",
        dhw_mode="LOW",
        ventilation_mode="DEM",
        thermal_disinfection="OFF",
        fix_temperature=18.5,
    )

    assert configured == replace(
        current,
        assigned_to=("hc1", "vent1"),
        heating_mode="ECO",
        dhw_mode="LOW",
        ventilation_mode="DEM",
        thermal_disinfection="OFF",
        fix_temperature=18.5,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"assigned_to": []},
        {"assigned_to": ["hc2"]},
        {"assigned_to": ["hc1", "hc1"]},
        {"heating_mode": "VENDOR"},
        {"dhw_mode": "VENDOR"},
        {"ventilation_mode": "VENDOR"},
        {"thermal_disinfection": "VENDOR"},
        {"fix_temperature": 30.0},
    ],
)
def test_detail_configuration_rejects_unadvertised_values(
    changes: dict[str, Any],
) -> None:
    current = replace(_values(), ventilation_mode="NOM")
    period = HolidayPeriod(
        start=datetime(2030, 8, 1, 8, tzinfo=UTC),
        end=datetime(2030, 8, 8, 18, tzinfo=UTC),
        identifier="7",
        write_values=current,
    )
    values = {
        "assigned_to": ["hc1", "dhw1"],
        "heating_mode": "FIX_TEMPERATURE",
        "dhw_mode": "OFF",
        "ventilation_mode": "NOM",
        "thermal_disinfection": "ON",
        "fix_temperature": 17.0,
    }
    values.update(changes)

    with pytest.raises(WriteValidationError):
        configure_holiday_values(period, _configuration(), **values)


def test_detail_configuration_requires_complete_period_and_safe_temperature() -> None:
    incomplete = HolidayPeriod(
        start=datetime(2030, 8, 1, 8, tzinfo=UTC),
        end=datetime(2030, 8, 8, 18, tzinfo=UTC),
        identifier="7",
    )
    arguments = {
        "assigned_to": ["hc1"],
        "heating_mode": "FIX_TEMPERATURE",
        "dhw_mode": "OFF",
        "ventilation_mode": "OFF",
        "thermal_disinfection": "ON",
        "fix_temperature": 17.0,
    }
    with pytest.raises(WriteValidationError, match="complete writable"):
        configure_holiday_values(incomplete, _configuration(), **arguments)

    period = replace(incomplete, write_values=_values())
    with pytest.raises(WriteValidationError, match="must be finite"):
        configure_holiday_values(
            period, _configuration(), **{**arguments, "fix_temperature": float("nan")}
        )

    no_limits = replace(
        _configuration(), fix_temperature_min=None, fix_temperature_max=None
    )
    with pytest.raises(WriteValidationError, match="safe fixed-temperature limits"):
        configure_holiday_values(
            period, no_limits, **{**arguments, "fix_temperature": 18.0}
        )

    with pytest.raises(WriteValidationError, match="not configurable"):
        configure_holiday_values(
            period,
            replace(_configuration(), ventilation_modes=()),
            **{**arguments, "ventilation_mode": "OFF"},
        )


def test_update_rejects_incomplete_period_and_unsupported_rename() -> None:
    incomplete = HolidayPeriod(
        start=datetime(2030, 8, 1, tzinfo=UTC),
        end=datetime(2030, 8, 2, tzinfo=UTC),
        identifier="not-numeric",
    )
    with pytest.raises(WriteValidationError, match="complete writable"):
        update_holiday_values(
            incomplete,
            incomplete.start,
            incomplete.end,
            "Holiday",
            _configuration(),
            UTC,
        )

    current = _values()
    period = HolidayPeriod(
        start=datetime(2030, 8, 1, tzinfo=UTC),
        end=datetime(2030, 8, 2, tzinfo=UTC),
        identifier="7",
        write_values=current,
    )
    ascii_configuration = replace(
        _configuration(), name_codec=HolidayNameCodec("ASCII", None, 32)
    )
    with pytest.raises(WriteValidationError, match="storing holiday names"):
        update_holiday_values(
            period,
            period.start,
            period.end,
            "Renamed",
            ascii_configuration,
            UTC,
        )


@pytest.mark.parametrize("summary", ["", "x" * 81, "bad\x00name"])
def test_invalid_holiday_names_are_rejected(summary: str) -> None:
    with pytest.raises(WriteValidationError, match="Holiday name"):
        create_holiday_values(
            date(2030, 8, 1),
            date(2030, 8, 2),
            summary,
            _configuration(),
            UTC,
        )


def test_cloud_name_limit_is_enforced() -> None:
    configuration = HolidayWriteConfiguration(
        date_time_mode="dateTime",
        assigned_to=("hc1",),
        heating_mode="FIX_TEMPERATURE",
        dhw_mode="OFF",
        ventilation_mode=None,
        thermal_disinfection=None,
        fix_temperature=17.0,
        name_codec=HolidayNameCodec("BASE64", "UTF8", 4),
        heating_modes=("FIX_TEMPERATURE",),
        dhw_modes=("OFF",),
        ventilation_modes=(),
        thermal_disinfection_modes=(),
        fix_temperature_min=10.0,
        fix_temperature_max=25.0,
    )
    with pytest.raises(WriteValidationError, match="must not exceed 4"):
        create_holiday_values(
            date(2030, 8, 1), date(2030, 8, 2), "Long name", configuration, UTC
        )


def test_snapshot_resource_extraction_is_bounded_to_holiday_paths() -> None:
    holiday = Resource(path=HOLIDAY_LIST_PATH)
    resources = holiday_resources_from_snapshots(
        {
            HOLIDAY_LIST_PATH: SimpleNamespace(resource=holiday, available=True),
            "/private": SimpleNamespace(
                resource=Resource(path="/private"), available=True
            ),
            "/missing": SimpleNamespace(resource=None, available=True),
        }
    )

    assert resources == {HOLIDAY_LIST_PATH: holiday}


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2030, 1, 1, 8, 1), datetime(2030, 1, 2, 8, 0)),
        (datetime(2030, 1, 2, 8, 0), datetime(2030, 1, 1, 8, 0)),
        (date(2030, 1, 1), datetime(2030, 1, 2, 8, 0)),
    ],
)
def test_invalid_calendar_times_are_rejected(
    start: date | datetime, end: date | datetime
) -> None:
    with pytest.raises(WriteValidationError):
        create_holiday_values(start, end, "Holiday", _configuration(), UTC)
