"""Tests for tolerant PointT holiday resource handling."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_ACTIVE_MODES_PATH,
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HolidayNameCodec,
    encode_holiday_name,
    holiday_period_id,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
)


def test_multiple_periods_and_gateway_timezone_are_normalized() -> None:
    resources = {
        "/gateway/tzInfo/timeZone": Resource(
            path="/gateway/tzInfo/timeZone",
            value="Europe/Berlin",
            has_value=True,
        ),
        HOLIDAY_LIST_PATH: Resource(
            path=HOLIDAY_LIST_PATH,
            value={
                "holidays": [
                    {
                        "id": "one",
                        "name": "Summer trip",
                        "startDate": "2026-07-01",
                        "endDate": "2026-07-12",
                    },
                    {
                        "holidayId": "two",
                        "startDateTime": "2026-12-20T08:00:00+01:00",
                        "endDateTime": "2027-01-03T18:00:00+01:00",
                    },
                ]
            },
            has_value=True,
        ),
        HOLIDAY_ACTIVE_MODES_PATH: Resource(path=HOLIDAY_ACTIVE_MODES_PATH),
    }

    state = parse_holiday_state(resources, fallback_timezone="UTC")

    assert state.timezone_source == "gateway"
    assert len(state.periods) == 2
    assert state.periods[0].start.isoformat() == "2026-07-01T00:00:00+02:00"
    assert state.periods[0].end.isoformat() == "2026-07-13T00:00:00+02:00"
    assert state.periods[0].name == "Summer trip"
    assert state.periods[0].all_day
    assert state.active is False
    assert state.invalid_period_count == 0


def test_incomplete_periods_are_ignored_without_losing_valid_entries() -> None:
    resource = Resource(
        path=HOLIDAY_CONFIGURATION_PATH,
        values=(
            {"period": {"from": "2026-08-01", "until": "2026-08-03"}},
            {"start": "2026-08-04"},
            {"start": "not-a-date", "end": "2026-08-06"},
            {"start": "2026-08-08", "end": "2026-08-07"},
        ),
    )

    state = parse_holiday_state(
        {resource.path: resource},
        fallback_timezone="invalid/timezone",
    )

    assert len(state.periods) == 1
    assert state.invalid_period_count == 3
    assert state.timezone_source == "utc_fallback"
    assert state.active is False


def test_active_modes_is_authoritative_and_schedule_is_fallback() -> None:
    period = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "start": "2026-08-20T00:00:00Z",
            "end": "2026-08-25T00:00:00Z",
        },
        has_value=True,
    )
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)

    fallback = parse_holiday_state(
        {period.path: period}, fallback_timezone="UTC", now=now
    )
    inactive = parse_holiday_state(
        {
            period.path: period,
            HOLIDAY_ACTIVE_MODES_PATH: Resource(
                path=HOLIDAY_ACTIVE_MODES_PATH,
                value={"activeModes": []},
                has_value=True,
            ),
        },
        fallback_timezone="UTC",
        now=now,
    )
    active = parse_holiday_state(
        {
            HOLIDAY_ACTIVE_MODES_PATH: Resource(
                path=HOLIDAY_ACTIVE_MODES_PATH,
                values=({"mode": "eco"},),
            )
        },
        fallback_timezone="UTC",
        now=now,
    )

    assert fallback.active is True
    assert inactive.active is False
    assert active.active is True


def test_structured_dates_and_duplicates_are_supported() -> None:
    duplicate = {
        "identifier": "period-1",
        "start": {"year": 2026, "month": 9, "day": 1, "hour": 0},
        "end": {"year": 2026, "month": 9, "day": 4, "hour": 0},
    }
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        values=(duplicate, {"items": [duplicate]}),
    )

    state = parse_holiday_state({resource.path: resource}, fallback_timezone="UTC")

    assert len(state.periods) == 1
    assert state.periods[0].end.day == 4


def test_empty_supported_list_means_no_active_holiday() -> None:
    resource = Resource(path=HOLIDAY_LIST_PATH, value=[], has_value=True)

    state = parse_holiday_state({resource.path: resource})

    assert state.active is False


def test_epoch_values_period_timezone_and_safe_names_are_supported() -> None:
    start = datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp()
    end = datetime(2026, 10, 2, 12, tzinfo=UTC).timestamp() * 1000
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        values=(
            {
                "startTime": start,
                "endTime": end,
                "timeZone": "Europe/Berlin",
                "title": "  Autumn   break  ",
            },
        ),
    )

    state = parse_holiday_state({resource.path: resource}, fallback_timezone="UTC")

    assert state.has_supported_source
    assert state.periods[0].start.isoformat() == "2026-10-01T14:00:00+02:00"
    assert state.periods[0].end.isoformat() == "2026-10-02T14:00:00+02:00"
    assert state.periods[0].name == "Autumn break"
    assert not state.periods[0].all_day


def test_explicit_utc_offset_is_used_for_naive_period_values() -> None:
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "startDateTime": "2026-10-01T12:00:00",
            "endDateTime": "2026-10-02T12:00:00",
            "timeZoneOffset": "+0230",
        },
        has_value=True,
    )

    state = parse_holiday_state({resource.path: resource})

    assert state.periods[0].start.utcoffset().total_seconds() == 9000


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (True, True),
        (0, False),
        (1.0, True),
        ("inactive", False),
        ("active", True),
        ("eco", True),
        ("not-supported", None),
        ([], False),
        ([False, True], True),
        ({"enabled": False}, False),
        ({"modes": []}, False),
        ({"vendor": "value"}, True),
        (None, None),
    ],
)
def test_active_mode_payload_variants(payload, expected: bool | None) -> None:
    resource = Resource(
        path=HOLIDAY_ACTIVE_MODES_PATH,
        value=payload,
        has_value=True,
    )

    state = parse_holiday_state({resource.path: resource})

    assert state.active is expected


def test_malformed_and_out_of_range_periods_are_bounded() -> None:
    nested: object = "not-a-period"
    for _ in range(8):
        nested = {"items": [nested]}
    candidates = [
        {"start": None, "end": "2026-01-01"},
        {"start": True, "end": "2026-01-01"},
        {"start": "", "end": "2026-01-01"},
        {"start": "not-a-date", "end": "2026-01-01"},
        {"start": "1999-01-01", "end": "1999-01-02"},
        {"start": "2026-01-01", "end": "2101-01-01"},
        {"start": {"year": 2026}, "end": "2026-01-02"},
        {
            "start": {"year": 2026, "month": 13, "day": 1},
            "end": "2026-01-02",
        },
        {"start": 10**30, "end": 10**30 + 1000},
        {"start": [2026, 1, 1], "end": "2026-01-02"},
        nested,
    ]
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value={"periods": candidates},
        has_value=True,
    )

    state = parse_holiday_state({resource.path: resource})

    assert state.periods == ()
    assert state.invalid_period_count == 10


def test_candidate_count_and_user_text_are_bounded() -> None:
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value=[
            {
                "id": str(index),
                "name": "x" * 81,
                "start": f"2026-01-{index % 28 + 1:02d}",
                "end": f"2026-02-{index % 28 + 1:02d}",
            }
            for index in range(70)
        ],
        has_value=True,
    )

    state = parse_holiday_state({resource.path: resource})

    assert len(state.periods) == 64
    assert all(period.name is None for period in state.periods)


def test_writable_app_configuration_and_period_are_strictly_parsed() -> None:
    resources = {
        HOLIDAY_LIST_PATH: Resource(
            path=HOLIDAY_LIST_PATH,
            value=[
                {
                    "id": 7,
                    "startDate": "2030-08-01T08:00:00",
                    "endDate": "2030-08-08T24:00:00",
                    "heatingMode": "FIX_TEMPERATURE",
                    "dhwMode": "OFF",
                    "ventilationMode": None,
                    "assignedTo": ["hc1", "dhw1"],
                    "name": "VXJsYXVi",
                    "thermalDesinfection": "ON",
                    "fixTemperature": 17.0,
                }
            ],
            has_value=True,
        ),
        HOLIDAY_CONFIGURATION_PATH: Resource(
            path=HOLIDAY_CONFIGURATION_PATH,
            value={
                "values": {
                    "date": {"allowedValues": ["dateTime"]},
                    "heatingMode": {"allowedValues": ["OFF", "FIX_TEMPERATURE"]},
                    "dhwMode": {"allowedValues": ["OFF", "ECO"]},
                    "ventilationMode": {"allowedValues": []},
                    "assignedTo": {"allowedValues": ["hc1", "dhw1"]},
                    "thermalDesinfection": {"allowedValues": ["ON", "OFF"]},
                    "fixTemperature": {"minValue": 10.0, "maxValue": 25.0},
                    "name": {
                        "stringConfig": {
                            "codingType": "BASE64",
                            "charset": "UTF8",
                            "maxLength": 32,
                        }
                    },
                }
            },
            has_value=True,
        ),
    }

    configuration = parse_holiday_write_configuration(resources)
    state = parse_holiday_state(resources, fallback_timezone="UTC")

    assert configuration is not None
    assert configuration.assigned_to == ("hc1", "dhw1")
    assert configuration.heating_mode == "FIX_TEMPERATURE"
    assert configuration.thermal_disinfection == "ON"
    assert configuration.heating_modes == ("OFF", "FIX_TEMPERATURE")
    assert configuration.dhw_modes == ("OFF", "ECO")
    assert configuration.fix_temperature_min == 10.0
    assert configuration.fix_temperature_max == 25.0
    assert state.periods[0].name == "Urlaub"
    assert state.periods[0].end.isoformat() == "2030-08-09T00:00:00+00:00"
    assert not state.periods[0].all_day
    assert holiday_period_id(state.periods[0]) == 7
    assert state.periods[0].write_values is not None


def test_write_configuration_requires_a_list_and_rejects_unknown_contract() -> None:
    configuration = Resource(
        path=HOLIDAY_CONFIGURATION_PATH,
        value={
            "values": {
                "date": {"allowedValues": ["dateTime"]},
                "heatingMode": {"allowedValues": ["VENDOR_NEW_MODE"]},
                "assignedTo": {"allowedValues": ["hc1"]},
            }
        },
        has_value=True,
    )
    holiday_list = Resource(path=HOLIDAY_LIST_PATH)

    assert (
        parse_holiday_write_configuration(
            {HOLIDAY_LIST_PATH: holiday_list, HOLIDAY_CONFIGURATION_PATH: configuration}
        )
        is None
    )
    assert (
        parse_holiday_write_configuration({HOLIDAY_CONFIGURATION_PATH: configuration})
        is None
    )


def test_name_encoding_is_bounded_and_codec_specific() -> None:
    assert (
        encode_holiday_name("Urlaub", HolidayNameCodec("BASE64", "UTF8", 32))
        == "VXJsYXVi"
    )
    assert encode_holiday_name("Holiday", HolidayNameCodec("ASCII", None, 32)) is None
    assert encode_holiday_name("x" * 33, HolidayNameCodec("BASE64", "UTF8", 32)) is None
    assert encode_holiday_name("Name", None) is None
    assert encode_holiday_name("\ud800", HolidayNameCodec("BASE64", "UTF8", 32)) is None


def test_period_level_name_configuration_decodes_without_global_config() -> None:
    period = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "id": 7,
            "startDate": "2030-08-01T08:00:00",
            "endDate": "2030-08-08T18:00:00",
            "name": "VXJsYXVi",
            "nameConfig": {
                "codingType": "BASE64",
                "charset": "UTF-8",
                "maxLength": 32,
            },
        },
        has_value=True,
    )

    state = parse_holiday_state({HOLIDAY_LIST_PATH: period})

    assert state.periods[0].name == "Urlaub"


def test_datetime_midnight_to_end_of_day_is_exposed_as_all_day() -> None:
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "id": 7,
            "startDate": "2030-08-01T00:00:00",
            "endDate": "2030-08-08T24:00:00",
        },
        has_value=True,
    )

    period = parse_holiday_state({HOLIDAY_LIST_PATH: resource}).periods[0]

    assert period.all_day
    assert period.start.date() == date(2030, 8, 1)
    assert period.end.date() == date(2030, 8, 9)


@pytest.mark.parametrize(
    "changed_values",
    [
        {"date": {"allowedValues": []}},
        {"assignedTo": {"allowedValues": []}},
        {"heatingMode": {"allowedValues": ["VENDOR"]}},
        {"dhwMode": {"allowedValues": ["VENDOR"]}},
        {"ventilationMode": {"allowedValues": ["VENDOR"]}},
        {"thermalDesinfection": {"allowedValues": ["VENDOR"]}},
        {"fixTemperature": {"minValue": 18.0}},
        {"fixTemperature": {"maxValue": 16.0}},
    ],
)
def test_write_configuration_rejects_unsafe_variants(changed_values: dict) -> None:
    values = {
        "date": {"allowedValues": ["dateTime"]},
        "heatingMode": {"allowedValues": ["FIX_TEMPERATURE", "OFF"]},
        "dhwMode": {"allowedValues": ["OFF"]},
        "ventilationMode": {"allowedValues": []},
        "assignedTo": {"allowedValues": ["hc1"]},
        "thermalDesinfection": {"allowedValues": ["ON", "OFF"]},
        "fixTemperature": {"minValue": 10.0, "maxValue": 25.0},
    }
    values.update(changed_values)
    resources = {
        HOLIDAY_LIST_PATH: Resource(
            path=HOLIDAY_LIST_PATH,
            metadata=ResourceMetadata(writable=True),
        ),
        HOLIDAY_CONFIGURATION_PATH: Resource(
            path=HOLIDAY_CONFIGURATION_PATH,
            value={"values": values},
            has_value=True,
        ),
    }

    assert parse_holiday_write_configuration(resources) is None


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("heatingMode", "VENDOR"),
        ("dhwMode", 1),
        ("ventilationMode", "VENDOR"),
        ("thermalDesinfection", "VENDOR"),
        ("assignedTo", []),
        ("assignedTo", ["unknown1"]),
        ("fixTemperature", "17"),
    ],
)
def test_incomplete_period_is_visible_but_never_writable(
    changed_field: str, changed_value: object
) -> None:
    payload = {
        "id": 7,
        "startDate": "2030-08-01T08:00:00",
        "endDate": "2030-08-08T18:00:00",
        "heatingMode": "FIX_TEMPERATURE",
        "dhwMode": "OFF",
        "ventilationMode": None,
        "assignedTo": ["hc1"],
        "name": None,
        "thermalDesinfection": None,
        "fixTemperature": 17.0,
    }
    payload[changed_field] = changed_value
    resource = Resource(path=HOLIDAY_LIST_PATH, value=payload, has_value=True)

    state = parse_holiday_state({HOLIDAY_LIST_PATH: resource})

    assert len(state.periods) == 1
    assert state.periods[0].write_values is None


def test_invalid_encoded_name_is_not_exposed() -> None:
    resource = Resource(
        path=HOLIDAY_LIST_PATH,
        value={
            "id": 7,
            "startDate": "2030-08-01",
            "endDate": "2030-08-02",
            "name": "not-base64!",
            "nameConfig": {
                "codingType": "BASE64",
                "charset": "UTF8",
                "maxLength": 32,
            },
        },
        has_value=True,
    )

    assert parse_holiday_state({HOLIDAY_LIST_PATH: resource}).periods[0].name is None
