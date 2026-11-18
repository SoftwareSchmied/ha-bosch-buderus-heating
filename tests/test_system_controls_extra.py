"""Silent mode window, low-noise operation, pool and gateway readings (0.9.2).

Every function has its own gate: without the path, without a writable flag,
without the advertised list or the limits it needs, nothing is created and
nothing is written. Every shape is constructed (nothing here confirms
behaviour on a real installation).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import gateway_clock
from custom_components.bosch_buderus_heating import system_controls_extra as extra
from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.discovery import (
    OPAQUE_CONTAINER_CHILDREN,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    entity_enabled_by_default,
    poll_group,
    resource_name,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.circuit_control_helpers import (
    Cloud,
    coordinator_for,
    number_entity,
    select_entity,
)

POWER = "/system/silentMode/powerReduction"
START = "/system/silentMode/startTime"
STOP = "/system/silentMode/stopTime"
NOISE_MODE = "/system/lowNoise/mode"
NOISE_DURATION = "/system/lowNoise/duration"
POOL_SETPOINT = "/heatSources/poolSetpointTemperature"
POOL_MODE = "/pool/additionalHeater/poolMode"
UPDATE_ENABLED = "/gateway/update/enabled"
LAST_CHECK = "/gateway/update/lastCheck"
LAST_UPDATE = "/gateway/update/lastUpdate"
CLOCK = "/gateway/DateTime"
LEDS = "/gateway/leds/inactivityTimeout"


def _number(path: str, value: float, *, kind: str = "integerValue", **fields: Any):
    item = {"id": path, "type": kind, "value": value, "writeable": 1}
    item.update(fields)
    return item


def _power(value: float = 30, **fields: Any) -> dict[str, Any]:
    return _number(
        POWER,
        value,
        **{"unitOfMeasure": "%", "minValue": 0, "maxValue": 100, **fields},
    )


def _time(path: str, value: float = 1320, **fields: Any) -> dict[str, Any]:
    return _number(
        path,
        value,
        **{"unitOfMeasure": "mins", "minValue": 0, "maxValue": 1439, **fields},
    )


def _duration(value: float = 60, unit: str | None = "mins", **fields: Any):
    return _number(
        NOISE_DURATION,
        value,
        **{"unitOfMeasure": unit, "minValue": 15, "maxValue": 720, **fields},
    )


def _pool_setpoint(**fields: Any) -> dict[str, Any]:
    return _number(
        POOL_SETPOINT,
        26.0,
        kind="floatValue",
        **{
            "unitOfMeasure": "C",
            "minValue": 10.0,
            "maxValue": 40.0,
            "stepSize": 1.0,
            **fields,
        },
    )


def _string(path: str, value: str, allowed: list[str] | None, **fields: Any):
    item: dict[str, Any] = {
        "id": path,
        "type": "stringValue",
        "value": value,
        "writeable": 1,
        **fields,
    }
    if allowed is not None:
        item["allowedValues"] = allowed
    return item


def _plant(*items: dict[str, Any]) -> dict[str, Any]:
    return {item["id"]: parse_resource(item) for item in items}


def _numbers(resources: dict[str, Any]) -> dict[str, Any]:
    return {item.resource_path: item for item in build_number_descriptions(resources)}


def _selects(resources: dict[str, Any]) -> dict[str, Any]:
    return {item.resource_path: item for item in build_select_descriptions(resources)}


# L02a silent mode: power reduction and window -------------------------------


def test_power_reduction_number_for_a_whole_percentage():
    description = _numbers(_plant(_power()))[POWER]
    assert description.write_policy is extra.POWER_REDUCTION_POLICY
    assert description.translation_key == "silent_mode_power_reduction"
    assert description.native_min_value == 0
    assert description.native_max_value == 100
    assert description.native_unit_of_measurement == "%"
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    assert description.device_class is None
    assert description.icon is None


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_power(writeable=0), "not_writable"),
        (_power(unitOfMeasure="C"), "unsupported_unit"),
        (_power(unitOfMeasure=None), "unsupported_unit"),
        (_power(maxValue=250), "implausible_bounds"),
        (_power(minValue=-10), "implausible_bounds"),
        (_power(minValue=None), "missing_bounds"),
        (_power(maxValue=None), "missing_bounds"),
        (
            _number(POWER, 3, kind="stringValue", unitOfMeasure="%"),
            "unsupported_resource_type",
        ),
    ],
)
def test_power_reduction_gate(item, reason):
    item = {key: value for key, value in item.items() if value is not None}
    resources = _plant(item)
    assert assess_control(resources[POWER]).rejection_reason == reason
    assert POWER not in _numbers(resources)


def test_power_reduction_also_as_a_float():
    resources = _plant(
        _number(
            POWER, 30.0, kind="floatValue", unitOfMeasure="%", minValue=0, maxValue=100
        )
    )
    assert POWER in _numbers(resources)


def test_window_times_are_numbers_in_the_reported_unit_not_durations():
    resources = _plant(_time(START), _time(STOP, 360))
    for path, key in (
        (START, "silent_mode_start_time"),
        (STOP, "silent_mode_stop_time"),
    ):
        description = _numbers(resources)[path]
        assert description.translation_key == key
        assert description.native_unit_of_measurement == "min"
        assert description.native_max_value == 1439
        # A point of a window, not a duration.
        assert description.device_class is None
        assert description.entity_registry_enabled_default is False
        assert description.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize(
    "item",
    [
        _time(START, unitOfMeasure="h"),
        # Plausible limits, but not minutes: no entity for another unit.
        _time(START, unitOfMeasure="h", minValue=0, maxValue=23),
        _time(START, unitOfMeasure="s", minValue=0, maxValue=86340),
        _time(START, unitOfMeasure="C"),
        _time(START, unitOfMeasure=None),
        _time(START, maxValue=2359),
        _time(START, minValue=-5),
        _time(START, writeable=0),
        _time(START, minValue=None),
        _time(START, maxValue=None),
    ],
)
def test_window_time_gate(item):
    item = {key: value for key, value in item.items() if value is not None}
    assert START not in _numbers(_plant(item))


def test_nothing_without_the_silent_mode_paths():
    resources = _plant(
        _string("/system/silentMode/enabled", "off", ["off", "auto", "on"])
    )
    assert not {POWER, START, STOP} & set(_numbers(resources))


async def test_power_reduction_writes_once_as_a_whole_number(hass):
    coordinator = coordinator_for(hass, _plant(_power(30)))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, POWER)
    await entity.async_set_native_value(40.0)
    # The setting is read live in one request before the one PUT.
    assert cloud.bulk_calls[0] == (POWER,)
    assert cloud.puts == [(POWER, 40)]
    assert isinstance(cloud.puts[0][1], int)
    assert entity.native_value == 40
    for refused in (40.5, 101.0, -1.0):
        with pytest.raises((ServiceValidationError, WriteValidationError)):
            await entity.async_set_native_value(refused)
    assert len(cloud.puts) == 1


async def test_window_time_writes_once(hass):
    coordinator = coordinator_for(hass, _plant(_time(START, 1320)))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, START)
    await entity.async_set_native_value(1380.0)
    assert cloud.puts == [(START, 1380)]
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(1500.0)
    assert len(cloud.puts) == 1


async def test_power_reduction_refuses_limits_that_changed_live(hass):
    coordinator = coordinator_for(hass, _plant(_power(30)))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, POWER)
    metadata = coordinator.data[POWER].resource.metadata
    cloud.change(POWER, metadata=replace(metadata, maximum=250.0))
    with pytest.raises(
        (ServiceValidationError, WriteValidationError, HomeAssistantError)
    ):
        await entity.async_set_native_value(40.0)
    assert cloud.puts == []


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ResourceForbidden(POWER, 403), "write_failed"),
        (ServiceUnavailable(503), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
    ],
)
async def test_power_reduction_failed_write_is_never_repeated(hass, failure, key):
    coordinator = coordinator_for(hass, _plant(_power(30)))
    cloud = Cloud(coordinator)
    cloud.put_error = failure
    entity = number_entity(hass, coordinator, POWER)
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_native_value(40.0)
    assert error.value.translation_key == key
    assert cloud.puts == [(POWER, 40)]


async def test_power_reduction_without_confirmation_stays_one_put(hass):
    coordinator = coordinator_for(hass, _plant(_power(30)))
    cloud = Cloud(coordinator)
    cloud.stuck[POWER] = 30
    entity = number_entity(hass, coordinator, POWER)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(40.0)
    assert cloud.puts == [(POWER, 40)]


def test_power_reduction_follows_the_control_pace_and_has_a_reading():
    """The integer reading used to be refused by the read schema: it stayed
    in the static group and had no entity of its own."""
    resource = _plant(_power())[POWER]
    assert poll_group(resource) is not PollGroup.STATIC
    descriptions = build_sensor_descriptions({POWER: resource})
    assert [item.resource_path for item in descriptions] == [POWER]
    assert descriptions[0].entity_registry_enabled_default is False


# F1 low-noise operation ---------------------------------------------------


def test_low_noise_duration_number_takes_the_unit_of_the_gateway():
    for unit, shown in (("mins", "min"), ("h", "h"), ("s", "s")):
        item = _duration(unit=unit)
        if unit == "h":
            item.update(minValue=1, maxValue=12)
        if unit == "s":
            item.update(minValue=900, maxValue=43200)
        description = _numbers(_plant(item))[NOISE_DURATION]
        assert description.write_policy is extra.LOW_NOISE_DURATION_POLICY
        assert description.native_unit_of_measurement == shown
        assert description.device_class is NumberDeviceClass.DURATION
        assert description.entity_registry_enabled_default is False


@pytest.mark.parametrize(
    "item",
    [
        _duration(unit="d"),
        _duration(unit="C"),
        _duration(unit=None),
        _duration(maxValue=100000),
        _duration(writeable=0),
        _duration(minValue=None),
    ],
)
def test_low_noise_duration_gate(item):
    item = {key: value for key, value in item.items() if value is not None}
    assert NOISE_DURATION not in _numbers(_plant(item))


def _mode(path: str, allowed, value="off", **fields):
    return _string(path, value, allowed, **fields)


def test_low_noise_mode_select_over_the_advertised_values():
    resources = _plant(_mode(NOISE_MODE, ["off", "levelOne", "levelTwo"]))
    description = _selects(resources)[NOISE_MODE]
    assert description.write_policy is extra.LOW_NOISE_MODE_POLICY
    assert description.options == ["off", "levelOne", "levelTwo"]
    assert description.translation_key == "low_noise_mode"
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    assert description.icon is None


@pytest.mark.parametrize("path", [NOISE_MODE, POOL_MODE])
@pytest.mark.parametrize(
    "item",
    [
        {"allowed": None},
        {"allowed": []},
        {"allowed": ["off"]},
        {"allowed": ["off", "on"], "writeable": 0},
        {"allowed": ["on", "auto"], "value": "off"},
        {"allowed": ["off", ""], "value": "off"},
    ],
)
def test_selection_gate(path, item):
    fields = {k: v for k, v in item.items() if k not in ("allowed", "value")}
    resources = _plant(_mode(path, item["allowed"], item.get("value", "off"), **fields))
    assert path not in _selects(resources)


def test_selections_need_the_path():
    resources = _plant(_mode("/system/seasonOptimizer/mode", ["off", "automatic"]))
    assert not {NOISE_MODE, POOL_MODE} & set(_selects(resources))
    assert not {NOISE_DURATION, POOL_SETPOINT} & set(_numbers(resources))


async def test_low_noise_mode_writes_once(hass):
    coordinator = coordinator_for(
        hass, _plant(_mode(NOISE_MODE, ["off", "levelOne", "levelTwo"]))
    )
    cloud = Cloud(coordinator)
    entity = select_entity(hass, coordinator, NOISE_MODE)
    assert entity.current_option == "off"
    await entity.async_select_option("levelOne")
    assert cloud.bulk_calls[0] == (NOISE_MODE,)
    assert cloud.puts == [(NOISE_MODE, "levelOne")]
    assert entity.current_option == "levelOne"
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("levelNine")
    assert len(cloud.puts) == 1


async def test_low_noise_mode_refuses_a_list_that_changed_live(hass):
    coordinator = coordinator_for(
        hass, _plant(_mode(NOISE_MODE, ["off", "levelOne", "levelTwo"]))
    )
    cloud = Cloud(coordinator)
    entity = select_entity(hass, coordinator, NOISE_MODE)
    metadata = coordinator.data[NOISE_MODE].resource.metadata
    cloud.change(NOISE_MODE, metadata=replace(metadata, allowed_values=("off",)))
    with pytest.raises((ServiceValidationError, HomeAssistantError)):
        await entity.async_select_option("levelOne")
    assert cloud.puts == []


async def test_low_noise_duration_failed_write_is_never_repeated(hass):
    coordinator = coordinator_for(hass, _plant(_duration(60)))
    cloud = Cloud(coordinator)
    cloud.put_error = ServiceUnavailable(503)
    entity = number_entity(hass, coordinator, NOISE_DURATION)
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_native_value(120.0)
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(NOISE_DURATION, 120)]


async def test_low_noise_mode_failed_write_is_never_repeated(hass):
    coordinator = coordinator_for(hass, _plant(_mode(NOISE_MODE, ["off", "levelOne"])))
    cloud = Cloud(coordinator)
    cloud.put_error = ServiceUnavailable(503)
    entity = select_entity(hass, coordinator, NOISE_MODE)
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("levelOne")
    assert cloud.puts == [(NOISE_MODE, "levelOne")]


# F10 pool ---------------------------------------------------------------------


def test_pool_target_of_the_heat_source_only_with_limits():
    description = _numbers(_plant(_pool_setpoint()))[POOL_SETPOINT]
    assert description.write_policy is extra.POOL_SETPOINT_POLICY
    assert description.translation_key == "pool_heat_source_setpoint"
    assert description.device_class is NumberDeviceClass.TEMPERATURE
    assert description.entity_registry_enabled_default is False
    for broken in (
        _pool_setpoint(writeable=0),
        {**_pool_setpoint(), "minValue": None},
        {**_pool_setpoint(), "unitOfMeasure": "mins"},
    ):
        broken = {k: v for k, v in broken.items() if v is not None}
        assert POOL_SETPOINT not in _numbers(_plant(broken))


def test_pool_mode_select_only_with_the_advertised_values():
    resources = _plant(_mode(POOL_MODE, ["off", "auto", "on"], value="auto"))
    description = _selects(resources)[POOL_MODE]
    assert description.write_policy is extra.POOL_MODE_POLICY
    assert description.translation_key == "pool_additional_heater_mode"
    assert description.entity_registry_enabled_default is False


def test_the_existing_pool_controls_stay():
    resources = _plant(
        {
            "id": "/pool/setpointTemp",
            "type": "floatValue",
            "value": 26.0,
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 10.0,
            "maxValue": 40.0,
            "stepSize": 1.0,
        }
    )
    assert "/pool/setpointTemp" in _numbers(resources)
    assert POOL_SETPOINT not in _numbers(resources)


async def test_pool_target_writes_once(hass):
    coordinator = coordinator_for(hass, _plant(_pool_setpoint()))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, POOL_SETPOINT)
    await entity.async_set_native_value(28.0)
    assert cloud.puts == [(POOL_SETPOINT, 28.0)]
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(45.0)
    assert len(cloud.puts) == 1


# Names ------------------------------------------------------------------------

LANGUAGES = [
    "bg",
    "cs",
    "da",
    "el",
    "es",
    "fi",
    "fr",
    "ga",
    "hr",
    "it",
    "nb",
    "nl",
    "pl",
    "pt",
    "sk",
    "sl",
    "sv",
]


@pytest.mark.parametrize(
    ("path", "english", "german"),
    [
        (POWER, "Silent mode: power reduction", "Flüstermodus: Leistungsreduzierung"),
        (START, "Silent mode: start time", "Flüstermodus: Startzeit"),
        (STOP, "Silent mode: stop time", "Flüstermodus: Endzeit"),
        (NOISE_DURATION, "Low-noise operation: duration", "Leiser Betrieb: Dauer"),
        (LEDS, "LED switch-off time", "LED-Abschaltzeit"),
        (UPDATE_ENABLED, "Automatic updates", "Automatische Updates"),
        (LAST_CHECK, "Last update check", "Letzte Update-Prüfung"),
        (LAST_UPDATE, "Last update", "Letztes Update"),
    ],
)
def test_names_in_every_language(path, english, german):
    assert resource_name(path, language="en") == english
    assert resource_name(path, language="de") == german
    for language in LANGUAGES:
        text = resource_name(path, language=language)
        assert text and text != english, language


def test_entity_names_in_the_translation_files():
    import json
    from pathlib import Path

    directory = Path(extra.__file__).parent / "translations"
    keys = {
        "number": [
            "silent_mode_power_reduction",
            "silent_mode_start_time",
            "silent_mode_stop_time",
            "low_noise_duration",
            "pool_heat_source_setpoint",
        ],
        "select": ["low_noise_mode", "pool_additional_heater_mode"],
        "sensor": ["gateway_clock_deviation"],
    }
    english = json.loads((directory / "en.json").read_text(encoding="utf-8"))
    for language in ["de", "en", *LANGUAGES]:
        data = json.loads((directory / f"{language}.json").read_text(encoding="utf-8"))
        for domain, names in keys.items():
            for key in names:
                name = data["entity"][domain][key]["name"]
                assert name, (language, key)
                if language not in ("en", "de"):
                    assert name != english["entity"][domain][key]["name"] or key in (
                        "pool_heat_source_setpoint",
                    ), (language, key)
    strings = json.loads(
        (directory.parent / "strings.json").read_text(encoding="utf-8")
    )
    assert (
        strings["entity"]["sensor"]["gateway_clock_deviation"]["name"]
        == (english["entity"]["sensor"]["gateway_clock_deviation"]["name"])
    )


# Diagnostics ------------------------------------------------------------------


def test_diagnostics_count_only():
    resources = _plant(
        _power(),
        _time(START),
        _mode(NOISE_MODE, ["off", "on"]),
        _mode(POOL_MODE, ["off", "on"], writeable=0),
    )
    result = extra.diagnostics(resources)
    assert result["silent_power_reduction"] == {
        "present": 1,
        "writable": 1,
        "with_options": 0,
        "with_limits": 1,
    }
    assert result["silent_start_time"]["present"] == 1
    assert result["silent_stop_time"]["present"] == 0
    assert result["low_noise_mode"] == {
        "present": 1,
        "writable": 1,
        "with_options": 1,
        "with_limits": 0,
    }
    assert result["pool_mode"]["writable"] == 0
    assert result["pool_setpoint"]["present"] == 0
    assert "path" not in repr(result).lower()


# L15 gateway readings -----------------------------------------------------------


def _text(path: str, value: str, **fields: Any) -> dict[str, Any]:
    return {"id": path, "type": "stringValue", "value": value, **fields}


def test_update_readings_are_diagnostic_sensors_disabled_by_default():
    resources = _plant(
        _text(UPDATE_ENABLED, "on", writeable=1),
        _text(LAST_CHECK, "2030-01-01T10:00:00"),
        _text(LAST_UPDATE, "2029-12-24T04:30:00"),
    )
    descriptions = {
        item.resource_path: item for item in build_sensor_descriptions(resources)
    }
    assert set(descriptions) == {UPDATE_ENABLED, LAST_CHECK, LAST_UPDATE}
    for description in descriptions.values():
        assert description.entity_registry_enabled_default is False
        assert description.entity_category is EntityCategory.DIAGNOSTIC
        assert not entity_enabled_by_default(description.resource_path)
    # They are readings: nothing writes them.
    for path in (UPDATE_ENABLED, LAST_CHECK, LAST_UPDATE):
        assert assess_control(resources[path]).policy is None
        assert path not in _numbers(resources) and path not in _selects(resources)


def test_update_readings_only_as_text():
    resources = _plant(
        {"id": LAST_CHECK, "type": "floatValue", "value": 3.0},
    )
    assert not build_sensor_descriptions(resources)


def test_update_readings_do_not_use_the_fast_groups():
    for path in (UPDATE_ENABLED, LAST_CHECK, LAST_UPDATE, CLOCK):
        resource = _plant(_text(path, "x", writeable=1))[path]
        assert poll_group(resource) is PollGroup.SLOW


def test_the_led_switch_off_time_is_probed_and_read_only():
    assert LEDS in OPAQUE_CONTAINER_CHILDREN["/gateway"]
    resources = _plant(
        _number(LEDS, 5, kind="integerValue", unitOfMeasure="mins", writeable=0)
    )
    descriptions = build_sensor_descriptions(resources)
    assert [item.resource_path for item in descriptions] == [LEDS]
    assert descriptions[0].entity_registry_enabled_default is False
    assert descriptions[0].entity_category is EntityCategory.DIAGNOSTIC
    # Never a control, even where the gateway lets it be written.
    writable = _plant(
        _number(
            LEDS, 5, kind="integerValue", unitOfMeasure="mins", minValue=1, maxValue=60
        )
    )
    assert LEDS not in _numbers(writable)
    assert assess_control(writable[LEDS]).policy is None


# L15 clock deviation ------------------------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")
RECEIVED = datetime(2030, 1, 1, 11, 0, 0, tzinfo=UTC)  # 12:00 in Berlin


def test_a_reading_without_a_zone_is_local_time():
    assert gateway_clock.deviation_seconds("2030-01-01T12:00:00", RECEIVED, BERLIN) == 0
    assert (
        gateway_clock.deviation_seconds("2030-01-01T12:03:20", RECEIVED, BERLIN) == 200
    )
    assert (
        gateway_clock.deviation_seconds("2030-01-01T11:59:00", RECEIVED, BERLIN) == -60
    )
    # Another convention shows as whole hours.
    assert gateway_clock.deviation_seconds("2030-01-01T12:00:00", RECEIVED, UTC) == 3600


def test_a_reading_with_a_zone_keeps_it():
    assert (
        gateway_clock.deviation_seconds("2030-01-01T11:00:30Z", RECEIVED, BERLIN) == 30
    )
    assert (
        gateway_clock.deviation_seconds(
            "2030-01-01T12:00:00+01:00", RECEIVED, timezone(timedelta(hours=-5))
        )
        == 0
    )


@pytest.mark.parametrize(
    "value",
    [None, 12, "", "tomorrow", "2030-13-45T99:00:00", "x" * 100, ["2030-01-01"]],
)
def test_no_deviation_for_anything_but_a_time(value):
    assert gateway_clock.deviation_seconds(value, RECEIVED, BERLIN) is None


def test_no_deviation_beyond_a_year_or_without_a_reference():
    assert (
        gateway_clock.deviation_seconds("2032-01-01T12:00:00", RECEIVED, BERLIN) is None
    )
    assert gateway_clock.deviation_seconds("2030-01-01T12:00:00", None, BERLIN) is None
    assert (
        gateway_clock.deviation_seconds(
            "2030-01-01T12:00:00", datetime(2030, 1, 1, 11), BERLIN
        )
        is None
    )
    assert (
        gateway_clock.deviation_seconds("2030-01-01T12:00:00", RECEIVED, None) is None
    )
    edge = RECEIVED + timedelta(days=366)
    assert (
        gateway_clock.deviation_seconds(
            (RECEIVED + timedelta(days=367)).astimezone(BERLIN).isoformat(),
            RECEIVED,
            BERLIN,
        )
        is None
    )
    assert (
        gateway_clock.deviation_seconds(edge.isoformat(), RECEIVED, BERLIN)
        == 366 * 86400
    )


def _clock_sensor(hass, value, *, received=RECEIVED, fresh=True, available=True):
    coordinator = coordinator_for(hass, _plant(_text(CLOCK, value, writeable=1)))
    coordinator.data[CLOCK] = ResourceSnapshot(
        coordinator.data[CLOCK].resource,
        available,
        received,
        freshness=Freshness.FRESH if fresh else Freshness.STALE,
    )
    sensor = gateway_clock.GatewayClockDeviationSensor(coordinator)
    sensor.hass = hass
    return sensor


async def test_the_clock_sensor_is_calculated_diagnostic_and_disabled(hass):
    await hass.config.async_set_time_zone("Europe/Berlin")
    sensor = _clock_sensor(hass, "2030-01-01T12:00:45")
    assert sensor.native_value == 45
    assert sensor.available is True
    assert sensor.device_class is SensorDeviceClass.DURATION
    assert sensor.native_unit_of_measurement == "s"
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is False
    assert sensor.extra_state_attributes == {"derived_from": {CLOCK: "cloud"}}
    assert sensor.unique_id == "gateway:gateway:DateTime:clock_deviation"
    assert sensor.translation_key == "gateway_clock_deviation"


async def test_the_clock_sensor_uses_the_time_of_the_reading_not_now(hass):
    """The reading is not newer than its poll: the deviation does not grow
    between two polls."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    sensor = _clock_sensor(hass, "2030-01-01T12:00:45")
    assert sensor.native_value == 45
    assert sensor.native_value == 45  # no clock of our own


