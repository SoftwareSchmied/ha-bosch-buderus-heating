"""Deterministic scheduling, changing installations, retention and recovery."""

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

from custom_components.bosch_buderus_heating.local_api.catalog import (
    CATALOG,
    make_request,
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_api.models import (
    LocalObservation,
    ReferenceList,
    ResourceReference,
    ScalarValue,
)
from custom_components.bosch_buderus_heating.local_polling import (
    inventory as inventory_module,
)
from custom_components.bosch_buderus_heating.local_polling._data import CLASSES
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness,
    ResourceInventory,
    represented_size,
)
from custom_components.bosch_buderus_heating.local_polling.planner import (
    Lane,
    PollPlanner,
)
from custom_components.bosch_buderus_heating.local_polling.policy import (
    PollClass,
    candidates,
    category,
    period,
    start_offset,
    validate_explicit,
    validate_interval,
)
from custom_components.bosch_buderus_heating.local_polling.storage import (
    ScheduleStorageError,
    checkpoint,
    restore,
)
from custom_components.bosch_buderus_heating.local_probe import TransportFailure
from custom_components.bosch_buderus_heating.local_read import ReadResult, ReadState
from tests.test_local_probe import CONTEXT

PATH = "/heatingCircuits/hc2/currentRoomSetpoint"
REQUEST = request_for_path(PATH)


def success(
    request=REQUEST,
    now=0,
    *,
    value=21,
    data=None,
    document=None,
    context=CONTEXT.identity,
):
    observation = LocalObservation(
        request,
        context,
        datetime(2026, 11, 1, tzinfo=UTC),
        now,
        "floatValue",
        False,
        None,
        data or ScalarValue(value, "C", (), None, None, None, (), (), None, ()),
        document
        if document is not None
        else MappingProxyType({"id": request.path, "value": value}),
    )
    return ReadResult(
        request,
        ReadState.SUCCESS,
        200,
        observation=observation,
        http_requests=1,
        response_bytes=100,
    )


def missing(request, *, status=404, problem="not_found"):
    return ReadResult(request, ReadState.PATH_ERROR, status, problem, http_requests=1)


def signals(inventory, names, now):
    request = request_for_path("/signals")
    inventory.add(request, now)
    return inventory.accept(
        success(
            request,
            now,
            data=ReferenceList(
                tuple(
                    ResourceReference("/signals/" + name, "unused-uri")
                    for name in names
                )
            ),
        ),
        now,
    )


def no_discovery(planner):
    planner.cursor = sum(1 for _ in candidates())
    planner._candidates = iter(())
    planner.exhausted, planner.rescan_at = True, planner.epoch + 86400


def test_complete_classification_and_bounded_domains_have_no_admin_or_history():
    assert set(CLASSES) == {d.template for d in CATALOG}
    assert Counter(CLASSES.values()) == {
        "live": 143,
        "settings": 9,
        "energy": 9,
        "diagnostics": 4,
        "counters": 6,
        "metadata": 13,
        "system_metadata": 8,
        "firmware": 1,
        "static": 3,
        "history": 49,
        "administration": 15,
        "discovery": 1,
    }
    requests = list(candidates())
    assert len(requests) == len({r.target for r in requests}) == 1140
    assert all(
        not r.query and category(r) not in {PollClass.HISTORY, PollClass.ADMINISTRATION}
        for r in requests
    )
    assert not any(r.path.startswith("/signals/") for r in requests)
    paths = [r.path for r in requests]
    for path in (
        PATH,
        "/heatingCircuits/hc2/maxFlowTemp",
        "/heatSources/dhw/cylinderUpperTemp",
        "/solarCircuits/sc1/solarYield",
    ):
        assert path in paths
    assert paths.index(PATH) < 100
    assert 0 <= start_offset("installation") <= 5
    assert start_offset("installation") == start_offset("installation")
    assert start_offset("other") != start_offset("installation")


