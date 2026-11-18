"""Recordings of a water softener and of boilers (series without a local path).

Expected, untested on a real installation: no softener or boiler answer has
been seen. The cloud stand-in answers in the shape of a heat pump's
recordings. What is checked here: a series is asked for only on a plant that
shows what it belongs to, a path that does not answer (or answers in an
unknown unit) yields no entity, and the plants without these series are
asked for nothing new.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.pointt import (
    ResourceForbidden,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.recordings import Period
from custom_components.bosch_buderus_heating.recordings.gating import (
    allowed,
    allowed_keys,
    has_boiler_shape,
    has_softener_circuit,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    entity_name,
    entity_specs,
    measures,
)
from custom_components.bosch_buderus_heating.recordings.parse import (
    parse_cloud,
    rejection,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    ALL_SERIES,
    BY_KEY,
    CLOUD_ONLY,
    LOCAL_PATHS,
    SERIES,
    Quantity,
    unit_factor,
)
from tests.test_recordings_core import local
from tests.test_recordings_manager import GW, Clock, Driver, manager

DASH = chr(0x2013)
WATER = BY_KEY["water"].path
BOILER_PATHS = {
    BY_KEY[key].path
    for key in ("ch_actual_power", "dhw_actual_power", "total_consumed")
}
SOFTENER_RESOURCES = {
    "/swCircuits/sw1/sensor/saltLevel": object(),
    "/swCircuits/sw1/regeneration/enabled": object(),
}
BOILER_RESOURCES = {
    "/heatSources/numberOfStarts": object(),
    "/heatSources/actualSupplyTemperature": object(),
}


class AddCloud:
    """Answers the paths of ``answers`` (unit by path), 404 the others."""

    def __init__(
        self, answers: dict[str, str], *, forbidden: tuple[str, ...] = ()
    ) -> None:
        self.answers, self.forbidden = answers, forbidden
        self.calls: list[tuple[str, str]] = []

    async def get_recording(self, gateway_id: str, path: str, interval: str) -> Any:
        assert gateway_id == GW
        self.calls.append((path, interval))
        if path in self.forbidden:
            raise ResourceForbidden(path, 403)
        if path not in self.answers:
            raise ResourceNotFound(path, 404)
        if len(interval) == 10:
            rows = [{"y": 10.0, "c": 1}] * 24
        elif len(interval) == 7:
            rows = [{"y": 240.0, "c": 24}] * 30
        else:
            rows = [{"y": 7000.0, "c": 1}] * 12
        return {
            "type": "yRecording",
            "unitOfMeasure": self.answers[path],
            "recording": rows,
        }

    def asked(self) -> set[str]:
        return {path for path, _ in self.calls}


def clock() -> Clock:
    return Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))


async def plant(
    hass: HomeAssistant,
    resources: dict[str, object],
    cloud: AddCloud,
    drivers: tuple[Any, ...] = (),
):
    now = clock()
    rec = await manager(hass, now, list(drivers), cloud)
    rec._test_coordinator.resources = dict(resources)
    return rec, now


# ----------------------------------------------------------------- series
def test_the_series_without_a_local_path_stay_out_of_the_local_catalogue() -> None:
    assert {item.key for item in CLOUD_ONLY} == {
        "water",
        "ch_actual_power",
        "dhw_actual_power",
        "total_consumed",
    }
    assert all(item.cloud_only and item.requires for item in CLOUD_ONLY)
    assert not any(item.path in LOCAL_PATHS for item in CLOUD_ONLY)
    assert not any(item.cloud_only for item in SERIES)
    assert len({item.key for item in ALL_SERIES}) == len(ALL_SERIES)
    assert len({item.path for item in ALL_SERIES}) == len(ALL_SERIES)
    # Not part of the energy paths read for another feature (the load).
    assert not any(item.cloud for item in CLOUD_ONLY)


def test_the_water_series_accepts_litres_and_cubic_metres_only() -> None:
    water = BY_KEY["water"]
    assert water.quantity is Quantity.VOLUME and water.unit == "L"
    assert unit_factor(Quantity.VOLUME, "l") == 1.0
    assert unit_factor(Quantity.VOLUME, "m³") == 1000.0
    assert unit_factor(Quantity.VOLUME, "kWh") is None
    assert unit_factor(Quantity.VOLUME, None) is None
    payload = {
        "type": "yRecording",
        "unitOfMeasure": "m3",
        "recording": [{"y": 0.5, "c": 1}] * 24,
    }
    parsed = parse_cloud(payload, water, "2026-11-13", None)  # type: ignore[arg-type]
    assert parsed is not None and parsed.samples[0].y == pytest.approx(500.0)
    payload["unitOfMeasure"] = "gal"
    assert parse_cloud(payload, water, "2026-11-13", None) is None  # type: ignore[arg-type]


def test_an_implausible_amount_of_water_is_rejected() -> None:
    from custom_components.bosch_buderus_heating.recordings.parse import Sample

    water = BY_KEY["water"]
    now = local(2026, 11, 14, 10).astimezone(UTC)
    ok = Sample(parse_rate("hour"), date(2026, 11, 13), 1_794_000_000, 500.0, 1.0)
    bad = Sample(parse_rate("hour"), date(2026, 11, 13), 1_794_000_000, 50_000.0, 1.0)
    assert rejection(ok, water, now=now, zone=UTC, floor=None) is None
    assert rejection(bad, water, now=now, zone=UTC, floor=None) == "implausible"


def parse_rate(name: str):
    from custom_components.bosch_buderus_heating.recordings.parse import Rate

    return Rate.HOUR if name == "hour" else Rate.DAY


# ----------------------------------------------------------------- gating
def test_gating_follows_what_the_plant_shows() -> None:
    water, boiler = BY_KEY["water"], BY_KEY["total_consumed"]
    assert has_softener_circuit(SOFTENER_RESOURCES)
    assert not has_softener_circuit(BOILER_RESOURCES)
    assert allowed(water, SOFTENER_RESOURCES) and not allowed(water, BOILER_RESOURCES)
    assert allowed(boiler, BOILER_RESOURCES)
    # A heat pump has the energy monitoring: no boiler path is asked.
    pump = {*BOILER_RESOURCES, "/heatSources/emon/totalConsumption"}
    assert not has_boiler_shape(pump) and not allowed(boiler, pump)
    # A bare heat source (a room controller) is no anchor of a recording.
    bare = ["/heatSources/actualModulation"]
    assert has_boiler_shape(bare) and not allowed(boiler, bare)
    assert not has_boiler_shape(["/system/info"])
    assert allowed_keys(None) == frozenset()


# ------------------------------------------------------------ the softener
async def test_a_softener_plant_reads_only_its_water_series(
    hass: HomeAssistant,
) -> None:
    cloud = AddCloud({WATER: "l"})
    driver = Driver(clock())
    rec, _ = await plant(hass, SOFTENER_RESOURCES, cloud, (driver,))
    await rec.async_run("start")
    # No heat pump path, no local read (it has no such path).
    assert cloud.asked() == {WATER}
    assert driver.requests == []
    assert rec.available() == ("water",)
    day = rec.daily("water", date(2026, 11, 13), date(2026, 11, 13))
    assert [item.y for item in day] == [pytest.approx(240.0)]
    specs = rec.specs()
    assert {spec.period for spec in specs} == set(Period)
    # Disabled by default; named after the app's meaning.
    assert not any(spec.enabled_default for spec in specs)
    assert specs[0].measure.anchor == "/swCircuits/sw1/sensor/saltLevel"
    names = {entity_name("en", spec) for spec in specs}
    assert "Water consumption yesterday (system recording)" in names
    assert "Wasserverbrauch gestern (Anlagenaufzeichnung)" in {
        entity_name("de", spec) for spec in specs
    }
    assert rec.measures()[0].counter is None


@pytest.mark.parametrize(
    "case", ["not_found", "forbidden", "unknown_unit", "not_a_recording"]
)
async def test_a_water_path_that_does_not_answer_gives_no_entity(
    hass: HomeAssistant, case: str
) -> None:
    answers = {WATER: "gal"} if case == "unknown_unit" else {}
    cloud = AddCloud(answers, forbidden=(WATER,) if case == "forbidden" else ())
    rec, now = await plant(hass, SOFTENER_RESOURCES, cloud)
    if case == "not_a_recording":

        async def odd(gateway_id: str, path: str, interval: str) -> Any:
            cloud.calls.append((path, interval))
            return {"unexpected": True}

        cloud.get_recording = odd  # type: ignore[method-assign]
    await rec.async_run("start")
    assert cloud.asked() == {WATER}
    assert rec.available() == () and rec.specs() == ()
    # Once, not again at the next run.
    asked = len(cloud.calls)
    now.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert len(cloud.calls) == asked or case == "not_a_recording"


async def test_no_softener_series_without_a_softener_circuit(
    hass: HomeAssistant,
) -> None:
    """The plants without a softener are asked for nothing new: neither a
    heat pump (nor its cloud-only run) nor a plant with a bare heat source."""
    cloud = AddCloud({WATER: "l", **{path: "kWh" for path in BOILER_PATHS}})
    heat_pump = {"/heatSources/emon/totalConsumption": object()}
    rec, _ = await plant(hass, heat_pump, cloud)
    await rec.async_run("start")
    assert cloud.asked().isdisjoint({WATER, *BOILER_PATHS})
    assert rec.available() == ()
    # A room controller: no read at all.
    cloud2 = AddCloud({WATER: "l"})
    rec2, _ = await plant(hass, {"/heatSources/actualModulation": object()}, cloud2)
    await rec2.async_run("start")
    assert cloud2.calls == [] and rec2.waiting == "no_heat_source_anchor"


# ------------------------------------------------------------- the boiler
async def test_a_boiler_plant_keeps_only_the_paths_that_answer(
    hass: HomeAssistant,
) -> None:
    ch = BY_KEY["ch_actual_power"].path
    consumed = BY_KEY["total_consumed"].path
    cloud = AddCloud({ch: "kWh", consumed: "Wh"})  # dhw: 404
    rec, _ = await plant(hass, BOILER_RESOURCES, cloud)
    await rec.async_run("start")
    # The heat pump's energy paths are asked for too (a plant with a heat
    # source and no local access); the boiler series are the new ones.
    assert cloud.asked() >= BOILER_PATHS
    assert set(rec.available()) == {"ch_actual_power", "total_consumed"}
    by_key = {item.key: item for item in rec.measures()}
    assert set(by_key) == {"ch_actual_power", "total_consumed"}
    assert not any(spec.enabled_default for spec in rec.specs())
    names = {entity_name("en", spec) for spec in rec.specs()}
    assert f"Heating {DASH} Boiler energy yesterday (system recording)" in names
    assert f"Total {DASH} Energy consumed this year (system recording)" in names
    # Wh is converted to kWh.
    total = rec.daily("total_consumed", date(2026, 11, 13), date(2026, 11, 13))
    assert [item.y for item in total] == [pytest.approx(0.24)]


async def test_a_boiler_path_in_an_unknown_unit_gives_no_entity(
    hass: HomeAssistant,
) -> None:
    ch = BY_KEY["ch_actual_power"].path
    cloud = AddCloud({ch: "W"})
    rec, _ = await plant(hass, BOILER_RESOURCES, cloud)
    await rec.async_run("start")
    assert ch in cloud.asked()
    assert "ch_actual_power" not in rec.available()
    assert rec.cloud_paths[ch][0] == "unit"


async def test_boiler_paths_are_asked_after_a_local_run_without_the_local_access(
    hass: HomeAssistant,
) -> None:
    """The local access has no path of these series: a run it handled is
    followed by a cloud run for them only."""
    consumed = BY_KEY["total_consumed"].path
    cloud = AddCloud({consumed: "kWh"})
    now = clock()
    driver = Driver(now)
    rec = await manager(hass, now, [driver], cloud)
    rec._test_coordinator.resources = dict(BOILER_RESOURCES)
    await rec.async_run("start")
    assert rec.runs[-1]["source"] == "local"
    # Only the boiler paths from the cloud, none of the heat pump's.
    assert cloud.asked() == BOILER_PATHS
    assert all(path in LOCAL_PATHS for path, _ in driver.requests)
    assert "total_consumed" in rec.available()


async def test_a_local_run_of_a_heat_pump_asks_the_cloud_for_nothing(
    hass: HomeAssistant,
) -> None:
    cloud = AddCloud({WATER: "l"})
    now = clock()
    rec = await manager(hass, now, [Driver(now)], cloud)
    await rec.async_run("start")
    assert cloud.calls == []


def test_the_measure_names_and_the_default_activation() -> None:
    found = measures(("water", "ch_actual_power", "dhw_actual_power", "total_consumed"))
    assert {item.key for item in found} == {
        "water",
        "ch_actual_power",
        "dhw_actual_power",
        "total_consumed",
    }
    assert not any(spec.enabled_default for spec in entity_specs(found))
    ids = {item.path_id for item in found}
    assert "swCircuits:sw1:sensor:water" in ids
    assert "heatSources:total:energyMonitoring:consumedEnergy" in ids
