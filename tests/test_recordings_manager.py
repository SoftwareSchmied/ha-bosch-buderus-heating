"""The reads of the plant's recordings: request plan, sources and values.

A manager with a fake local source and a fake cloud client; the answers
follow the shapes read from a real installation. Expected, untested on a
real installation: the request plan over days, the switch to the cloud and
the change of summer time.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_read import (
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.recordings import (
    Period,
    recordings_view,
)
from custom_components.bosch_buderus_heating.recordings.manager import (
    _MANAGERS,
    CLOUD_BUDGET,
    OPTION_IMPORT,
    RecordingsManager,
    statistic_id,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    CALCULATED,
    RECORDING,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    BY_PATH,
    CLOUD_PATHS,
    LOCAL_PATHS,
)
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    async_gateway_digest,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_recordings_core import days, fixed, hours, local, recording

Z = ZoneInfo("Europe/Berlin")
GW = "gateway-recordings-1"
PLANT = {
    "total_heat": 2.0,
    "total_compressor": 0.5,
    "total_eheater": 0.0,
    "ch_heat": 1.5,
    "outdoor_temperature": 300.0,
}
FORBIDDEN = "/recordings/heatSources/emon/total/burner"


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now


class Driver:
    """A local source: the plant's paths answer, the others 404, one 403."""

    def __init__(self, clock: Clock, *, reachable: bool = True) -> None:
        self.clock, self.reachable = clock, reachable
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def read_explicit(self, request):
        self.requests.append((request.path, dict(request.query)))
        if not self.reachable:
            return ReadResult(request, ReadState.ACCESS_ERROR, problem="connect")
        series = BY_PATH[request.path]
        if request.path == FORBIDDEN:
            return ReadResult(request, ReadState.PATH_ERROR, 403, ReadProblem.FORBIDDEN)
        if series.key not in PLANT:
            return ReadResult(request, ReadState.PATH_ERROR, 404, ReadProblem.NOT_FOUND)
        value = PLANT[series.key]
        count = 60 if series.mean else 1
        now = self.clock().astimezone(Z)
        current = now.replace(minute=0, second=0, microsecond=0)
        query = dict(request.query)
        groups = []
        if query["sampleRate"] == "all":
            first = date(2026, 11, 1)
            groups.append(
                days(
                    first, [(value * 24, count * 24)] * ((now.date() - first).days + 1)
                )
            )
            start = datetime.combine(
                now.date() - timedelta(days=5), datetime.min.time(), Z
            )
        else:
            start = datetime.combine(
                date.fromisoformat(query["startDate"]), datetime.min.time(), Z
            )
        elapsed = int((current - start).total_seconds() // 3600) + 1
        groups.append(hours(start, [(value, count)] * elapsed, end=current))
        unit = "C" if series.mean else "kWh"
        return ReadResult(
            request,
            ReadState.SUCCESS,
            200,
            observation=SimpleNamespace(data=recording(*groups, unit=unit)),
        )


class Cloud:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def get_recording(self, gateway_id: str, path: str, interval: str):
        assert gateway_id == GW
        self.calls.append((path, interval))
        if self.fail is not None:
            raise self.fail
        if path.endswith("/total/eheater"):
            raise ResourceNotFound(path, 404)
        if len(interval) == 10:
            rows = [{"y": 1.0, "c": 1}] * 24
        elif len(interval) == 7:
            rows = [{"y": 24.0, "c": 24}] * 30
        else:
            rows = [{"y": 700.0, "c": 1}] * 12
        return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


class Coordinator:
    def __init__(self, hass, entry, drivers=(), cloud=None, *, cloud_down=False):
        self.hass, self.config_entry = hass, entry
        self.gateway = SimpleNamespace(gateway_id=GW)
        self.local_selection = SimpleNamespace(running_sources=lambda: tuple(drivers))
        self.client = cloud
        self._down = cloud_down
        self.resources = {"/heatSources/emon/totalConsumption": object()}

    def cloud_unavailable(self) -> bool:
        return self._down


async def manager(hass, clock, drivers=(), cloud=None, **kwargs):
    await hass.config.async_update(time_zone="Europe/Berlin")
    entry = kwargs.pop("entry", None) or MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    result = RecordingsManager(hass, entry, GW, now=clock)
    coordinator = Coordinator(hass, entry, drivers, cloud, **kwargs)
    result.attach(coordinator)
    result._test_coordinator = coordinator  # keep it alive (weak set)
    hass.data.setdefault(_MANAGERS, {})[(entry.entry_id, GW)] = result
    await result.async_start(schedule=False)
    return result


def key(measure: str, period: Period, method: str = RECORDING) -> str:
    from custom_components.bosch_buderus_heating.recordings.measures import (
        measures,
    )

    found = next(item for item in measures(PLANT) if item.key == measure)
    suffix = ":calculated" if method == CALCULATED else ""
    return f"{found.path_id}:{period.value}{suffix}"


# ---------------------------------------------------------- request plan
async def test_the_start_reads_the_whole_history_then_hourly_the_last_two_days(
    hass: HomeAssistant,
) -> None:
    """V5 and rule 15: ``all`` once per path at the start; then hourly the
    hours of yesterday and today of the paths that answered; 404 again
    after a day, 403 not before the next start."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    assert len(driver.requests) == len(LOCAL_PATHS)
    assert all(query == {"sampleRate": "all"} for _, query in driver.requests)
    assert set(rec.available()) == set(PLANT)
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("hourly")
    assert {path for path, _ in driver.requests} == {
        BY_KEY[item].path for item in PLANT
    }
    assert all(
        query
        == {"sampleRate": "PT1H", "startDate": "2026-11-12", "endDate": "2026-11-14"}
        for _, query in driver.requests
    )
    driver.requests.clear()
    clock.now += timedelta(hours=25)
    await rec.async_run("hourly")
    again = {path for path, query in driver.requests if query == {"sampleRate": "all"}}
    assert len(again) == len(LOCAL_PATHS) - len(PLANT) - 1
    assert FORBIDDEN not in {path for path, _ in driver.requests}


async def test_nothing_is_read_when_nothing_needs_the_recordings(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")  # once after the start, to find the series
    driver.requests.clear()
    await rec.async_run("hourly")
    assert driver.requests == []
    assert rec.waiting == "not_needed"
    release = recordings_view(hass, GW).acquire()
    await rec.async_run("hourly")
    assert driver.requests
    release()
    release()  # once only
    assert rec._consumers == 0


async def test_without_the_local_access_the_cloud_reads_the_energy_paths(
    hass: HomeAssistant,
) -> None:
    """Rules 11 and 16, V5: local unreachable, so the cloud; only the nine
    energy paths, today and yesterday first, within the budget."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    driver = Driver(clock, reachable=False)
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    assert len(driver.requests) == 1  # stopped at the first unreachable read
    assert 0 < len(cloud.calls) <= CLOUD_BUDGET
    assert {path for path, _ in cloud.calls} <= set(CLOUD_PATHS)
    first = cloud.calls[0][0]
    assert cloud.calls[:2] == [(first, "2026-11-14"), (first, "2026-11-13")]
    assert rec.cloud_paths["/recordings/heatSources/emon/total/eheater"][0] == "missing"
    assert rec.runs[-1]["source"] == "cloud"
    first = len(cloud.calls)
    # The next run goes on with the older intervals, not again the same.
    clock.now += timedelta(hours=1)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("hourly")
    later = cloud.calls[first:]
    assert ("/recordings/heatSources/emon/total/eheater", "2026-11-14") not in later
    assert len(later) <= CLOUD_BUDGET


@pytest.mark.parametrize(
    "error", [RateLimited(60), ResourceForbidden("/recordings/x", 403)]
)
async def test_a_rate_limit_or_refusal_ends_or_skips_cloud_reads(
    hass: HomeAssistant, error: Exception
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    cloud = Cloud(fail=error)
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    if isinstance(error, RateLimited):
        assert len(cloud.calls) == 1
    else:
        # A refused path is not asked again before the next start.
        assert len({path for path, _ in cloud.calls}) == len(CLOUD_PATHS)
        assert len(cloud.calls) == len(CLOUD_PATHS)


async def test_no_cloud_reads_while_the_cloud_is_unavailable_or_for_no_heat_source(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud, cloud_down=True)
    await rec.async_run("start")
    assert cloud.calls == [] and rec.runs[-1]["cloud"] == "unavailable"
    rec._test_coordinator._down = False
    rec._test_coordinator.resources = {"/system/info": object()}
    rec._detected = False
    await rec.async_run("start")
    assert cloud.calls == [] and rec.runs[-1]["cloud"] == "no_heat_source"


# ----------------------------------------------------------------- values
async def test_yesterday_switches_only_once_the_new_day_is_final(
    hass: HomeAssistant,
) -> None:
    """Rule 4 and V2: the electricity is compressor plus heater; a past
    period keeps its value until the new one is confirmed."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    rec = await manager(hass, clock, [Driver(clock)])
    heat = key("total_heat", Period.YESTERDAY)
    power = key("total_electricity", Period.YESTERDAY)
    rec.request(heat)
    rec.request(power)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    specs = {spec.key: spec for spec in rec.specs()}
    assert specs[heat].enabled_default and specs[power].enabled_default
    shown = rec.shown(specs[heat])
    assert shown is not None and shown.value == pytest.approx(48.0)
    assert shown.start == date(2026, 11, 13) and shown.source == "local_recording"
    assert rec.shown(specs[power]).value == pytest.approx(12.0)
    # Just after midnight: the new yesterday is read once only.
    clock.now = (local(2026, 11, 15, 0) + timedelta(minutes=20)).astimezone(UTC)
    await rec.async_run("hourly")
    assert rec.shown(specs[heat]).start == date(2026, 11, 13)
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert rec.shown(specs[heat]).start == date(2026, 11, 14)


async def test_entities_default_and_comparison_methods(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    specs = rec.specs()
    enabled = sorted(spec.key for spec in specs if spec.enabled_default)
    assert enabled == sorted(
        [
            key("total_heat", Period.YESTERDAY),
            key("total_electricity", Period.YESTERDAY),
        ]
    )
    by_key = {spec.key: spec for spec in specs}
    # The heat per mode comes from the counters first.
    assert by_key[key("ch_heat", Period.YESTERDAY, CALCULATED)].primary
    assert not by_key[key("ch_heat", Period.YESTERDAY)].primary
    # No comparison for temperatures.
    assert key("outdoor_temperature", Period.YESTERDAY, CALCULATED) not in by_key
    assert len({spec.key for spec in specs}) == len(specs)


async def test_the_state_survives_a_restart(hass: HomeAssistant, hass_storage) -> None:
    """Rule 9: the series and the shown values are stored per gateway."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    rec.request(key("total_heat", Period.CURRENT_MONTH))
    await rec.async_run("start")
    await hass.async_block_till_done()
    await rec._store.async_save(rec._data())
    again = RecordingsManager(hass, entry, GW, now=clock)
    await again.async_start(schedule=False)
    assert set(again.available()) == set(PLANT)
    spec = next(
        s for s in again.specs() if s.key == key("total_heat", Period.CURRENT_MONTH)
    )
    assert again.shown(spec) is not None
    stored = json.dumps(hass_storage)
    assert GW not in stored


async def test_gateways_have_separate_statistics(hass: HomeAssistant) -> None:
    """Rule 23 and 18: a stable ID per gateway, keyed, never the gateway ID."""
    one = await async_gateway_digest(hass, "gateway-a")
    two = await async_gateway_digest(hass, "gateway-b")
    series = BY_KEY["total_heat"]
    assert statistic_id(one, series) != statistic_id(two, series)
    assert statistic_id(one, series) == statistic_id(
        await async_gateway_digest(hass, "gateway-a"), series
    )
    assert "gateway" not in statistic_id(one, series)


async def test_diagnostics_hold_states_and_counts_only(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    report = rec.diagnostics()
    text = json.dumps(report)
    assert GW not in text and "48.0" not in text
    assert report["series"]["total_heat"]["local"] == "ok"
    assert report["series"]["total_burner"]["local"] == "forbidden"
    assert report["import_enabled"] is False
    assert report["last_run"]["source"] == "local"


async def test_the_daily_interface_for_other_features(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    await manager(hass, clock, [Driver(clock)])
    view = recordings_view(hass, GW)
    assert view is not None and view.loaded
    rec = view._manager
    await rec.async_run("start")
    assert "total_electricity" in view.measure_keys()
    rows = view.daily("total_electricity", date(2026, 11, 1), date(2026, 11, 3))
    assert [row.value for row in rows] == [12.0, 12.0, 12.0]
    temperature = view.daily(
        "outdoor_temperature", date(2026, 11, 1), date(2026, 11, 1)
    )
    assert temperature[0].value == pytest.approx(5.0)
    assert view.first_day("total_heat") == date(2026, 11, 1)
    month = view.monthly("total_heat", date(2026, 11, 1), date(2026, 11, 14))
    assert month is not None and not month.final
    assert (
        view.period("total_heat", Period.CURRENT_YEAR, date(2026, 11, 14)) is not None
    )
    assert view.daily("unknown", date(2026, 11, 1), date(2026, 11, 2)) == ()
    assert recordings_view(hass, "other") is None


async def test_the_import_option_counts_as_a_need(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    rec._detected = True
    assert rec.needed()
    # Without a recorder nothing is imported, the reads go on.
    await rec.async_run("hourly")
    assert rec.runs[-1]["import"] == "recorder_unavailable"


def test_fixed_offsets_helper() -> None:
    assert fixed(local(2026, 7, 1)).utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(("value", "discarded"), [(16.0, False), (91.0, True)])
async def test_one_fixed_plausibility_limit_for_every_recording(
    hass: HomeAssistant, monkeypatch, value, discarded
) -> None:
    """Rule 13: 90 kWh per hour (1.5 x 60 kW) for every energy recording,
    whatever the heat generator; an hour with a heater above the nominal
    output of the outdoor unit stays."""
    monkeypatch.setitem(PLANT, "total_heat", value)
    monkeypatch.setitem(PLANT, "total_eheater", 9.0)
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    assert rec.diagnostics()["plausibility_limit_kwh_per_hour"] == 90.0
    stats = rec.ledgers["total_heat"].stats
    assert (stats["discarded_implausible"] > 0) is discarded
    assert rec.ledgers["total_eheater"].stats["discarded_implausible"] == 0