@pytest.mark.parametrize("interval", [60, 300, 900])
def test_class_intervals_and_expiry_remain_configured_not_backlog_based(interval):
    inventory = ResourceInventory(CONTEXT.identity, interval)
    record = inventory.add(REQUEST, 100)
    inventory.accept(success(now=100), 100)
    assert period(REQUEST, interval) == interval
    assert period(request_for_path("/gateway/brand"), interval) == 86400
    late = inventory_module.late_after(interval)
    for elapsed, freshness, available in [
        (0, Freshness.FRESH, True),
        # A grace of a tenth of the interval, at least 10 s (PLAN-3).
        (interval, Freshness.FRESH, True),
        (late, Freshness.LATE, True),
        (3 * interval, Freshness.EXPIRED, False),
    ]:
        view = inventory.view(record, 100 + elapsed, source_ready=True)
        assert (view.freshness, view.available, view.age_seconds) == (
            freshness,
            available,
            elapsed,
        )
    assert inventory.next_expiry(100) == 100 + late
    assert inventory.next_expiry(100 + late) == 100 + 3 * interval
    assert inventory.next_expiry(100 + 3 * interval) is None
    assert record.observation.observed_monotonic == 100


@pytest.mark.parametrize("value", [True, 0, 30, 61, 960, "60", 60.0])
def test_invalid_live_interval_rejected(value):
    with pytest.raises(ValueError):
        validate_interval(value)


@pytest.mark.parametrize("status", [403, 404, 406, 200])
def test_known_path_error_immediate_unavailable_then_same_record_recovers(status):
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    inventory.accept(success(), 0)
    inventory.accept(missing(REQUEST, status=status, problem="invalid_response"), 30)
    assert not inventory.view(record, 30, source_ready=True).available
    # Withdrawn: asked again soon (PLAN-1), never later than its period.
    assert record.observation.observed_monotonic == 0 and record.due == 60
    inventory.accept(success(now=60, value=22), 60)
    assert inventory.records[REQUEST.target] is record
    assert inventory.view(record, 60, source_ready=True).available
    assert record.actual_interval == 60 and record.retries == 0


def test_timeout_does_not_refresh_or_prematurely_invalidate_known_value():
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    inventory.accept(success(), 0)
    inventory.accept(
        missing(REQUEST, status=None, problem=TransportFailure.TIMEOUT), 60
    )
    assert inventory.view(record, 179, source_ready=True).available
    assert not inventory.view(record, 180, source_ready=True).available
    assert record.observation.observed_monotonic == 0


@pytest.mark.parametrize(
    "status,problem,delays",
    [
        (403, "forbidden", [3600, 21600, 604800, 604800]),
        (404, "not_found", [3600, 21600, 604800, 604800]),
        (200, "invalid_response", [900, 3600, 86400, 86400]),
        (None, TransportFailure.TIMEOUT, [300, 900, 3600, 86400]),
    ],
)
def test_unconfirmed_path_backoff(status, problem, delays):
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    now = 0
    for delay in delays:
        inventory.accept(missing(REQUEST, status=status, problem=problem), now)
        assert record.due == now + delay and not record.confirmed
        now = record.due


def test_signal_inventory_is_atomic_bounded_and_requires_two_missing_lists():
    inventory = ResourceInventory(CONTEXT.identity)
    assert signals(inventory, ["first", "second"], 0)
    request = make_request(
        "/signals/{signalId}",
        parameters={"signalId": "first"},
        signal_ids=inventory.signal_ids,
    )
    record = inventory.records[request.target]
    inventory.accept(success(request, 1), 1)
    assert not signals(inventory, [str(i) for i in range(513)], 2)
    assert inventory.signal_ids == frozenset({"first", "second"})
    assert record.signal_misses == 0 and record.active
    assert signals(inventory, ["second"], 3) and record.active
    assert signals(inventory, ["second"], 4) and not record.active
    assert not inventory.view(record, 4, source_ready=True).available
    assert signals(inventory, ["first", "second"], 5) and record.active
    assert record.invalid and not inventory.view(record, 5, source_ready=True).available
    inventory.accept(success(request, 6), 6)
    assert inventory.view(record, 6, source_ready=True).available
    assert len(inventory.records) == 3


