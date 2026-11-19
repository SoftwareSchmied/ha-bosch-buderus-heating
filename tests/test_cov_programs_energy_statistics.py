"""Energy history import: failure paths of the cloud source, the stored state
and the cycle (refusals, outages, unreadable or unwritable state)."""

# Fixtures are imported from the module that defines them.
# ruff: noqa: F811

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_energy import HistoryRefused
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    CADENCE,
    START_DELAY,
    EnergyHistory,
    _CloudSource,
    async_gateway_digest,
    async_last_rows,
    history_of,
    track,
)
from tests.test_rrc_energy_statistics import (
    HISTORY,
    START,
    advance,
    day_record,
    history_cloud,
    last_rows,  # noqa: F401 - fixture
    legacy_setup_path,  # noqa: F401 - fixture
    recorder_loaded,  # noqa: F401 - fixture
    records,
    start,
    stop,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# ------------------------------------------------------------ cloud source
class FakeClient:
    def __init__(self, *, value=None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls = 0

    async def get_resource(self, gateway_id, path, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return parse_resource({"id": path, "type": "floatValue", "value": self.value})

    async def get_resource_query(self, gateway_id, path, query, **_kwargs):
        return await self.get_resource(gateway_id, path)


def source(client: FakeClient) -> _CloudSource:
    coordinator = SimpleNamespace(
        client=client, gateway=SimpleNamespace(gateway_id="gw")
    )
    return _CloudSource(coordinator)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "12", None, -1, 2.5])
async def test_unusable_entry_count_is_not_a_count(value):
    assert await source(FakeClient(value=value)).entries() is None


@pytest.mark.parametrize(("value", "count"), [(12, 12), (12.0, 12), (0, 0)])
async def test_whole_entry_count_is_read(value, count):
    assert await source(FakeClient(value=value)).entries() == count


@pytest.mark.parametrize(
    "error", [ResourceNotFound(HISTORY, 404), UnexpectedHttpStatus(400)]
)
@pytest.mark.parametrize("with_query", [True, False])
async def test_refused_form_of_the_history_is_reported_as_refused(error, with_query):
    client = FakeClient(error=error)
    with pytest.raises(HistoryRefused):
        await source(client).page({"entry": 3} if with_query else {})
    assert client.calls == 1


@pytest.mark.parametrize(
    "error", [ResourceForbidden(HISTORY, 403), UnexpectedHttpStatus(502)]
)
async def test_other_history_errors_are_not_taken_for_a_refused_form(error):
    with pytest.raises(type(error)):
        await source(FakeClient(error=error)).page({"entry": 3})


# ---------------------------------------------------- the recorded last rows
class FakeInstance:
    def __init__(self, row) -> None:
        self.row = row

    async def async_add_executor_job(self, func, hass, number, wanted, *_rest):
        return {wanted: [self.row]}


async def test_last_rows_skip_rows_without_a_numeric_start(hass: HomeAssistant):
    zone = dt_util.get_default_time_zone()
    digest = await async_gateway_digest(hass, "gw")
    with patch(
        "homeassistant.helpers.recorder.get_instance",
        return_value=FakeInstance({"start": "2026-10-01", "sum": 5.0}),
    ):
        assert await async_last_rows(hass, digest, zone) == {}
    stamp = datetime(2026, 10, 1, 22, tzinfo=dt_util.UTC).timestamp()
    with patch(
        "homeassistant.helpers.recorder.get_instance",
        return_value=FakeInstance({"start": stamp, "sum": "n/a"}),
    ):
        rows = await async_last_rows(hass, digest, zone)
    # A row whose sum is no number keeps its day, without a sum.
    assert rows
    assert all(total is None for _day, total in rows.values())


# ---------------------------------------------------------- the cycle
def fail_history(cloud, error: Exception) -> None:
    plain_get = cloud.get_resource

    async def get(gateway_id, path, **kwargs):
        if path == HISTORY:
            raise error
        return await plain_get(gateway_id, path, **kwargs)

    async def query(gateway_id, path, query, **_kwargs):
        raise error

    cloud.get_resource = get
    cloud.get_resource_query = query


async def first_cycle(hass, freezer, cloud):
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
    return entry, stack, history_of(entry.runtime_data.coordinators[0])


async def test_forbidden_history_waits_a_day_and_writes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    fail_history(cloud, ResourceForbidden(HISTORY, 403))
    entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert history.state.reason == "http_403"
        assert history.state.next_due >= dt_util.utcnow() + timedelta(hours=23)
        assert recorder_loaded.call_count == 0
        assert history.diagnostics()["reason"] == "http_403"
    finally:
        await stop(hass, entry, stack)


async def test_history_refused_in_both_forms_stops_for_a_week(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    fail_history(cloud, ResourceNotFound(HISTORY, 404))
    entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert history.state.reason == "history_form_switched"
        assert history.state.switched
        await advance(hass, freezer, CADENCE + timedelta(minutes=1))
        assert history.state.reason == "history_unsupported"
        assert history.state.unsupported
        assert history.state.next_due >= dt_util.utcnow() + timedelta(days=6, hours=23)
        assert recorder_loaded.call_count == 0
    finally:
        await stop(hass, entry, stack)


async def test_unexpected_status_keeps_the_regular_cadence(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    fail_history(cloud, UnexpectedHttpStatus(418))
    entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert history.state.reason == "unexpected_http_status"
        assert not history.state.unsupported
        assert history.state.next_due == dt_util.utcnow() + CADENCE
    finally:
        await stop(hass, entry, stack)


async def test_an_unforeseen_error_is_logged_and_retried_at_the_cadence(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    recorder_loaded,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cloud = history_cloud(records())
    fail_history(cloud, RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert history.state.reason == "import_failed"
        assert history.state.next_due == dt_util.utcnow() + CADENCE
        assert "The energy history import failed" in caplog.text
        assert not history._running
        assert recorder_loaded.call_count == 0
    finally:
        await stop(hass, entry, stack)


async def test_a_state_that_cannot_be_stored_does_not_stop_the_import(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    recorder_loaded,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from homeassistant.helpers.storage import Store

    plain_save = Store.async_save

    async def save(self, data):
        if ".rrc_energy." in self.key:
            raise OSError("disk full")
        return await plain_save(self, data)

    cloud = history_cloud(records())
    with patch.object(Store, "async_save", save), caplog.at_level(logging.WARNING):
        entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert "The energy history import could not be stored" in caplog.text
        assert history.state.reason is None
        assert recorder_loaded.call_count > 0
        assert history.state.next_due == dt_util.utcnow() + CADENCE
    finally:
        await stop(hass, entry, stack)


async def test_an_unreadable_stored_state_starts_over(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    recorder_loaded,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from homeassistant.helpers.storage import Store

    plain_load = Store.async_load

    async def load(self):
        if ".rrc_energy." in self.key:
            raise ValueError("corrupt")
        return await plain_load(self)

    cloud = history_cloud(records())
    with patch.object(Store, "async_load", load), caplog.at_level(logging.WARNING):
        entry, stack, history = await first_cycle(hass, freezer, cloud)
    try:
        assert "The stored energy history import could not be read" in caplog.text
        assert history.state.last_day is not None  # imported from scratch
        assert recorder_loaded.call_count > 0
    finally:
        await stop(hass, entry, stack)


async def test_without_a_backoff_a_rate_limit_still_defers_the_import(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    fail_history(cloud, RateLimited(5400))
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        history = history_of(coordinator)
        with patch.object(type(coordinator.client), "backoff", None):
            await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.state.reason == "rate_limited"
        assert history.state.next_due >= dt_util.utcnow() + timedelta(minutes=89)
    finally:
        await stop(hass, entry, stack)


# ------------------------------------------------------------- lifecycle
async def test_a_stopped_import_neither_starts_plans_nor_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        history = history_of(coordinator)
        history.stop()
        history.stop()  # a second stop is harmless
        history.state.next_due = None
        history._schedule(START + timedelta(hours=1))
        assert history.state.next_due is None
        await advance(hass, freezer, timedelta(hours=4))
        assert cloud.requested(HISTORY) == 0
        # Stopped while its stored state loads: nothing is planned afterwards.
        late = EnergyHistory(coordinator)
        late.stop()
        await late.async_start()
        assert late.state.next_due is None
        await late.async_run()
        assert late.waiting == "not_started"
    finally:
        await stop(hass, entry, stack)


async def test_a_new_plan_replaces_the_old_one_and_a_busy_import_ignores_a_due(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        later = START + timedelta(hours=10)
        history._schedule(later)
        assert history.state.next_due == later
        await advance(hass, freezer, timedelta(hours=4))
        # The replaced plan did not fire.
        assert cloud.requested(HISTORY) == 0
        history._running = True
        plan = history._unsub
        history._due(dt_util.utcnow())
        history._unsub = plan  # the plan of the busy import stays in place
        await hass.async_block_till_done()
        assert cloud.requested(HISTORY) == 0
        history._running = False
    finally:
        await stop(hass, entry, stack)


async def test_other_than_a_room_controller_never_reads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        with patch(
            "custom_components.bosch_buderus_heating.rrc_energy_statistics.context_of",
            return_value=SimpleNamespace(rrc_plant=False),
        ):
            await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.waiting == "not_rrc"
        assert cloud.requested(HISTORY) == 0
    finally:
        await stop(hass, entry, stack)


async def test_a_paused_cloud_postpones_the_cycle(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        history = history_of(coordinator)
        with patch.object(coordinator, "cloud_unavailable", return_value=True):
            await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        assert history.waiting == "cloud_unavailable"
        assert cloud.requested(HISTORY) == 0
        assert history.state.next_due > dt_util.utcnow()
    finally:
        await stop(hass, entry, stack)


async def test_tracking_a_coordinator_again_stops_the_previous_import(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud(records())
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        first = history_of(coordinator)
        unloads: list = []
        second = track(unloads.append, coordinator)
        assert second is not first
        assert first._stopped and not second._stopped
        assert history_of(coordinator) is second
        assert unloads == [second.stop]
        second.stop()
    finally:
        await stop(hass, entry, stack)


# ------------------------------------------------------------- the writes
async def test_series_without_rows_add_no_statistic(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, recorder_loaded
) -> None:
    cloud = history_cloud([day_record(date(2026, 10, 1))])
    freezer.move_to(START)
    entry, stack = await start(hass, cloud)
    try:
        history = history_of(entry.runtime_data.coordinators[0])
        history._write({"outdoor_temperature": []}, dt_util.get_default_time_zone())
        assert recorder_loaded.call_count == 0
    finally:
        await stop(hass, entry, stack)
