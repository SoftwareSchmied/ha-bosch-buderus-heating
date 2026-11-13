"""W5 (CT-G05): the energy history as external statistics, through the real
setup of an RRC 2.0 room controller (``ct200-shapes.json``, firmware
05.04.00) and the fake cloud of ``tests/helpers/rrc_cloud.py``.

The history records come from ``energy.json`` (synthetic). The import is
expected, untested on real hardware: energy values as kWh, gas only with
a confirmed unit, nothing as the state of an entity (P39).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import rrc_energy_statistics
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    RateLimited,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_energy import (
    HEAT_SOURCE_TYPE_PATH,
    HEAT_SOURCE_WAIT,
    MAX_REQUESTS,
    parse_day,
)
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    CADENCE,
    RETRY_DELAY,
    START_DELAY,
    async_gateway_digest,
    day_start,
    energy_store_key,
    gateway_digest,
    history_of,
    row_day,
    statistic_id,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry
from tests.test_rrc_budget_all_packages import PLANTS

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
LAST_ROWS = f"{PACKAGE}.rrc_energy_statistics.async_last_rows"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


async def sid(hass: HomeAssistant, slug: str, gateway: str = GW) -> str:
    """The statistic ID of a gateway on this installation."""
    return statistic_id(await async_gateway_digest(hass, gateway), slug)


async def store_key(hass: HomeAssistant, entry_id: str, gateway: str = GW) -> str:
    """The name of the import state of a gateway on this installation."""
    return energy_store_key(entry_id, await async_gateway_digest(hass, gateway))


def records() -> list[dict[str, object]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["provenance"]["physical_device_verified"] is False
    return list(data["records"])


def day_record(day: date, **values: float) -> dict[str, object]:
    return {"d": day.strftime("%d-%m-%Y"), "T": 5.0, "eCh": 1.0, **values}


def history_cloud(
    items: list[dict[str, object]],
    *,
    page: int = 4,
    gas_unit: str = "kWh",
    heat_source: str | None = "Hybrid",
    tree: dict[str, Resource] | None = None,
) -> RrcCloud:
    """The CT200 tree with a history answered by date or by entry; without
    a query from the first record on."""
    cloud = RrcCloud(load_tree("rrc/ct200-shapes.json") if tree is None else tree)
    cloud.tree["/energy/gas/unit"] = parse_resource(
        {"id": "/energy/gas/unit", "type": "stringValue", "value": gas_unit}
    )
    cloud.tree["/energy/historyEntries"] = parse_resource(
        {"id": "/energy/historyEntries", "type": "floatValue", "value": len(items)}
    )
    if heat_source is not None:
        cloud.tree[HEAT_SOURCE_TYPE_PATH] = parse_resource(
            {"id": HEAT_SOURCE_TYPE_PATH, "type": "stringValue", "value": heat_source}
        )
    cloud.history = items  # type: ignore[attr-defined]
    cloud.history_queries = []  # type: ignore[attr-defined]
    plain_get = cloud.get_resource

    async def get(gateway_id, path, **kwargs):
        if path != HISTORY:
            return await plain_get(gateway_id, path, **kwargs)
        cloud.calls["GET", path] += 1
        cloud.history_queries.append({})  # type: ignore[attr-defined]
        cloud._check_rate_limit(path)
        chunk = cloud.history[:page]  # type: ignore[attr-defined]
        return parse_resource(
            {"id": HISTORY, "type": "energyRecordings", "value": list(chunk)}
        )

    cloud.get_resource = get  # type: ignore[method-assign]

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
    """The requests of the history import (the plant type only for it)."""
    return sum(
        count
        for (method, path), count in cloud.calls.items()
        if path.startswith("/energy")
        or (method == "GET" and path == HEAT_SOURCE_TYPE_PATH)
    )


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def start(hass: HomeAssistant, cloud: RrcCloud, entry=None, gateway=None):
    entry = entry or _entry(hass)
    stack = cloud_patches(cloud, Clock(), gateway or Gateway(GW))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, stack


async def stop(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


@pytest.fixture
def last_rows():
    """No statistics recorded before (the recorder is not running)."""
    with patch(LAST_ROWS, AsyncMock(return_value={})) as mock:
        yield mock


@pytest.fixture
def recorder_loaded(hass: HomeAssistant, last_rows):
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
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded, last_rows
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
        # The gas unit was read once for the day, the recorded statistics
        # once for the empty state.
        assert cloud.calls["GET", "/energy/gas/unit"] == 1
        assert last_rows.await_count == 1
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
        assert cloud.calls["GET", HISTORY] + cloud.calls["GET?", HISTORY] == 1
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
    key = await store_key(hass, entry.entry_id)
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
        # It asks from the last known day on, which the controller has.
        assert cloud.history_queries[0] == {  # type: ignore[attr-defined]
            "date": last_day.isoformat()
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
    key = await store_key(hass, entry.entry_id)
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
    # A recorded row gives its day back, also in a half-hour zone.
    for zone in (berlin, kolkata, dt_util.get_time_zone("America/St_Johns")):
        for day in (date(2026, 3, 29), date(2026, 10, 25), date(2026, 10, 26)):
            assert row_day(day_start(day, zone).timestamp(), zone) == day
    assert GW not in statistic_id(gateway_digest("key", GW), "gas_heating")


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
        assert await sid(hass, "heat_pump_electricity_heating") in written
        assert await sid(hass, "gas_heating") not in written
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


# ------------------------------------------------- deep review (SYSTEM-02/05/14)
def legacy_record(day: date, **values: float) -> dict[str, object]:
    """A record of older firmware: ``eCH``/``eHW`` instead of the keys."""
    return {"d": day.strftime("%d-%m-%Y"), "T": 5.0, "eCH": 3.0, "eHW": 1.0, **values}


async def test_a_boiler_never_records_older_keys_as_heat_pump_electricity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    """SYSTEM-02: on a boiler ``eCH``/``eHW`` are gas, here without a
    confirmed unit, so only the temperature is recorded."""
    freezer.move_to(START)
    first = date(2026, 10, 15)
    items = [legacy_record(first + timedelta(days=n)) for n in range(4)]
    cloud = history_cloud(items, page=10, gas_unit="m3", heat_source="Boiler")
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        written = {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
        assert written == {await sid(hass, "outdoor_temperature")}
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.state.heat_source == "gas"
        assert history.state.reason == "gas_unit_unconfirmed"
        assert history.state.last_day == date(2026, 10, 18)
    finally:
        await stop(hass, entry, stack)


async def test_an_unreadable_recorder_holds_the_first_import_back(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded, last_rows
) -> None:
    """SYSTEM-05: without the recorded rows an empty state imports nothing."""
    freezer.move_to(START)
    cloud = history_cloud(records())
    last_rows.side_effect = RuntimeError("database is locked")
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.waiting == "statistics_unreadable"
        assert history_requests(cloud) == 0
        assert recorder_loaded.call_count == 0
        assert not history.state.statistics_checked
        assert history.state.next_due == dt_util.utcnow() + RETRY_DELAY
        last_rows.side_effect = None
        await advance(hass, freezer, RETRY_DELAY + timedelta(minutes=1))
        assert history.waiting is None
        assert history.state.statistics_checked
        assert history_requests(cloud) > 0
        assert recorder_loaded.call_count > 0
    finally:
        await stop(hass, entry, stack)


@pytest.mark.parametrize("plant", sorted(PLANTS))
async def test_t2_the_energy_history_stays_within_its_budget(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded, plant
) -> None:
    """SYSTEM-14: T2 with the energy history. Thirty hours of cycles on both
    room controller trees: every cycle at most eight requests (the plant
    type read for older keys included), none between the cycles."""
    build, gateway, *_ = PLANTS[plant]
    freezer.move_to(START)
    first = START.date() - timedelta(days=150)
    items = [
        (legacy_record if n % 10 == 0 else day_record)(first + timedelta(days=n))
        for n in range(150)
    ]
    # No plant type in the tree: it answers HTTP 500 like a missing path.
    cloud = history_cloud(items, page=5, tree=build(), heat_source=None)
    entry, stack = await start(hass, cloud, gateway=gateway)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        assert history is not None
        reads = history_requests(cloud)
        await advance(hass, freezer, START_DELAY - timedelta(minutes=1))
        assert history_requests(cloud) == reads
        per_cycle: list[int] = []
        days: list[date | None] = []
        await advance(hass, freezer, timedelta(minutes=2))
        first_cycle = dt_util.utcnow()
        held: list[bool] = []
        for _cycle in range(10):
            per_cycle.append(history_requests(cloud) - reads)
            reads = history_requests(cloud)
            days.append(history.state.last_day)
            held.append(history.state.reason == "heat_source_type_unavailable")
            # E2: the older keys hold the import for a day at most.
            assert held[-1] is (dt_util.utcnow() - first_cycle < HEAT_SOURCE_WAIT), (
                _cycle
            )
            assert history.state.last_requests == per_cycle[-1]
            # Nothing between two cycles.
            await advance(hass, freezer, CADENCE - timedelta(minutes=5))
            assert history_requests(cloud) == reads
            await advance(hass, freezer, timedelta(minutes=6))
        assert all(0 < count <= MAX_REQUESTS for count in per_cycle), per_cycle
        assert sum(per_cycle) <= 10 * MAX_REQUESTS
        # The first answer has older keys: held for a day, nothing passed.
        assert held == [True] * 8 + [False] * 2
        assert days[:8] == [None] * 8
        # Then the import goes on: while the type stays unreadable the older
        # keys are left out (VER-04), never filed as gas or as heat pump
        # electricity, and the type is read again.
        assert history.state.heat_source is None
        assert [day for day in days if day is not None] == sorted(
            day for day in days if day is not None
        )
        assert days[-1] is not None and days[-1] > first
        written = {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
        assert await sid(hass, "heat_pump_electricity_heating") in written
        assert await sid(hass, "gas_heating") not in written
    finally:
        await stop(hass, entry, stack)


# ------------------------------------------------- E1: keyed statistic IDs
def test_the_digest_is_keyed_and_cannot_be_found_back_by_hashing() -> None:
    key = "0123456789abcdef0123456789abcdef"
    digest = gateway_digest(key, GW)
    assert (
        digest == hmac.new(key.encode(), GW.encode(), hashlib.sha256).hexdigest()[:16]
    )
    assert len(digest) == 16
    # Not the plain hash of the identifier, which anybody can compute.
    assert digest != hashlib.sha256(GW.encode()).hexdigest()[:16]
    assert gateway_digest(key, GW) == digest  # stable
    assert gateway_digest("another installation", GW) != digest
    assert gateway_digest(key, "gateway-two") != digest
    assert statistic_id(digest, "gas_heating") == (
        f"bosch_buderus_heating:rrc_{digest[:12]}_gas_heating"
    )
    assert energy_store_key("entry", digest) == (
        f"bosch_buderus_heating.rrc_energy.entry.{digest}"
    )


async def _written_ids(hass, freezer, cloud, recorder_loaded, entry=None):
    entry, stack = await start(hass, cloud, entry)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        return entry, {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
    finally:
        await stop(hass, entry, stack)


async def test_statistic_ids_follow_the_instance_and_survive_a_new_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage,
    recorder_loaded,
) -> None:
    """E1: the IDs are stable when the entry is added again and after a
    restored backup (the same instance ID), and the import state is named
    by the same keyed digest."""
    freezer.move_to(START)
    key = await instance_id.async_get(hass)
    digest = gateway_digest(key, GW)
    entry, first = await _written_ids(
        hass, freezer, history_cloud(records()), recorder_loaded
    )
    assert first
    assert statistic_id(digest, "heat_pump_electricity_heating") in first
    unkeyed = hashlib.sha256(GW.encode()).hexdigest()[:16]
    assert not [item for item in first if unkeyed[:12] in item]
    # The store name follows the digest.
    assert energy_store_key(entry.entry_id, digest) in hass_storage
    assert energy_store_key(entry.entry_id, unkeyed) not in hass_storage
    # Removing the entry removes its state; a new entry for the same
    # gateway writes the same statistics again.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert energy_store_key(entry.entry_id, digest) not in hass_storage
    recorder_loaded.reset_mock()
    freezer.tick(CADENCE)
    entry, second = await _written_ids(
        hass, freezer, history_cloud(records()), recorder_loaded
    )
    assert second == first
    assert energy_store_key(entry.entry_id, digest) in hass_storage


async def test_another_installation_gets_other_statistic_ids(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    with patch(
        "homeassistant.helpers.instance_id.async_get",
        AsyncMock(return_value="installation-b"),
    ):
        _entry_b, written = await _written_ids(
            hass, freezer, history_cloud(records()), recorder_loaded
        )
    assert written
    digest_b = gateway_digest("installation-b", GW)
    assert all(
        item.startswith(f"bosch_buderus_heating:rrc_{digest_b[:12]}_")
        for item in written
    )
    own = gateway_digest(await instance_id.async_get(hass), GW)
    assert own != digest_b
    assert not [item for item in written if own[:12] in item]


async def test_the_restored_instance_id_keeps_the_statistic_ids(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage, recorder_loaded
) -> None:
    """A backup brings the instance ID back (``core.uuid``)."""
    restored = "f" * 32
    hass_storage["core.uuid"] = {
        "version": 1,
        "minor_version": 1,
        "key": "core.uuid",
        "data": {"uuid": restored},
    }
    hass.data.pop("core.uuid", None)  # nothing read yet in this process
    freezer.move_to(START)
    _entry_r, written = await _written_ids(
        hass, freezer, history_cloud(records()), recorder_loaded
    )
    digest = gateway_digest(restored, GW)
    assert statistic_id(digest, "heat_pump_electricity_heating") in written


async def test_files_of_the_unkeyed_digest_are_removed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage, recorder_loaded
) -> None:
    """Earlier test builds named the state by the plain digest: it is
    removed at the start and with the entry, never read."""
    freezer.move_to(START)
    entry = _entry(hass)
    unkeyed = hashlib.sha256(GW.encode()).hexdigest()[:16]
    old = energy_store_key(entry.entry_id, unkeyed)
    hass_storage[old] = {
        "version": 1,
        "minor_version": 1,
        "key": old,
        "data": {"version": 1, "last_day": "2026-10-19", "cycles": 7},
    }
    entry, stack = await start(hass, history_cloud(records()), entry)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        assert old not in hass_storage
        assert history.state.cycles == 0  # not taken over
    finally:
        await stop(hass, entry, stack)
    hass_storage[old] = {"version": 1, "minor_version": 1, "key": old, "data": {}}
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert old not in hass_storage
    assert await store_key(hass, entry.entry_id) not in hass_storage


async def test_diagnostics_name_neither_the_gateway_nor_the_key(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    from custom_components.bosch_buderus_heating.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    freezer.move_to(START)
    entry, stack = await start(hass, history_cloud(records()))
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        key = await instance_id.async_get(hass)
        digest = gateway_digest(key, GW)
        report = json.dumps(
            await async_get_config_entry_diagnostics(hass, entry), default=str
        )
        energy = json.dumps(
            rrc_energy_statistics.diagnostics(entry.runtime_data.coordinators[0])
        )
        assert "energy_history" in report
        for secret in (key, digest, digest[:12]):
            assert secret not in report
        assert GW not in energy
    finally:
        await stop(hass, entry, stack)


# ------------------------------------- E2: an unreadable plant type for a day
async def test_an_unreadable_plant_type_holds_the_days_and_loses_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    freezer.move_to(START)
    first = date(2026, 10, 15)
    items = [legacy_record(first + timedelta(days=n)) for n in range(4)]
    cloud = history_cloud(items, page=10, heat_source=None)  # 500: missing
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        history = history_of(coordinator)
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.state.reason == "heat_source_type_unavailable"
        assert recorder_loaded.call_count == 0
        report = rrc_energy_statistics.diagnostics(coordinator)
        assert report["reason"] == "heat_source_type_unavailable"
        assert report["heat_source_type_unreadable_seconds"] == 0
        assert report["heat_source_type_failed_cycles"] == 1
        await advance(hass, freezer, CADENCE + timedelta(minutes=1))
        report = rrc_energy_statistics.diagnostics(coordinator)
        assert (
            report["heat_source_type_unreadable_seconds"]
            == (CADENCE + timedelta(minutes=1)).total_seconds()
        )
        assert report["heat_source_type_failed_cycles"] == 2
        assert recorder_loaded.call_count == 0
        # The type answers within the day: every day comes in.
        cloud.tree[HEAT_SOURCE_TYPE_PATH] = parse_resource(
            {"id": HEAT_SOURCE_TYPE_PATH, "type": "stringValue", "value": "Heatpump"}
        )
        await advance(hass, freezer, CADENCE + timedelta(minutes=1))
        assert history.state.reason is None
        assert history.state.heat_source == "electricity"
        report = rrc_energy_statistics.diagnostics(coordinator)
        assert report["heat_source_type_unreadable_seconds"] is None
        heating = await sid(hass, "heat_pump_electricity_heating")
        rows = [
            call.args[2]
            for call in recorder_loaded.call_args_list
            if call.args[1]["statistic_id"] == heating
        ]
        assert [row["sum"] for row in rows[0]] == [3.0, 6.0, 9.0, 12.0]
        assert await sid(hass, "gas_heating") not in {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
    finally:
        await stop(hass, entry, stack)


async def test_the_wait_for_the_plant_type_survives_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_storage,
    recorder_loaded,
) -> None:
    """E2: a restart keeps when the wait began; the day is over 24 hours
    after the first server error, not after the restart."""
    freezer.move_to(START)
    first = date(2026, 10, 15)
    items = [legacy_record(first + timedelta(days=n)) for n in range(4)]
    cloud = history_cloud(items, page=10, heat_source=None)
    entry, stack = await start(hass, cloud)
    await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
    began = dt_util.utcnow()
    history = history_of(entry.runtime_data.coordinators[0])
    assert history.state.heat_source_unreadable_since == began
    await stop(hass, entry, stack)
    stored = hass_storage[await store_key(hass, entry.entry_id)]["data"]
    assert stored["heat_source_unreadable_since"] == began.isoformat()

    freezer.tick(timedelta(hours=22))
    entry, stack = await start(hass, cloud, entry)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        assert history.state.heat_source_unreadable_since == began
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.state.reason == "heat_source_type_unavailable"
        assert recorder_loaded.call_count == 0
        await advance(hass, freezer, CADENCE + timedelta(minutes=1))
        assert dt_util.utcnow() - began >= HEAT_SOURCE_WAIT
        assert history.state.reason == "legacy_keys_ambiguous"
        written = {
            call.args[1]["statistic_id"] for call in recorder_loaded.call_args_list
        }
        assert written == {await sid(hass, "outdoor_temperature")}
    finally:
        await stop(hass, entry, stack)