def test_invalid_signal_list_keeps_previous_and_prunes_only_old_search_records():
    inventory = ResourceInventory(CONTEXT.identity)
    signals(inventory, ["gone"], 0)
    listing = request_for_path("/signals")
    invalid = ReferenceList((ResourceReference("/not/a/signal", "unused"),))
    assert not inventory.accept(success(listing, 1, data=invalid), 1)
    assert inventory.signal_ids == frozenset({"gone"})
    # P41: an empty list after known signals is no answer, not a removal.
    signals(inventory, [], 2)
    assert inventory.signal_problem == "empty_local_signal_list"
    assert all(record.active for record in inventory.records.values())
    signals(inventory, ["other"], 3)
    signals(inventory, ["other"], 4)
    inventory.prune(86399)
    assert len(inventory.records) == 3
    inventory.prune(86400)
    assert len(inventory.records) == 2


def test_memory_and_record_limits_do_not_turn_discarded_data_fresh(monkeypatch):
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    inventory.accept(success(), 0)
    original = record.observation
    monkeypatch.setattr(
        inventory_module, "MAX_RETAINED_BYTES", inventory.retained_bytes
    )
    assert not inventory.accept(success(now=60, document={"text": "x" * 1024}), 60)
    assert record.observation is original and record.problem == "local_memory_limit"
    assert not inventory.view(record, 60, source_ready=True).available
    monkeypatch.setattr(inventory_module, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="resource_limit"):
        inventory.add(request_for_path("/gateway/brand"), 0)
    assert inventory.add(REQUEST, 1) is record
    assert represented_size({"a": (Decimal("1.25"), True, None, "ä")})[0] > 0


@pytest.mark.parametrize("kind", ["context", "future", "request", "absent"])
def test_invalid_observations_never_replace_current_values(kind):
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    result = success()
    if kind == "context":
        result = success(context=replace(CONTEXT.identity, generation=99))
    if kind == "future":
        result = success(now=1)
    if kind == "request":
        result = replace(
            result,
            observation=replace(
                result.observation, request=request_for_path("/gateway/brand")
            ),
        )
    if kind == "absent":
        result = replace(result, observation=None)
    with pytest.raises(ValueError):
        inventory.accept(result, 0)
    assert record.observation is None


def test_planner_caps_parallelism_preserves_spacing_and_minute_reserve():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    # T1/T2: 200 ms between starts, the final 3 s of each section reserved.
    a = planner.pick(0)
    assert a is not None and planner.pick(0.199) is None
    b = planner.pick(0.2)
    assert b is not None and planner.pick(0.5) is None
    planner.finish(a, missing(a.request), 0.5)
    c = planner.pick(0.5)
    assert c is not None
    planner.finish(b, missing(b.request), 1)
    planner.finish(c, missing(c.request), 1)
    assert planner.pick(57) is None and planner.next_wake(57) == 60
    assert planner.pick(60) is not None


def test_discovery_reaches_late_hc2_solar_and_every_fixed_candidate_despite_errors():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    starts = []
    now = 0
    for _ in range(15000):
        work = planner.pick(now)
        if work:
            starts.append((now, work.request.path))
            planner.finish(work, missing(work.request), now)
            now += 0.25
        else:
            if planner.exhausted:
                break
            now = max(now + 0.25, planner.next_wake(now))
    assert planner.exhausted
    assert {path for _, path in starts} == {r.path for r in candidates()}
    assert len(planner.inventory.records) == 1140
    assert all(not r.confirmed for r in planner.inventory.records.values())
    assert any(path == "/heatingCircuits/hc2/maxFlowTemp" for _, path in starts)
    batches = []
    for stamp, _ in starts:
        if not batches or stamp - batches[-1][-1] >= 300:
            batches.append([])
        batches[-1].append(stamp)
    assert all(len(batch) <= 64 for batch in batches)


def test_overloaded_regular_population_is_fair_and_old_values_expire():
    inventory = ResourceInventory(CONTEXT.identity)
    selected = [r for r in candidates() if category(r) is PollClass.LIVE][:300]
    planner = PollPlanner(inventory, 0)
    no_discovery(planner)
    for request in selected:
        inventory.add(request, 0)
        inventory.accept(success(request, 0), 0)
    seen = Counter()
    # Three-second operations saturate two slots. Early IDs cannot hog the budget.
    pending = []
    for tick in range(4800):
        now = tick * 0.25
        for end, work in tuple(pending):
            if end <= now:
                pending.remove((end, work))
                planner.finish(work, success(work.request, now), now)
        work = planner.pick(now)
        if work:
            pending.append((now + 3, work))
            seen[work.request.target] += 1
    assert len(seen) == len(selected)
    assert max(seen.values()) - min(seen.values()) <= 1
    assert any(
        inventory.view(r, 1200, source_ready=True).freshness is Freshness.EXPIRED
        for r in inventory.records.values()
    )
    assert len(planner.inflight) <= 2 and len(inventory.records) == 300


