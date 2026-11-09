"""G3/O51: readings nobody shows are read less often, never left out."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import entity_use as module
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.entity_use import (
    entity_paths,
    entity_use,
)
from custom_components.bosch_buderus_heating.local_api.catalog import (
    make_request,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_read import ReadResult, ReadState
from custom_components.bosch_buderus_heating.local_selection import (
    UNUSED_SIGNAL_PERIOD,
    signal_slows_when_unused,
)
from custom_components.bosch_buderus_heating.managed_local import _hourly_reason
from custom_components.bosch_buderus_heating.platform_scope import setup_platform
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    RequestMetrics,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    slows_when_unused,
)
from tests.ha_test_helpers import MockConfigEntry, async_fire_time_changed
from tests.test_local_polling import success
from tests.test_local_probe import CONTEXT
from tests.test_managed_local import prepared as prepared
from tests.test_managed_local import ready

GATEWAY = "000123"
FLAG = "/signals/SRC.Some.Flag"


def entity(unique_id, path=None, *, gateway=GATEWAY, description=None, **extra):
    return SimpleNamespace(
        unique_id=unique_id,
        coordinator=SimpleNamespace(gateway=SimpleNamespace(gateway_id=gateway)),
        entity_description=description,
        **({"resource_path": path} if path is not None else {}),
        **extra,
    )


def register(hass, entry, domain, unique_id, *, disabled=True):
    return (
        er.async_get(hass)
        .async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None,
        )
        .entity_id
    )


async def later(hass):
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=module.REFRESH_DELAY + 1)
    )
    await hass.async_block_till_done()


@pytest.fixture
def entry(hass):
    value = MockConfigEntry(domain=DOMAIN)
    value.add_to_hass(hass)
    return value


def test_entity_paths_include_combined_and_extra_readings():
    description = SimpleNamespace(
        resource_path="/a/b", secondary_resource_path="/a/c", key="x"
    )
    item = entity("u", "/a/d", description=description, extra_resource_paths=("/e",))
    assert entity_paths(item) == {"/a/b", "/a/c", "/a/d", "/e"}
    assert entity_paths(SimpleNamespace(entity_description=None)) == frozenset()


async def test_only_paths_whose_entities_are_all_disabled_count(hass, entry):
    use = entity_use(hass, entry)
    assert entity_use(hass, entry) is use
    calls = []
    use.add_listener(lambda: calls.append(use.disabled_paths(GATEWAY)))
    # Two entities of one path: one disabled, one enabled.
    use.note("sensor", entity(f"{GATEWAY}:a", "/shared"))
    use.note("switch", entity(f"{GATEWAY}:b", "/shared"))
    use.note("sensor", entity(f"{GATEWAY}:c", "/alone"))
    # Noted, but not registered (yet): counts as used.
    use.note("sensor", entity(f"{GATEWAY}:d", "/pending"))
    # Without a path or a gateway nothing is noted.
    use.note("sensor", entity(f"{GATEWAY}:e"))
    use.note("sensor", SimpleNamespace(unique_id="x", resource_path="/x"))
    register(hass, entry, "sensor", f"{GATEWAY}:a")
    register(hass, entry, "switch", f"{GATEWAY}:b", disabled=False)
    alone = register(hass, entry, "sensor", f"{GATEWAY}:c")
    assert use.disabled_paths(GATEWAY) == frozenset()  # not worked out yet
    await later(hass)
    assert use.disabled_paths(GATEWAY) == {"/alone"}
    assert use.disabled_paths("other") == frozenset()
    assert calls == [{"/alone"}]
    unchanged = use.disabled_paths(GATEWAY)
    # A new name changes nothing; the answer stays the same object.
    er.async_get(hass).async_update_entity(alone, name="Renamed")
    await later(hass)
    assert use.disabled_paths(GATEWAY) is unchanged and len(calls) == 1
    # Enabling the entity makes its path used again, and says so.
    er.async_get(hass).async_update_entity(alone, disabled_by=None)
    await later(hass)
    assert use.disabled_paths(GATEWAY) == frozenset()
    assert calls[-1] == frozenset()


async def test_listener_failure_and_unload_end_cleanly(hass, entry):
    use = entity_use(hass, entry)
    seen = []

    def broken():
        raise RuntimeError("synthetic")

    use.add_listener(broken)
    remove = use.add_listener(lambda: seen.append(True))
    use.note("sensor", entity(f"{GATEWAY}:a", "/a"))
    register(hass, entry, "sensor", f"{GATEWAY}:a")
    await later(hass)
    assert seen == [True]  # a failing listener does not stop the others
    remove()
    remove()
    # Unloading the entry ends the registry listener and the tracker.
    use.note("sensor", entity(f"{GATEWAY}:b", "/b"))
    for callback in entry._on_unload or ():
        callback()
    assert use._closed and use._pending is None
    use.refresh()
    use.note("sensor", entity(f"{GATEWAY}:c", "/c"))
    assert entity_use(hass, entry) is not use


async def test_a_failing_listener_is_reported_once_without_identifiers(
    hass, entry, caplog
):
    use = entity_use(hass, entry)

    def broken():
        raise RuntimeError(f"synthetic {GATEWAY} /signals/SRC.Some.Flag")

    use.add_listener(broken)
    for name in ("a", "b", "c"):
        use.note("sensor", entity(f"{GATEWAY}:{name}", f"/{name}"))
        register(hass, entry, "sensor", f"{GATEWAY}:{name}")
        caplog.clear()
        with caplog.at_level("DEBUG", logger=module.__name__):
            await later(hass)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        debug = [r for r in caplog.records if r.levelname == "DEBUG"]
        # The first failure is a warning, the later ones are debug only.
        assert len(warnings) == (1 if name == "a" else 0), name
        assert debug, name
        for record in warnings:
            text = record.getMessage()
            assert "RuntimeError" in text
            assert GATEWAY not in text and "/signals" not in text
            assert record.exc_info is None


async def test_legacy_platform_notes_every_entity_before_adding(hass, entry):
    coordinator = SimpleNamespace(entity_use=entity_use(hass, entry))
    entry.runtime_data = SimpleNamespace(
        coordinators=(coordinator,), client=SimpleNamespace(metrics=None)
    )
    added = []
    item = entity(f"{GATEWAY}:a", "/a")

    def build(scope, add):
        add(iter([item]))
        add([], True)

    setup_platform(
        entry,
        "switch",
        lambda entities, *args, **kwargs: added.append((list(entities), args)),
        build,
    )
    assert added == [([item], ()), ([], (True,))]
    register(hass, entry, "switch", f"{GATEWAY}:a")
    await later(hass)
    assert entity_use(hass, entry).disabled_paths(GATEWAY) == {"/a"}


# ----------------------------------------------------------------- O51


def test_control_exemptions_keep_climate_hot_water_holiday_and_calendar():
    assert slows_when_unused("/heatingCircuits/hc1/heatCurveMax")
    assert slows_when_unused("/ventilation/zone1/operationMode")
    for path in (
        "/holidayMode/activated",
        "/holidayMode/list",
        "/system/holidayModes/hm1/startStop",
        "/dhwCircuits/dhw1/temperatureLevels/high",
        "/heatingCircuits/hc1/operationMode",
        "/heatingCircuits/hc1/manualRoomSetpoint",
        "/heatingCircuits/hc2/currentSuWiMode",
        "/system/seasonOptimizer/mode",
        "/system/globalSeasonOptimizer/currentMode",
    ):
        assert not slows_when_unused(path), path


def _control(path):
    return Resource(
        path=path, value=1.0, has_value=True, metadata=ResourceMetadata(writable=True)
    )


async def test_disabled_control_paths_poll_every_15_minutes(hass, entry):
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway(GATEWAY), entry
    )
    curve, mode, hot_water, holiday = (
        "/heatingCircuits/hc1/heatCurveMax",
        "/heatingCircuits/hc1/operationMode",
        "/dhwCircuits/dhw1/temperatureLevels/high",
        "/holidayMode/activated",
    )
    coordinator.resources = {p: _control(p) for p in (curve, mode, hot_water, holiday)}
    coordinator._rebuild_poll_groups()
    assert set(coordinator._paths_by_group[PollGroup.CONTROL]) == {
        curve,
        mode,
        hot_water,
        holiday,
    }
    use = entity_use(hass, entry)
    ids = {}
    for path in (curve, mode, hot_water, holiday):
        key = f"{GATEWAY}:{path}"
        use.note("number", entity(key, path))
        ids[path] = register(hass, entry, "number", key)
    before = coordinator.schedule_token()
    await later(hass)
    # Only the path without an exemption moves to the slow group.
    assert coordinator._paths_by_group[PollGroup.SLOW] == (curve,)
    assert set(coordinator._paths_by_group[PollGroup.CONTROL]) == {
        mode,
        hot_water,
        holiday,
    }
    assert coordinator._validity_group(coordinator.resources[curve]) is PollGroup.SLOW
    assert coordinator.schedule_token() != before
    # Enabled again: back at the control pace.
    er.async_get(hass).async_update_entity(ids[curve], disabled_by=None)
    await later(hass)
    assert curve in coordinator._paths_by_group[PollGroup.CONTROL]
    assert curve not in coordinator._paths_by_group[PollGroup.SLOW]
    # A stopped coordinator no longer follows the registry.
    await coordinator.async_shutdown()
    await coordinator.async_shutdown()
    er.async_get(hass).async_update_entity(ids[curve], disabled_by=None)
    coordinator.resources = {}
    coordinator._entity_use_changed()


async def test_all_enabled_plant_keeps_every_control_group(hass, entry):
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway(GATEWAY), entry
    )
    curve = "/heatingCircuits/hc1/heatCurveMax"
    coordinator.resources = {curve: _control(curve)}
    coordinator._rebuild_poll_groups()
    use = entity_use(hass, entry)
    use.note("number", entity(f"{GATEWAY}:{curve}", curve))
    register(hass, entry, "number", f"{GATEWAY}:{curve}", disabled=False)
    await later(hass)
    assert coordinator._paths_by_group[PollGroup.CONTROL] == (curve,)
    assert coordinator._paths_by_group[PollGroup.SLOW] == ()


# ----------------------------------------------------------------- G3


def signal(name):
    return make_request(
        "/signals/{signalId}", parameters={"signalId": name}, signal_ids=(name,)
    )


def test_signal_exemptions_keep_pairs_counters_faults_and_installation_date():
    assert signal_slows_when_unused(FLAG)
    assert signal_slows_when_unused("/signals/SRC.CUHP.HP1.DripPanTemp")
    for path in (
        "/signals/SC.InstallationDate.Day",
        "/signals/SRC.CUHP.HP1.Stats.CompressorCH",  # a run time counter
        "/signals/SRC.Some.Counter",
        "/signals/SRC.CUHP.HP1.NumberOfStarts",
        "/signals/SRC.CUHP.ErrorCode",
        "/signals/SRC.CUHP.HP1.AlarmActive",
        "/heatingCircuits/hc1/roomtemperature",  # not a signal
        "/heatSources/returnTemperature",  # an observation pair, no signal
    ):
        assert not signal_slows_when_unused(path), path


@pytest.mark.parametrize("kind", ["counters", "static"])
def test_counter_and_static_signal_classes_keep_their_pace(monkeypatch, kind):
    """G3/A3: a signal the catalog classes as counter or static keeps its pace
    even when its name matches none of the counter or fault patterns."""
    from custom_components.bosch_buderus_heating.local_polling import policy

    # Names that match none of the counter or fault patterns.
    name = f"SRC.Review.Plain{'A' if kind == 'counters' else 'B'}Meter"
    plain = "SRC.Review.PlainFlag"
    monkeypatch.setitem(policy.SIGNAL_CLASSES, name, kind)
    request, other = signal(name), signal(plain)
    assert signal_slows_when_unused(request.path)
    assert signal_slows_when_unused(other.path)
    use = SimpleNamespace(
        disabled_paths=lambda gateway: frozenset({request.path, other.path}),
        noted_paths=lambda gateway: frozenset(),
    )
    assert _hourly_reason(use, GATEWAY, other, None) == "disabled"
    assert _hourly_reason(use, GATEWAY, request, None) is None
    # Neither without any entity.
    nobody = SimpleNamespace(
        disabled_paths=lambda gateway: frozenset(),
        noted_paths=lambda gateway: frozenset(),
    )
    assert _hourly_reason(nobody, GATEWAY, other, lambda item: True) == (
        "without_entity"
    )
    assert _hourly_reason(nobody, GATEWAY, request, lambda item: True) is None


def test_floor_slows_but_never_speeds_up_a_reading():
    slow = {FLAG: 3600, "/signals/SC.InstallationDate.Day": 60}
    inventory = ResourceInventory(
        CONTEXT.identity, 60, floor=lambda request: slow.get(request.path)
    )
    assert inventory.period(signal("SRC.Some.Flag")) == UNUSED_SIGNAL_PERIOD
    assert inventory.period(signal("SRC.Some.Text")) == 60
    # A static reading stays at its own (longer) period.
    assert inventory.period(signal("SC.InstallationDate.Day")) == 86400
    assert ResourceInventory(CONTEXT.identity, 60).period(signal("A.B")) == 60


def test_a_shorter_period_applies_from_the_last_read():
    slow = {FLAG}
    inventory = ResourceInventory(
        CONTEXT.identity,
        60,
        floor=lambda request: 3600 if request.path in slow else None,
    )
    inventory.signal_ids = frozenset({"SRC.Some.Flag", "SRC.Some.Text"})
    request, other = signal("SRC.Some.Flag"), signal("SRC.Some.Text")
    record = inventory.add(request, 0)
    unconfirmed = inventory.add(other, 0)
    inventory.accept(success(request, 100), 100)
    assert record.due == 100 + 3600 and record.scheduled_period == 3600
    unconfirmed.due = 5000
    assert not inventory.periods_changed(120)  # nothing shorter yet
    slow.clear()
    assert inventory.periods_changed(130)
    assert record.due == 160 and unconfirmed.due == 5000
    # Already overdue by the new period: due now, never later than before.
    record.due = 4000
    assert inventory.periods_changed(500) and record.due == 500
    assert not inventory.periods_changed(600)


# ------------------------------------------------- G3 in the local source


async def test_local_source_reads_disabled_signals_hourly_until_enabled(hass, prepared):
    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    service, inventory = source.service, source.service.inventory
    use = entity_use(hass, p.entry)
    name = "SRC.Some.Flag"
    request = signal(name)
    date = signal("SC.InstallationDate.Day")
    ids = {}
    for item in (request, date):
        unique_id = local_only_unique_id(GATEWAY, item.path)
        use.note("sensor", entity(unique_id, item.path))
        ids[item.path] = register(hass, p.entry, "sensor", unique_id)
    # Until the registry answer is worked out: the usual interval.
    assert inventory.period(request) == 60
    await later(hass)
    assert inventory.period(request) == UNUSED_SIGNAL_PERIOD
    assert inventory.period(date) == 86400  # exempt, and static anyway
    inventory.signal_ids = frozenset({name})
    record = inventory.add(request, p.clock())
    now = p.clock()
    inventory.accept(success(request, now, context=service.context.identity), now)
    assert record.due == now + UNUSED_SIGNAL_PERIOD
    # Enabling the entity: the usual interval at once, without a restart.
    er.async_get(hass).async_update_entity(ids[request.path], disabled_by=None)
    await later(hass)
    assert inventory.period(request) == 60
    assert record.due == now + 60
    # Stopping the source stops it listening.
    await source.close()
    assert source._remove_use_listener is None


async def test_diagnostics_count_signals_read_hourly(hass, prepared):
    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    inventory = source.service.inventory
    use = entity_use(hass, p.entry)
    request = signal("SRC.Some.Flag")
    unique_id = local_only_unique_id(GATEWAY, request.path)
    use.note("sensor", entity(unique_id, request.path))
    entity_id = register(hass, p.entry, "sensor", unique_id)
    inventory.signal_ids = frozenset({"SRC.Some.Flag"})
    inventory.add(request, p.clock())
    assert source.diagnostics()["signals_read_hourly_unused"] == 0
    await later(hass)
    assert source.diagnostics()["signals_read_hourly_unused"] == 1
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    await later(hass)
    assert source.diagnostics()["signals_read_hourly_unused"] == 0
    await source.close()
    assert source.diagnostics()["signals_read_hourly_unused"] == 0


async def test_signals_without_any_entity_are_read_hourly(hass, prepared):
    """A3: identifiers, private signals and answers no entity can show are
    read hourly; readings the integration uses itself and anything an entity
    shows keep their pace."""
    from tests.test_local_api import read

    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    service, inventory = source.service, source.service.inventory
    use = entity_use(hass, p.entry)
    now = p.clock()

    def answer(name, raw, *, at=None):
        observation = replace(
            read(
                {"id": f"/signals/{name}", "writeable": 0, **raw},
                observed_monotonic=now if at is None else at,
            ),
            context=service.context.identity,
        )
        inventory.signal_ids = inventory.signal_ids | {name}
        record = inventory.add(observation.request, now)
        inventory.accept(
            ReadResult(
                observation.request,
                ReadState.SUCCESS,
                200,
                observation=observation,
                http_requests=1,
                response_bytes=10,
            ),
            observation.observed_monotonic,
        )
        return observation.request, record

    text = {"type": "stringValue", "value": "A1B2"}
    number = {"type": "floatValue", "value": 1.5, "unitOfMeasure": "C"}
    free, _ = answer("SRC.Some.Text", text)
    measured, _ = answer("SRC.Some.Number", number)
    declared, _ = answer(
        "SRC.Some.Choice", {**text, "value": "eco", "allowedValues": ["eco", "comfort"]}
    )
    fault, _ = answer("SRC.Some.ErrorText", text)
    counter, _ = answer("SRC.Some.StartsText", text)
    serial = signal("SRC.Device.SerialNumber")
    assert inventory.period(serial) == UNUSED_SIGNAL_PERIOD  # an identifier
    assert inventory.period(free) == UNUSED_SIGNAL_PERIOD
    for request in (measured, declared, fault, counter):
        assert inventory.period(request) == 60, request.path
    assert source.hourly_counts() == {"disabled": 0, "without_entity": 1}
    diagnostics = source.diagnostics()
    assert diagnostics["signals_read_hourly_without_entity"] == 1
    assert diagnostics["signals_read_hourly_unused"] == 0
    # A later answer that an entity can show: the usual pace again.
    _, record = answer("SRC.Some.Text", {**text, "value": "true"}, at=now + 1)
    assert record.observation.observed_monotonic == now + 1
    assert inventory.period(free) == 60
    # A noted entity (here enabled) keeps the pace whatever the answer.
    answer("SRC.Some.Text", text, at=now + 2)
    assert inventory.period(free) == UNUSED_SIGNAL_PERIOD
    unique_id = local_only_unique_id(GATEWAY, free.path)
    token = service._period_token()
    use.note("sensor", entity(unique_id, free.path))
    register(hass, p.entry, "sensor", unique_id, disabled=False)
    await later(hass)
    assert inventory.period(free) == 60
    # O13 learns of it: the noted paths are part of the period token.
    noted = use.noted_paths(GATEWAY)
    assert free.path in noted
    assert any(part is noted for part in service._period_token())
    assert not any(part is noted for part in token)
    assert source.hourly_counts() == {"disabled": 0, "without_entity": 0}
    await source.close()


async def test_period_token_follows_options_entities_and_cloud_plans(hass, prepared):
    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    use = entity_use(hass, p.entry)
    token = source._period_token(source.context, use, GATEWAY)
    marks = [object()]
    plan = SimpleNamespace(schedule_token=lambda: (marks[0],))
    original = p.runtime.source_data
    cloud = {
        key
        for key, binding in p.runtime.snapshot.bindings.items()
        if binding.connection_id == p.cloud
    }
    p.runtime.source_data = lambda key: plan if key in cloud else original(key)

    def same(first, second):
        return len(first) == len(second) and all(
            a is b for a, b in zip(first, second, strict=True)
        )

    first = token()
    assert same(first, token())
    marks[0] = object()  # the cloud changed its plan
    second = token()
    assert not same(first, second)
    # A cloud without a plan of its own: asked again every time.
    p.runtime.source_data = lambda key: (
        SimpleNamespace() if key in cloud else original(key)
    )
    assert not same(token(), token())
    # A stopped cloud has no pace: stable.
    p.runtime.source_data = lambda key: None if key in cloud else original(key)
    assert same(token(), token())