async def test_the_clock_sensor_needs_a_current_reading(hass):
    await hass.config.async_set_time_zone("Europe/Berlin")
    assert _clock_sensor(hass, "2030-01-01T12:00:45", fresh=False).available is False
    assert (
        _clock_sensor(hass, "2030-01-01T12:00:45", available=False).available is False
    )
    unreadable = _clock_sensor(hass, "not a time")
    assert unreadable.available is False
    assert unreadable.native_value is None


def _tracking(coordinator):
    added: list[Any] = []
    entry = SimpleNamespace(async_on_unload=lambda remove: None)
    gateway_clock.track_clock_deviation_sensor(
        entry, coordinator, lambda entities: added.extend(entities)
    )
    return added


async def test_no_clock_sensor_without_the_path(hass):
    coordinator = coordinator_for(
        hass, _plant(_text(LAST_CHECK, "2030-01-01T10:00:00"))
    )
    assert _tracking(coordinator) == []
    with_clock = coordinator_for(hass, _plant(_text(CLOCK, "2030-01-01T12:00:00")))
    created = _tracking(with_clock)
    assert len(created) == 1
    assert isinstance(created[0], gateway_clock.GatewayClockDeviationSensor)


async def test_the_clock_sensor_finds_the_path_in_either_spelling(hass):
    await hass.config.async_set_time_zone("Europe/Berlin")
    assert gateway_clock.clock_path([CLOCK]) == CLOCK
    assert gateway_clock.clock_path(["/gateway/dateTime"]) == "/gateway/dateTime"
    assert gateway_clock.clock_path([LAST_CHECK]) is None
    lower = "/gateway/dateTime"
    coordinator = coordinator_for(hass, _plant(_text(lower, "2030-01-01T12:00:10")))
    coordinator.data[lower] = ResourceSnapshot(
        coordinator.data[lower].resource, True, RECEIVED
    )
    sensor = gateway_clock.GatewayClockDeviationSensor(coordinator)
    sensor.hass = hass
    assert sensor.native_value == 10
    assert sensor.extra_state_attributes == {"derived_from": {lower: "cloud"}}
    assert len(_tracking(coordinator)) == 1