def history(rate="all", days=1):
    definition = next(
        d
        for d in CATALOG
        if d.template.startswith("/recordings/") and "{" not in d.template
    )
    return make_request(
        definition.template,
        query={
            "sampleRate": rate,
            "startDate": "2026-01-01",
            "endDate": (date(2026, 1, 1) + timedelta(days=days - 1)).isoformat(),
        },
    )


@pytest.mark.parametrize(
    "rate,limit", [("all", 1), ("PT1H", 7), ("P1D", 31), ("P1M", 366)]
)
def test_explicit_history_requires_bounded_window(rate, limit):
    validate_explicit((history(rate, limit),))
    with pytest.raises(ValueError, match="window_limit"):
        validate_explicit((history(rate, limit + 1),))
    request = history()
    with pytest.raises(ValueError, match="window_required"):
        validate_explicit(
            (make_request(request.definition.template, query={"sampleRate": "all"}),)
        )


def test_explicit_sharing_preserves_deadline_and_queue_bound():
    inventory = ResourceInventory(CONTEXT.identity)
    planner = PollPlanner(inventory, 0)
    no_discovery(planner)
    inventory.add(REQUEST, 0)
    inventory.accept(success(), 0)
    operation = planner.explicit((REQUEST,), 0)
    assert planner.explicit((REQUEST,), 50) is operation and operation.deadline == 120
    work = planner.pick(60)
    planner.finish(work, success(now=60), 60)
    assert (
        operation.done
        and operation.results[REQUEST.target].observation.observed_monotonic == 60
    )
    assert not planner.operations
    items = list(candidates())[:9]
    for request in items[:8]:
        planner.explicit((request,), 61)
    with pytest.raises(ValueError, match="queue_full"):
        planner.explicit((items[8],), 61)
    planner.expire_operations(181)
    assert not planner.operations
    with pytest.raises(ValueError):
        planner.explicit((REQUEST, REQUEST), 200)


def test_history_not_periodic_cancellation_and_response_byte_limit():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    request = history()
    operation = planner.explicit((request,), 0)
    work = planner.pick(0)
    assert work.lane is Lane.EXPLICIT
    result = replace(success(request), response_bytes=1024 * 1024 + 1)
    planner.finish(work, result, 0)
    assert operation.done and operation.problem == "local_explicit_size_limit"
    assert not planner.inventory.records
    other = planner.explicit((request,), 1)
    planner.cancel(other)
    assert other.done and other.problem == "cancelled"
    assert planner.pick(2) is None


def test_deferral_is_not_discovery_attempt_or_loss_of_candidate():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    work = planner.pick(0)
    planner.finish(
        work, ReadResult(work.request, ReadState.DEFERRED, retry_after_seconds=7200), 0
    )
    assert planner.slice_attempts == 0 and planner.attempts[Lane.DISCOVERY] == 0
    assert planner.pick(7199) is None
    next_work = planner.pick(7200)
    assert next_work.request == work.request


def test_checkpoint_keeps_progress_and_negative_pauses_but_no_measurements():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    for number in range(10):
        work = planner.pick(number)
        planner.finish(work, missing(work.request), number)
    record = planner.inventory.add(REQUEST, 10)
    planner.inventory.accept(success(now=10, value=123456789), 10)
    saved = checkpoint(planner, 20)
    assert "123456789" not in json.dumps(saved) and "observation" not in json.dumps(
        saved
    )
    new = PollPlanner(ResourceInventory(CONTEXT.identity), 1000)
    restore(new, saved, 1000)
    assert new.cursor == planner.cursor and new.wheel == planner.wheel
    restored = new.inventory.records[REQUEST.target]
    assert restored.confirmed and restored.observation is None and restored.due == 1000
    original = next(r for r in planner.inventory.records.values() if not r.confirmed)
    assert (
        new.inventory.records[original.request.target].due == 1000 + original.due - 20
    )
    assert record.observation.data.value == 123456789


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("cursor", 999999),
        ("slice_attempts", 65),
        ("discovery_pause_remaining", 301),
        ("rescan_remaining", float("nan")),
        ("wheel", 10),
        ("signals", ["duplicate", "duplicate"]),
        ("records", [{}]),
    ],
)
def test_invalid_checkpoint_does_not_partially_replace_inventory(field, value):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    planner.inventory.add(REQUEST, 0)
    saved = checkpoint(planner, 0)
    saved[field] = value
    with pytest.raises(ScheduleStorageError):
        restore(planner, saved, 100)
    assert list(planner.inventory.records) == [REQUEST.target]


