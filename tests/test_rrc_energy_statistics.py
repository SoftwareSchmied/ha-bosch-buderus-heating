"""W5 (CT-G05): the energy history as external statistics, through the real
setup of an RRC 2.0 room controller (``ct200-shapes.json``, firmware
05.04.00) and the fake cloud of ``tests/helpers/rrc_cloud.py``.

The history records come from ``energy.json`` (synthetic). The import is
expected, untested on real hardware: energy values as kWh, gas only with
a confirmed unit, nothing as the state of an entity (P39).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import rrc_energy_statistics
from custom_components.bosch_buderus_heating.pointt import Gateway, RateLimited
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_energy import (
    MAX_REQUESTS,
    parse_day,
)
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    CADENCE,
    START_DELAY,
    day_start,
    energy_store_key,
    history_of,
    statistic_id,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

try:
    from pytest_homeassistant.common import async_fire_time_changed
except ImportError:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.common import (  # type: ignore[no-redef]
        async_fire_time_changed,
    )

PACKAGE = "custom_components.bosch_buderus_heating"
FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "rrc" / "energy.json"
GW = "gateway-one"
HISTORY = "/energy/history"
START = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
ADD = "homeassistant.components.recorder.statistics.async_add_external_statistics"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def records() -> list[dict[str, object]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["provenance"]["physical_device_verified"] is False
    return list(data["records"])


def day_record(day: date, **values: float) -> dict[str, object]:
    return {"d": day.strftime("%d-%m-%Y"), "T": 5.0, "eCh": 1.0, **values}


def history_cloud(
    items: list[dict[str, object]], *, page: int = 4, gas_unit: str = "kWh"
) -> RrcCloud:
    """The CT200 tree with a history answered by date or by entry."""
    cloud = RrcCloud(load_tree("rrc/ct200-shapes.json"))
    cloud.tree["/energy/gas/unit"] = parse_resource(
        {"id": "/energy/gas/unit", "type": "stringValue", "value": gas_unit}
    )
    cloud.tree["/energy/historyEntries"] = parse_resource(
        {"id": "/energy/historyEntries", "type": "floatValue", "value": len(items)}
    )
    cloud.history = items  # type: ignore[attr-defined]
    cloud.history_queries = []  # type: ignore[attr-defined]

    async def query(gateway_id, path, query, **_kwargs):
        cloud.calls["GET?", path] += 1
        cloud.history_queries.append(dict(query))  # type: ignore[attr-defined]
        cloud._check_rate_limit(path)
        assert path == HISTORY
        if "entry" in query:
            start = int(query["entry"])
            chunk = cloud.history[start : start + page]  # type: ignore[attr-defined]
        else:
            first = date.fromisoformat(str(query["date"]))
            chunk = [
                item
                for item in cloud.history  # type: ignore[attr-defined]
                if (day := parse_day(item.get("d"))) is not None and day >= first
            ][:page]
        return parse_resource(
            {"id": HISTORY, "type": "energyRecordings", "value": list(chunk)}
        )

    cloud.get_resource_query = query  # type: ignore[method-assign]
    return cloud


def history_requests(cloud: RrcCloud) -> int:
    return sum(
        count
        for (method, path), count in cloud.calls.items()
        if path.startswith("/energy")
    )


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def start(hass: HomeAssistant, cloud: RrcCloud, entry=None):
    entry = entry or _entry(hass)
    stack = cloud_patches(cloud, Clock(), Gateway(GW))
    stack.__enter__()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, stack


async def stop(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.__exit__(None, None, None)


@pytest.fixture
def recorder_loaded(hass: HomeAssistant):
    """The recorder counts as loaded; its import is recorded instead."""
    hass.config.components.add("recorder")
    added = MagicMock()
    with patch(ADD, added):
        yield added


# --------------------------------------------------------------- schedule
async def test_nothing_is_read_at_the_start_and_nothing_without_a_recorder(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    cloud = history_cloud(records())
    entry, stack = await start(hass, cloud)
    try:
        assert history_requests(cloud) == 0
        history = history_of(entry.runtime_data.coordinators[0])
        assert history is not None
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.waiting == "recorder_unavailable"
        assert history_requests(cloud) == 0
        # No entity carries the history (P39).
        registry = er.async_get(hass)
        assert not [
            item
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if "energy" in item.unique_id and "history" in item.unique_id
        ]
    finally:
        await stop(hass, entry, stack)


async def test_cycles_come_every_three_hours_with_a_bounded_budget(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    first = date(2026, 9, 1)
    items = [day_record(first + timedelta(days=n)) for n in range(49)]
    cloud = history_cloud(items, page=3)
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY - timedelta(minutes=1))
        assert history_requests(cloud) == 0
        await advance(hass, freezer, timedelta(minutes=2))
        assert history_requests(cloud) == MAX_REQUESTS
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.state.reason == "request_budget_reached"
        assert history.state.mode == "date"
        assert recorder_loaded.call_count > 0
        reads = history_requests(cloud)
        await advance(hass, freezer, CADENCE - timedelta(minutes=5))
        assert history_requests(cloud) == reads
        await advance(hass, freezer, timedelta(minutes=10))
        assert history_requests(cloud) - reads <= MAX_REQUESTS
        assert history_requests(cloud) > reads
        # The gas unit was read once for the day.
        assert cloud.calls["GET", "/energy/gas/unit"] == 1
        # No statistic holds an identifier of the gateway.
        for call in recorder_loaded.call_args_list:
            metadata = call.args[1]
            assert GW not in json.dumps(metadata, default=str)
            assert metadata["statistic_id"].startswith("bosch_buderus_heating:rrc_")
    finally:
        await stop(hass, entry, stack)


async def test_no_read_while_the_cloud_is_paused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    cloud = history_cloud(records())
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        coordinator.client.backoff.activate(3600)
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        history = history_of(coordinator)
        assert history.waiting == "cloud_unavailable"
        assert history_requests(cloud) == 0
    finally:
        await stop(hass, entry, stack)


async def test_a_rate_limit_on_the_history_pauses_the_cloud_access(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    cloud = history_cloud(records())
    cloud.rate_limit[HISTORY] = 7200
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        history = history_of(coordinator)
        assert history.state.reason == "rate_limited"
        assert coordinator.client.backoff.remaining > 3000
        assert coordinator.cloud_unavailable()
        # The next cycle waits for the pause and the cadence.
        assert history.state.next_due >= dt_util.utcnow() + timedelta(
            hours=1, minutes=59
        )
        assert cloud.calls["GET?", HISTORY] == 1
    finally:
        await stop(hass, entry, stack)


async def test_a_restart_continues_where_the_import_stopped(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage,
    recorder_loaded,
) -> None:
    freezer.move_to(START)
    first = date(2026, 9, 1)
    items = [day_record(first + timedelta(days=n)) for n in range(49)]
    cloud = history_cloud(items, page=3)
    entry, stack = await start(hass, cloud)
    await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
    history = history_of(entry.runtime_data.coordinators[0])
    last_day = history.state.last_day
    due = history.state.next_due
    await stop(hass, entry, stack)
    key = energy_store_key(entry.entry_id, GW)
    stored = json.dumps(hass_storage[key])
    assert GW not in stored and "MARKER" not in stored

    cloud.history_queries.clear()  # type: ignore[attr-defined]
    entry, stack = await start(hass, cloud, entry)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.state.last_day == last_day
        # A restart neither reads at once nor earlier than planned.
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert dt_util.utcnow() < due
        assert cloud.history_queries == []  # type: ignore[attr-defined]
        await advance(hass, freezer, due - dt_util.utcnow() + timedelta(minutes=1))
        assert cloud.history_queries[0] == {  # type: ignore[attr-defined]
            "date": (last_day + timedelta(days=1)).isoformat()
        }
    finally:
        await stop(hass, entry, stack)


async def test_removing_the_entry_removes_the_import_state(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage,
    recorder_loaded,
) -> None:
    freezer.move_to(START)
    cloud = history_cloud(records())
    entry, stack = await start(hass, cloud)
    await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
    await stop(hass, entry, stack)
    key = energy_store_key(entry.entry_id, GW)
    assert key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


def test_day_starts_are_local_midnight_at_the_top_of_an_hour() -> None:
    berlin = dt_util.get_time_zone("Europe/Berlin")
    assert day_start(date(2026, 10, 25), berlin) == datetime(
        2026, 10, 24, 22, 0, tzinfo=UTC
    )
    assert day_start(date(2026, 10, 26), berlin) == datetime(
        2026, 10, 25, 23, 0, tzinfo=UTC
    )
    kolkata = dt_util.get_time_zone("Asia/Kolkata")
    assert day_start(date(2026, 10, 26), kolkata) == datetime(
        2026, 10, 25, 18, 0, tzinfo=UTC
    )
    assert statistic_id(GW, "gas_heating") == statistic_id(GW, "gas_heating")
    assert GW not in statistic_id(GW, "gas_heating")


async def test_gas_in_another_unit_stays_out_of_the_recorder(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    cloud = history_cloud(records(), page=20, gas_unit="m3")
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        written = {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
        assert statistic_id(GW, "heat_pump_electricity_heating") in written
        assert statistic_id(GW, "gas_heating") not in written
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.state.reason == "gas_unit_unconfirmed"
        report = rrc_energy_statistics.diagnostics(entry.runtime_data.coordinators[0])
        assert report["gas_unit_confirmed"] is False
        assert "gas_heating" not in report["statistics"]
    finally:
        await stop(hass, entry, stack)


async def test_a_history_answer_with_rate_limit_counts_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    """The pause asked for is kept, never shortened by the import."""
    freezer.move_to(START)
    cloud = history_cloud(records())
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        with patch.object(
            coordinator.client,
            "get_resource_query",
            AsyncMock(side_effect=RateLimited(retry_after=None)),
        ):
            await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        history = history_of(coordinator)
        assert history.state.reason == "rate_limited"
        assert coordinator.client.backoff.remaining > 0
        assert history.state.next_due >= dt_util.utcnow() + CADENCE - timedelta(
            minutes=1
        )
    finally:
        await stop(hass, entry, stack)