def test_saturated_lanes_receive_eighty_ten_ten_without_starvation():
    inventory = ResourceInventory(CONTEXT.identity)
    for request in [r for r in candidates() if category(r) is PollClass.LIVE][:300]:
        record = inventory.add(request, 0)
        inventory.accept(success(request), 0)
        record.due = 0
    planner = PollPlanner(inventory, 0)
    counts = Counter()
    for index in range(100):
        now = index * 0.25
        planner.explicit((history(),), now)
        work = planner.pick(now)
        assert work is not None
        counts[work.lane] += 1
        planner.finish(work, success(work.request, now), now)
    assert counts == {Lane.REGULAR: 80, Lane.DISCOVERY: 10, Lane.EXPLICIT: 10}
    assert len(planner.operations) <= 1 and not planner.inflight


def test_growing_discovery_population_is_promoted_without_resetting_search():
    inventory = ResourceInventory(CONTEXT.identity)
    planner = PollPlanner(inventory, 0)
    now = 0
    first_seen = {}
    revisited = set()
    for _ in range(5000):
        work = planner.pick(now)
        if work is None:
            now = max(now + 0.25, planner.next_wake(now))
            continue
        if work.request.target in first_seen:
            revisited.add(work.request.target)
        first_seen.setdefault(work.request.target, now)
        result = success(
            work.request,
            now,
            data=ReferenceList(()) if work.request.path == "/signals" else None,
        )
        planner.finish(work, result, now)
        now += 0.25
        if len(first_seen) >= 160 and len(revisited) >= 30:
            break
    assert len(first_seen) >= 160 and len(revisited) >= 30
    assert all(r.confirmed for r in inventory.records.values())
    assert planner.cursor >= 160
    assert len(inventory.records) == len(first_seen)


def test_signal_limit_transaction_and_fresh_directory_after_restart(monkeypatch):
    inventory = ResourceInventory(CONTEXT.identity)
    assert signals(inventory, [str(i) for i in range(512)], 0)
    planner = PollPlanner(inventory, 0)
    saved = checkpoint(planner, 0)
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 100)
    restore(restored, saved, 100)
    signal_records = [
        r
        for r in restored.inventory.records.values()
        if r.request.path.startswith("/signals/")
    ]
    assert len(signal_records) == 512 and all(not r.active for r in signal_records)
    assert signals(restored.inventory, ["0"], 101)
    assert sum(r.active for r in signal_records) == 1
    original = restored.inventory.signal_ids
    monkeypatch.setattr(
        inventory_module, "MAX_RECORDS", len(restored.inventory.records)
    )
    assert not signals(restored.inventory, ["new"], 102)
    assert restored.inventory.signal_ids == original
    assert restored.inventory.signal_problem == "local_resource_limit"


def test_firmware_reconsideration_preserves_cursor_and_selected_negative_scope():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    works = []
    for index in range(3):
        work = planner.pick(index)
        planner.finish(work, missing(work.request), index)
        works.append(work)
    planner.reconsider(100, frozenset({works[0].request.definition.template}))
    assert planner.cursor == 3
    assert planner.inventory.records[works[0].request.target].due == 100
    assert planner.inventory.records[works[1].request.target].due > 100
    planner.reconsider(100)
    assert all(r.due <= 100.5 for r in planner.inventory.records.values())


def test_catalog_revision_revisits_candidates_and_drops_removed_cached_paths():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    work = planner.pick(0)
    planner.finish(work, missing(work.request), 0)
    saved = checkpoint(planner, 1)
    saved["revision"] = "f" * 64
    saved["records"].append({**saved["records"][0], "path": "/retired/metric"})
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 100)
    restore(restored, saved, 100)
    assert restored.cursor == 0 and not restored.exhausted
    assert len(restored.inventory.records) == 1
    assert next(iter(restored.inventory.records.values())).due == 3699
    saved["records"][-1]["path"] = "https://untrusted.example/token"
    with pytest.raises(ScheduleStorageError):
        restore(restored, saved, 100)


@pytest.mark.parametrize(
    "change",
    ["duplicate", "bad_schema", "non_boolean", "negative", "query", "false_end"],
)
def test_bad_record_or_inconsistent_completion_rejected_atomically(change):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    planner.inventory.add(REQUEST, 0)
    saved = checkpoint(planner, 0)
    row = saved["records"][0]
    if change == "duplicate":
        saved["records"].append(dict(row))
    if change == "bad_schema":
        row["schema"] = 1
    if change == "non_boolean":
        row["confirmed"] = 1
    if change == "negative":
        row["remaining"] = -1
    if change == "query":
        row["path"] += "?x=1"
    if change == "false_end":
        saved["exhausted"] = True
    with pytest.raises(ScheduleStorageError):
        restore(planner, saved, 100)
    assert planner.inventory.records[REQUEST.target].created == 0


def memory_profile():
    import tracemalloc

    from custom_components.bosch_buderus_heating.local_api.catalog import (
        response_schema,
    )
    from custom_components.bosch_buderus_heating.local_api.decoder import (
        decode_resource,
    )
    from custom_components.bosch_buderus_heating.local_api.schema import matches

    requests = [
        r for r in candidates() if "StringValue" in r.definition.response_schemas
    ]
    inventory = ResourceInventory(CONTEXT.identity)
    tracemalloc.start()
    accepted = rejected = 0
    try:
        for index, request in enumerate(requests):
            document = {
                "id": request.path,
                "type": "stringValue",
                "writeable": 0,
                "value": str(index) + "x" * 219990,
            }
            if not matches(document, response_schema(request)):
                continue
            body = json.dumps(document).encode()
            observation = decode_resource(
                request,
                body,
                context=CONTEXT.identity,
                observed_at=datetime.now(UTC),
                observed_monotonic=index,
            )
            inventory.add(request, index)
            result = ReadResult(
                request,
                ReadState.SUCCESS,
                200,
                observation=observation,
                response_bytes=len(body),
            )
            if inventory.accept(result, index):
                accepted += 1
            else:
                rejected += 1
            if accepted + rejected == 48:
                break
        _, peak = tracemalloc.get_traced_memory()
        return {
            "accepted": accepted,
            "rejected": rejected,
            "retained_bytes": inventory.retained_bytes,
            "retained_nodes": inventory.retained_nodes,
            "peak_traced_bytes": peak,
        }
    finally:
        tracemalloc.stop()


def test_retention_limit_with_large_decoded_responses_has_bounded_measured_peak():
    profile = memory_profile()
    assert profile["accepted"] > 0 and profile["rejected"] > 0
    assert profile["accepted"] + profile["rejected"] == 48
    assert profile["retained_bytes"] <= 8 * 1024 * 1024
    assert profile["retained_nodes"] <= 65536
    assert profile["peak_traced_bytes"] < 64 * 1024 * 1024


def test_node_limit_preserves_old_observation_without_false_freshness(monkeypatch):
    inventory = ResourceInventory(CONTEXT.identity)
    record = inventory.add(REQUEST, 0)
    inventory.accept(success(), 0)
    original = record.observation
    monkeypatch.setattr(inventory_module, "MAX_RETAINED_NODES", 4)
    assert not inventory.accept(
        success(now=1, document={"values": tuple(range(10))}), 1
    )
    assert record.observation is original and record.invalid
    assert inventory.retained_nodes <= 4


def test_slice_time_limit_and_explicit_deadline_reserve_are_independent():
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    work = planner.pick(0)
    planner.finish(work, missing(work.request), 0)
    assert planner.pick(600) is None
    assert planner.discovery_pause_until == 900
    assert planner.pick(900) is not None
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    no_discovery(planner)
    operation = planner.explicit((history(),), 0)
    assert planner._explicit(115) is None
    planner.expire_operations(120)
    assert operation.done and operation.problem == "deadline"


# ------------------------------------------- reading plan (O42, O43, O47)


def signal_request(name):
    return make_request(
        "/signals/{signalId}", parameters={"signalId": name}, signal_ids=(name,)
    )


def test_fixed_values_are_read_rarely_and_serial_numbers_never():
    hourly = request_for_path("/gateway/tzInfo/timeZone")
    assert category(hourly) is PollClass.SYSTEM_METADATA
    for path in (
        "/gateway/dataProcessing/status",
        "/gateway/tzInfo/timeZone",
        "/system/basicInfo",
        "/system/bus",
        "/system/iSRC/installationStatus",
        "/system/iSRC/supportStatus",
        "/system/variableTariff/supportStatus",
        "/system/variableTariff/tariffId",
    ):
        assert period(request_for_path(path), 60) == 3600, path
    # Device and zone values keep 15 min: a low battery or a lost radio
    # pairing shows up as before, also without a cloud.
    for path in (
        "/devices/device1/battery",
        "/devices/device1/rfPairedStatus",
        "/devices/device1/name",
        "/devices/device1/assignedHC",
        "/devices/device1/zoneId",
        "/devices/device1/type",
        "/devices/device1/versionFirmware",
        "/devices/uhc/assignedTo",
        "/zones/zone1/name",
        "/zones/zone1/icon",
        "/zones/configuration",
        "/heatSources/hs1/heatPumpType",
    ):
        request = request_for_path(path)
        assert category(request) is PollClass.METADATA, path
        assert period(request, 60) == 900, path
    firmware = request_for_path("/gateway/versionFirmware")
    assert category(firmware) is PollClass.FIRMWARE and period(firmware, 60) == 900
    assert period(request_for_path("/gateway/brand"), 60) == 86400
    for part in ("Day", "Month", "Year"):
        request = signal_request(f"SC.InstallationDate.{part}")
        assert category(request) is PollClass.STATIC
        assert period(request, 60) == 86400
    other = signal_request("SC.RTC.RelativeTimer")
    assert category(other) is PollClass.LIVE and period(other, 60) == 60
    # A paired cloud reading still keeps the local reading at its pace.
    assert period(hourly, 60, 300) == 300
    # O47: device serial numbers are neither searched nor read.
    assert not any(r.path.endswith("/sgtin") for r in candidates())
    with pytest.raises(ValueError, match="local_periodic_request_required"):
        ResourceInventory(CONTEXT.identity).add(
            request_for_path("/devices/device1/sgtin"), 0
        )


def stored_negative(problem="not_found", status=404, rounds=3):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    record = planner.inventory.add(REQUEST, 0)
    now = 0
    for _ in range(rounds):
        planner.inventory.accept(missing(REQUEST, status=status, problem=problem), now)
        now = record.due
    return planner, record, now


def test_a_path_not_found_three_times_is_asked_again_weekly():
    planner, record, now = stored_negative()
    assert record.failures == 3 and record.problem == "not_found"
    assert record.due - (3600 + 21600) == 604800
    saved = checkpoint(planner, 3600 + 21600)
    row = saved["records"][0]
    assert row["problem"] == "not_found" and row["remaining"] == 604800
    # A firmware change and a new search ask at once again.
    planner.reconsider(now)
    assert record.due == now and record.failures == 0
    planner, record, _ = stored_negative()
    planner.restart_search(30000)
    assert record.due == 30000 and record.failures == 0


@pytest.mark.parametrize("schema_changed", [False, True])
def test_restored_negatives_keep_reason_failures_and_pause(schema_changed):
    planner, _, _ = stored_negative()
    saved = checkpoint(planner, 3600 + 21600)
    if schema_changed:
        saved["revision"] = "f" * 64
        saved["records"][0]["schema"] = "0" * 64
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 1000)
    restore(restored, saved, 1000)
    record = restored.inventory.records[REQUEST.target]
    assert (record.problem, record.failures, record.due) == (
        "not_found",
        3,
        1000 + 604800,
    )
    view = restored.inventory.view(record, 1000, source_ready=True)
    assert view.problem == "not_found" and not view.available


def test_a_changed_schema_still_asks_soon_after_other_failures():
    planner, _, _ = stored_negative(TransportFailure.TIMEOUT, None)
    saved = checkpoint(planner, 0)
    saved["records"][0]["schema"] = "0" * 64
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 1000)
    restore(restored, saved, 1000)
    record = restored.inventory.records[REQUEST.target]
    assert record.due == 1000 and record.problem == TransportFailure.TIMEOUT


def test_a_changed_access_asks_restored_negatives_at_once():
    planner, _, _ = stored_negative()
    planner.access_key = "1:1"
    saved = checkpoint(planner, 0)
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 1000)
    restored.access_key = "1:2"
    restore(restored, saved, 1000)
    record = restored.inventory.records[REQUEST.target]
    assert record.due == 1000 and record.failures == 0


def test_older_schedules_and_dropped_serial_numbers_still_load():
    planner, _, _ = stored_negative()
    saved = checkpoint(planner, 0)
    del saved["records"][0]["problem"]  # saved before O94
    restored = PollPlanner(ResourceInventory(CONTEXT.identity), 1000)
    restore(restored, saved, 1000)
    assert restored.inventory.records[REQUEST.target].problem is None
    # A record of a path no longer read is dropped with the new revision.
    saved["revision"] = "f" * 64
    saved["records"].append({**saved["records"][0], "path": "/devices/device1/sgtin"})
    restore(restored, saved, 1000)
    assert list(restored.inventory.records) == [REQUEST.target]
    # ... but never within the same revision.
    from custom_components.bosch_buderus_heating.local_polling.policy import REVISION

    saved["revision"] = REVISION
    with pytest.raises(ScheduleStorageError):
        restore(restored, saved, 1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("problem", "Not Found"),
        ("problem", 404),
        ("problem", "x" * 65),
        ("remaining", 604801),
        ("other", 1),
    ],
)
def test_invalid_stored_records_are_rejected(field, value):
    planner, _, _ = stored_negative()
    saved = checkpoint(planner, 0)
    saved["records"][0][field] = value
    with pytest.raises(ScheduleStorageError):
        restore(PollPlanner(ResourceInventory(CONTEXT.identity), 0), saved, 0)


# ------------------------------------------------- schema keys (O60, O61)


def test_schema_keys_stay_byte_identical_and_are_worked_out_once(monkeypatch):
    import hashlib

    from custom_components.bosch_buderus_heating.local_api import catalog
    from custom_components.bosch_buderus_heating.local_polling import storage

    # Keys of earlier releases: stored schedules keep matching.
    assert (
        storage.schema_key(REQUEST)
        == "d38338bb0ed24522235538dd0e9bad6d8cfb2a6d9df8a2cb34b5fc1908147991"
    )
    assert (
        storage.schema_key(signal_request("SC.InstallationDate.Day"))
        == "269cdf738e77f7e9a0a6f8dda0bf0114e58c2de2908648385708b557f23fb9ea"
    )
    assert (
        storage.schema_key(request_for_path("/heatSources/hs1/workingTime"))
        == "5827c8fb5f01acdd045d7449df27c6cd69e940380537fa51cc56bd1f42cf9cf2"
    )
    for request in (*candidates(), signal_request("any")):
        assert (
            storage.schema_key(request)
            == hashlib.sha256(
                repr(
                    (
                        request.definition,
                        catalog.OPERATIONS[request.definition.template]["response"],
                    )
                ).encode()
            ).hexdigest()
        )
    # A restore parses each stored path once.
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    for request in list(candidates())[:50]:
        planner.inventory.add(request, 0)
    saved = checkpoint(planner, 0)
    parsed = []
    original = storage.request_for_path

    def counted(path, **kwargs):
        parsed.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(storage, "request_for_path", counted)
    monkeypatch.setattr(catalog, "request_for_path", counted)
    catalog._cached_schema.cache_clear()
    restore(PollPlanner(ResourceInventory(CONTEXT.identity), 0), saved, 0)
    # Once per stored path; the schema keys of known templates are kept.
    assert len(parsed) == len(saved["records"]) == 50
