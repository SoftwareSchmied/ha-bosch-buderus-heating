"""Performance measures (M1 to M4, M7, M8): same answers as before, fewer computations.

Each measure is checked against the code it replaced (an oracle written out
here) on seeded random input, and its guard is broken in a mutation test
that must fail.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    burner_share,
    derived_inputs,
    energy_contracts,
    local_selection,
    local_state,
)
from custom_components.bosch_buderus_heating.configuration_changes import (
    ConfigurationRuns,
)
from custom_components.bosch_buderus_heating.derived_inputs import Input
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_only import (
    HYBRID_MANAGER_SIGNALS,
    definition_for,
    hybrid_evidence,
)
from custom_components.bosch_buderus_heating.local_plant_view import private_path
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness,
    ResourceView,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.local_state import (
    LocalCondition,
    LocalSourceState,
    fault_path,
)
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    PerformanceFactorSensor,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_configuration_changes import topology
from tests.test_derived_values import air
from tests.test_local_probe import CONTEXT
from tests.test_local_read import payload
from tests.test_local_selection import LOCAL_BINDING, TARGET, FakeRuntime
from tests.test_performance import _engine

PATHS = (
    "/system/type",
    "/heatSources/type",
    "/heatSources/hs1/type",
    "/heatSources/hs2/type",
    "/heatSources/hs10/typeX",
    "/heatSources/hybrid/controlStrategy",
    "/heatSources/hybrid/",
    "/heatSources/numberOfStarts",
    "/system/sensors/temperatures/outdoor_t1",
    HYBRID_MANAGER_SIGNALS + "Status",
    "/signals/HYBMANX",
    "/signals/SRC.CUHP.HP1.CompressorPower",
)
VALUES = ("hybrid", "Hybrid Boiler", "hybman", "HYB-MAN", "boiler", None, 3, "eHybrid")


# ------------------------------------------------------------------ M1
def _oracle(coordinator, read, local_paths):
    """hybrid_detected as it was: every path is read, then looked at."""
    readings = []
    data = getattr(coordinator, "data", None) or {}
    for path, resource in (getattr(coordinator, "resources", None) or {}).items():
        current = data[path].resource if path in data else resource
        readings.append((path, current.value if current.has_value else None))
    for path in local_paths:
        reading = read(coordinator, path)
        readings.append((path, reading.value if reading is not None else None))
    return bool(hybrid_evidence(readings))


def _plant(rng):
    cloud = {
        path: Resource(path, value=rng.choice(VALUES), has_value=rng.random() < 0.8)
        for path in PATHS
        if rng.random() < 0.5
    }
    local = {path: rng.choice(VALUES) for path in PATHS if path not in cloud}
    local.update({f"/signals/SYN.{n}": 1.0 for n in range(rng.randrange(40))})
    return cloud, local


def _fake_read(local):
    reads = []

    def read(coordinator, path):
        reads.append(path)
        value = local.get(path)
        return None if value is None else Input(path, value, "local", None)

    return read, reads


@pytest.mark.parametrize("seed", range(200))
def test_hybrid_detection_is_what_it_was(monkeypatch, seed):
    rng = random.Random(seed)
    cloud, local = _plant(rng)
    coordinator = SimpleNamespace(
        resources=cloud, data={p: SimpleNamespace(resource=r) for p, r in cloud.items()}
    )
    read, reads = _fake_read(local)
    monkeypatch.setattr(burner_share, "read", read)
    monkeypatch.setattr(burner_share, "local_only_paths", lambda c: frozenset(local))
    expected = _oracle(coordinator, read, local)
    reads.clear()
    assert burner_share.hybrid_detected(coordinator) is expected
    # Only the type paths are read: the other paths count by their name.
    assert set(reads) <= {p for p in local if p.endswith("/type")}


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("/system/type", "hybrid", True),
        ("/system/type", "boiler", False),
        ("/heatSources/type", "Hybrid Boiler", True),
        ("/heatSources/hs1/type", "hybman", True),
        ("/heatSources/hs1/type", "boiler", False),
        ("/heatSources/hybrid/controlStrategy", None, True),
        (HYBRID_MANAGER_SIGNALS + "Status", None, True),
        ("/heatSources/numberOfStarts", "hybrid", False),
    ],
)
@pytest.mark.parametrize("where", ["cloud", "local"])
def test_each_kind_of_hybrid_evidence_alone(monkeypatch, where, path, value, expected):
    cloud = (
        {path: Resource(path, value=value, has_value=value is not None)}
        if where == "cloud"
        else {}
    )
    local = {path: value if value is not None else 1.0} if where == "local" else {}
    coordinator = SimpleNamespace(
        resources=cloud, data={p: SimpleNamespace(resource=r) for p, r in cloud.items()}
    )
    read, _ = _fake_read(local)
    monkeypatch.setattr(burner_share, "read", read)
    monkeypatch.setattr(burner_share, "local_only_paths", lambda c: frozenset(local))
    assert burner_share.hybrid_detected(coordinator) is expected
    assert _oracle(coordinator, read, local) is expected


def test_a_signal_of_the_hybrid_manager_counts_without_being_read(monkeypatch):
    read, reads = _fake_read({HYBRID_MANAGER_SIGNALS + "Status": 1.0})
    monkeypatch.setattr(burner_share, "read", read)
    monkeypatch.setattr(
        burner_share,
        "local_only_paths",
        lambda c: frozenset({HYBRID_MANAGER_SIGNALS + "Status"}),
    )
    assert burner_share.hybrid_detected(SimpleNamespace(resources={}, data={}))
    assert reads == []


def test_hybrid_detection_is_worked_out_once_per_update_pass(monkeypatch):
    selection = LocalSelection(FakeRuntime(), TARGET)
    coordinator = SimpleNamespace(local_selection=selection, resources={}, data={})
    other = SimpleNamespace(local_selection=selection, resources={}, data={})
    calls = Counter()
    original = burner_share._hybrid_detected

    def counting(item):
        calls[id(item)] += 1
        return original(item)

    monkeypatch.setattr(burner_share, "_hybrid_detected", counting)
    monkeypatch.setattr(burner_share, "local_only_paths", lambda c: frozenset())
    with selection.update_pass():
        assert [burner_share.hybrid_detected(coordinator) for _ in range(5)] == [
            False
        ] * 5
        burner_share.hybrid_detected(other)
    assert calls[id(coordinator)] == 1 and calls[id(other)] == 1
    # Outside a pass nothing is kept, and the next pass sees new readings.
    coordinator.resources = {
        "/system/type": Resource("/system/type", value="hybrid", has_value=True)
    }
    assert burner_share.hybrid_detected(coordinator) is True
    assert burner_share.hybrid_detected(coordinator) is True
    assert calls[id(coordinator)] == 3
    with selection.update_pass():
        assert burner_share.hybrid_detected(coordinator) is True


# ------------------------------------------------------------------ M2
OBSERVED = (
    "/heatingCircuits/hc2/currentRoomSetpoint",
    "/heatSources/actualSupplyTemperature",
    "/heatSources/returnTemperature",
    "/heatSources/systemPressure",
    "/heatSources/actualModulation",
    "/heatSources/compressor/powerElecActual",
    "/system/sensors/temperatures/outdoor_t1",
    "/dhwCircuits/dhw1/actualTemp",
    "/dhwCircuits/dhw1/currentSetpoint",
    "/heatingCircuits/hc1/currentRoomSetpoint",
    "/notifications",
    "/devices/device1/errors",
    "/devices/device2/errors",
)
FAULT_BODIES = {
    "/notifications": {"type": "errorList", "values": []},
    "/devices/device1/errors": {"type": "deviceErrors", "values": []},
    "/devices/device2/errors": {"type": "deviceErrors", "values": []},
}
SIGNAL_PATHS = (
    "/signals/SRC.CUHP.HP1.CompressorPower",
    "/signals/SYN.Test1",
    "/pool/currentTemp",
    "/heatSources/dhw/cylinderUpperTemp",
)
OTHER = replace(CONTEXT.identity, generation=CONTEXT.identity.generation + 1)


def _obs(path, mono, *, context=CONTEXT.identity, value=21.25):
    body = (
        json.dumps({"id": path, "writeable": 0, **FAULT_BODIES[path]}).encode()
        if path in FAULT_BODIES
        else payload(path=path, value=value)
    )
    return decode_resource(
        request_for_path(path, signal_ids=(path.removeprefix("/signals/"),)),
        body,
        context=context,
        observed_at=datetime(2026, 11, 1, tzinfo=UTC),
        observed_monotonic=mono,
    )


def _view(path, mono, *, context=CONTEXT.identity, available=True, observation=True):
    obs = _obs(path, mono, context=context) if observation else None
    request = obs.request if obs is not None else request_for_path(path)
    return ResourceView(request, Freshness.FRESH, available, 1.0, 60, 60.0, None, obs)


def _old_check(context, condition, confirmed, observations, resources):
    """The checks of the state as they were: the message of the error."""
    if (
        context.namespace is not GatewayNamespace.K40_LOCAL
        or not isinstance(condition, LocalCondition)
        or type(confirmed) is not bool
        or not isinstance(observations, tuple)
        or len(observations) > 2048
        or (condition is LocalCondition.READY and not confirmed)
        or any(not hasattr(o, "request") or o.context != context for o in observations)
    ):
        return "invalid_local_source_state"
    paths = [o.request.target for o in observations]
    if len(set(paths)) != len(paths):
        return "duplicate_local_observation"
    if (
        not isinstance(resources, tuple)
        or len(resources) > 2048
        or any(
            not isinstance(v, ResourceView)
            or (v.observation is not None and v.observation.context != context)
            for v in resources
        )
        or len({v.request.target for v in resources}) != len(resources)
    ):
        return "invalid_local_resource_views"
    return None


def _old_current(condition, confirmed, resources):
    if confirmed and condition in {LocalCondition.READY, LocalCondition.PAUSED}:
        return tuple(
            v.observation
            for v in resources
            if v.available and v.observation is not None
        )
    return ()


@pytest.fixture(autouse=False)
def fresh_checks():
    local_state._VALIDATED.clear()
    yield
    local_state._VALIDATED.clear()


def _mutate(rng, views, mono):
    """One random edit of a tuple of views: valid and invalid ones."""
    views = list(views)
    index = rng.randrange(len(views))
    kind = rng.choice(
        [
            "same",
            "same",
            "replace",
            "replace",
            "replace",
            "drop",
            "duplicate",
            "foreign",
            "retarget",
            "plain",
            "unavailable",
            "noobs",
            "append",
            "swap",
            "wrong_type",
        ]
    )
    path = views[index].request.path
    if kind == "replace":
        views[index] = _view(path, mono)
    elif kind == "drop":
        del views[index]
    elif kind == "duplicate":
        views.append(views[rng.randrange(len(views))])
    elif kind == "foreign":
        views[index] = _view(path, mono, context=OTHER)
    elif kind == "retarget":
        views[index] = _view(rng.choice(OBSERVED), mono)
    elif kind == "unavailable":
        views[index] = _view(path, mono, available=False)
    elif kind == "noobs":
        views[index] = _view(path, mono, observation=False)
    elif kind == "append":
        views.append(_view("/heatingCircuits/hc3/currentRoomSetpoint", mono))
    elif kind == "swap" and len(views) > 1:
        views[0], views[-1] = views[-1], views[0]
    elif kind == "wrong_type":
        views[index] = "not a view"
    return tuple(views)


def _states(seed):
    """Random chains of states; yields (answer, expectation) for each."""
    rng = random.Random(seed)
    local_state._VALIDATED.clear()
    views = tuple(_view(path, 1.0) for path in OBSERVED)
    mono = 2.0
    outcomes = []
    for _ in range(60):
        candidate = _mutate(rng, views, mono)
        mono += 1.0
        condition = rng.choice(list(LocalCondition))
        confirmed = rng.random() < 0.9
        observations = tuple(
            v.observation
            for v in candidate
            if isinstance(v, ResourceView) and v.observation is not None
        )
        if rng.random() < 0.15:
            observations = (*observations[::-1][:-1], *observations[:1])
        expected = _old_check(
            CONTEXT.identity, condition, confirmed, observations, candidate
        )
        try:
            state = LocalSourceState(
                CONTEXT.identity,
                condition,
                confirmed,
                observations,
                resources=candidate,
            )
        except ValueError as error:
            answer = str(error)
        else:
            answer = None
            if state.current_observations != _old_current(
                condition, confirmed, candidate
            ) or any(
                a is not b
                for a, b in zip(
                    state.current_observations,
                    _old_current(condition, confirmed, candidate),
                    strict=False,
                )
            ):
                answer = "different current observations"
            if answer is None and tuple(state.fault_views) != tuple(
                v for v in candidate if fault_path(v.request.path)
            ):
                answer = "different fault views"
            if expected is None:
                views = candidate
        outcomes.append((answer, expected))
    return outcomes


@pytest.mark.parametrize("seed", range(120))
def test_a_published_state_is_checked_as_before(fresh_checks, seed):
    outcomes = _states(seed)
    assert all(answer == expected for answer, expected in outcomes), outcomes
    assert any(expected is None for _, expected in outcomes)
    assert any(expected is not None for _, expected in outcomes)


def test_a_full_size_state_is_carried_without_checking_each_view(fresh_checks):
    views = tuple(
        _view(f"/signals/S{n:04d}" if False else OBSERVED[n % len(OBSERVED)], 1.0)
        for n in range(len(OBSERVED))
    )
    first = LocalSourceState(
        CONTEXT.identity,
        LocalCondition.READY,
        True,
        tuple(v.observation for v in views),
        resources=views,
    )
    assert local_state._VALIDATED[CONTEXT.identity][0] is views
    replaced = (*views[:3], _view(OBSERVED[3], 2.0), *views[4:])
    second = LocalSourceState(
        CONTEXT.identity,
        LocalCondition.READY,
        True,
        tuple(v.observation for v in replaced),
        resources=replaced,
    )
    assert local_state._VALIDATED[CONTEXT.identity][0] is replaced
    assert second.current_observations[3] is replaced[3].observation
    assert first.current_observations[3] is views[3].observation
    # A foreign observation in the one replaced view is still caught.
    foreign = (*views[:3], _view(OBSERVED[3], 3.0, context=OTHER), *views[4:])
    with pytest.raises(ValueError, match="invalid_local_source_state"):
        LocalSourceState(
            CONTEXT.identity,
            LocalCondition.READY,
            True,
            tuple(v.observation for v in foreign),
            resources=foreign,
        )


# ------------------------------------------------------------------ M4
def _accepting(runs, generations):
    answers = []
    for generation in generations:
        try:
            expected = generation == runs.capture(
                generation.connection_id, generation.binding_id
            )
        except Exception:
            expected = False
        answers.append((runs.accepts(generation), expected))
    return answers


def test_accepts_answers_as_before_and_captures_once():
    before, cloud, _target, binding, others = topology()
    runs = ConfigurationRuns(before)
    generations = [runs.capture(cloud, key) for key in [binding, *others]]
    generations.append(runs.capture(cloud))
    captures = []
    original = runs.capture

    def counting(*args, **kwargs):
        captures.append(args)
        return original(*args, **kwargs)

    runs.capture = counting  # type: ignore[method-assign]
    for _ in range(10):
        for generation in generations:
            assert runs.accepts(generation) is True
    # The answer is worked out once per generation, not at every call.
    assert len(captures) == len(generations)
    runs.capture = original  # type: ignore[method-assign]
    assert all(a == e for a, e in _accepting(runs, generations))
    # Every change starts anew: switch the binding and then the cloud off.
    data, options = before.to_data(), before.to_options()
    options["bindings"][binding]["enabled"] = False
    from custom_components.bosch_buderus_heating.configuration import (
        load_configuration,
    )
    from tests.test_configuration import OWNER

    runs.update(load_configuration(OWNER, data, options))
    answers = _accepting(runs, generations)
    assert all(a == e for a, e in answers)
    assert answers[0] == (False, False)
    runs.invalidate_binding(others[0])
    answers = _accepting(runs, generations)
    assert all(a == e for a, e in answers)
    options["connections"][cloud]["enabled"] = False
    runs.update(load_configuration(OWNER, data, options))
    answers = _accepting(runs, generations)
    assert all(a == e for a, e in answers) and not any(a for a, _ in answers)


def test_accepts_never_keeps_an_answer_over_a_change():
    before, cloud, _target, binding, _others = topology()
    runs = ConfigurationRuns(before)
    generation = runs.capture(cloud, binding)
    assert runs.accepts(generation) is True
    assert runs.accepts(generation) is True
    runs.invalidate_binding(binding)
    assert runs.accepts(generation) is False
    assert runs.accepts(runs.capture(cloud, binding)) is True


# ------------------------------------------------------------------ M7
def _energy(rng, path="/heatSources/emon/totalConsumption"):
    parts = [
        {key: rng.choice([0.0, 12.5, 400.0, -1.0, "x", 2**60])}
        for key in ("compressor", "eheater", "outputProduced", "burner")
        if rng.random() < 0.8
    ]
    return Resource(
        path,
        values=tuple(parts),
        metadata=ResourceMetadata(
            resource_type="emonValue", unit=rng.choice([None, "kWh", "Wh", "J"])
        ),
    )


@pytest.mark.parametrize("seed", range(60))
def test_the_energy_assessment_is_the_same_and_kept_per_resource(seed):
    rng = random.Random(seed)
    energy_contracts._ASSESSED.clear()
    resources = [_energy(rng, f"/heatSources/emon/part{n % 3}") for n in range(12)]
    for resource in resources * 3:
        assert energy_contracts.assess_energy(resource) == (
            energy_contracts._assess_energy(resource)
        )
    # The same object gives the same answer object; a new object of the same
    # path is assessed again.
    first = energy_contracts.assess_energy(resources[0])
    assert energy_contracts.assess_energy(resources[0]) is first
    newer = replace(resources[0], values=({"compressor": 99.0},))
    assert energy_contracts.assess_energy(newer) == energy_contracts._assess_energy(
        newer
    )
    assert energy_contracts.assess_energy(newer) is not first


def test_the_energy_assessments_kept_stay_bounded():
    energy_contracts._ASSESSED.clear()
    for n in range(energy_contracts._MAX_ASSESSED * 3):
        resource = Resource(
            f"/heatSources/emon/p{n}",
            values=({"compressor": 1.0},),
            metadata=ResourceMetadata(resource_type="emonValue"),
        )
        energy_contracts.assess_energy(resource)
        assert len(energy_contracts._ASSESSED) <= energy_contracts._MAX_ASSESSED


# ------------------------------------------------------------------ M8
async def test_the_counters_are_taken_once_per_update_pass(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    plant = air(hass, freezer)
    engine = await _engine(plant)
    selection = LocalSelection(FakeRuntime(), TARGET)
    plant.coordinator.local_selection = selection
    periods = (Period.TODAY, Period.SINCE_COMMISSIONING, Period.CURRENT_MONTH)
    sensors = [
        PerformanceFactorSensor(plant.coordinator, engine, period, "total")
        for period in periods
    ]
    alone = []
    for sensor in sensors:
        sensor._refresh()
        alone.append(sensor._result)
    calls = []
    original = engine.sync
    monkeypatch.setattr(
        engine, "sync", lambda now=None: (calls.append(1), original(now))[1]
    )
    with selection.update_pass():
        for sensor in sensors:
            sensor._refresh()
    assert len(calls) == 1
    assert [sensor._result for sensor in sensors] == alone
    # Outside a pass every sensor takes the counters itself, as before.
    for sensor in sensors:
        sensor._refresh()
    assert len(calls) == 1 + len(sensors)
    with selection.update_pass():
        sensors[0]._refresh()
        sensors[1]._refresh()
    assert len(calls) == 2 + len(sensors)


# ------------------------------------------------------------------ M3
def _fault_answers(views):
    real = LocalSourceState(
        CONTEXT.identity,
        LocalCondition.READY,
        True,
        tuple(v.observation for v in views if v.observation is not None),
        resources=views,
    )
    # Any other object is looked through as a whole, as it always was.
    plain = SimpleNamespace(
        condition=LocalCondition.READY,
        resources=views,
        current_observations=real.current_observations,
    )
    answers = []
    for state in (real, plain):
        runtime = FakeRuntime()
        runtime.local_state = lambda binding, item=state: (  # type: ignore[method-assign]
            item if binding == LOCAL_BINDING else None
        )
        selection = LocalSelection(runtime, TARGET)
        answers.append((selection.fault_reading(), selection.device_error_lists()))
    return answers


@pytest.mark.parametrize("seed", range(40))
def test_the_fault_lists_are_found_as_before(seed):
    rng = random.Random(seed)
    local_state._VALIDATED.clear()
    views = []
    for path in rng.sample(OBSERVED, len(OBSERVED)):
        views.append(
            _view(
                path,
                float(len(views)),
                available=rng.random() < 0.8,
            )
        )
    views = tuple(
        replace(v, freshness=rng.choice(list(Freshness))) if rng.random() < 0.3 else v
        for v in views
    )
    real, plain = _fault_answers(views)
    assert real == plain
    # And after a few views were replaced, as the service does.
    for _ in range(5):
        index = rng.randrange(len(views))
        views = (
            *views[:index],
            _view(views[index].request.path, 99.0),
            *views[index + 1 :],
        )
        real, plain = _fault_answers(views)
        assert real == plain


def _old_same_readings(before, after):
    if before.keys() != after.keys():
        return False
    return all(
        local_selection._same_reading(before[path], now) for path, now in after.items()
    )


def _old_differing_paths(before, after):
    changed = set(before.keys() ^ after.keys())
    changed.update(
        path
        for path, now in after.items()
        if path in before and not local_selection._same_reading(before[path], now)
    )
    return changed


def _readings(rng, observations, paths):
    result = {}
    for path in paths:
        roll = rng.random()
        if roll < 0.15:
            result[path] = None
        else:
            result[path] = ("local", rng.choice(observations))
    return result


@pytest.mark.parametrize("seed", range(120))
def test_the_readings_are_compared_as_before(seed):
    rng = random.Random(seed)
    observations = [
        _obs(path, float(n), value=rng.choice([1.0, 2.0, 1.0]))
        for n, path in enumerate(OBSERVED[:7] * 2)
    ]
    paths = [f"/p/{n}" for n in range(rng.randrange(1, 14))]
    before = _readings(rng, observations, paths)
    after = dict(before)
    for path in rng.sample(paths, rng.randrange(len(paths) + 1)):
        after[path] = rng.choice(
            [None, ("local", rng.choice(observations)), ("other", observations[0])]
        )
    if rng.random() < 0.3:
        after.pop(rng.choice(paths), None)
    if rng.random() < 0.3:
        after["/p/new"] = ("local", observations[1])
    if rng.random() < 0.3:  # the same keys in another order
        after = dict(reversed(list(after.items())))
    assert local_selection._same_readings(before, after) is _old_same_readings(
        before, after
    )
    assert local_selection._differing_paths(before, after) == _old_differing_paths(
        before, after
    )
    assert local_selection._same_readings(before, dict(before))


# ------------------------------------------------------------------ known paths
POOL = (
    [f"/heatSources/hs{n}/emon/totalConsumption" for n in range(1, 5)]
    + [f"/heatingCircuits/hc{n}/currentRoomSetpoint" for n in range(1, 5)]
    + [f"/signals/S{n}" for n in range(30)]
)
PATTERNS = (
    re.compile(r"/heatSources/hs\d+/emon/totalConsumption"),
    re.compile(r"/heatingCircuits/hc\d+/currentRoomSetpoint"),
    re.compile(r"/signals/S\d+"),
    re.compile(r"/nothing/.*"),
)


@pytest.mark.parametrize("seed", range(40))
def test_the_known_paths_are_what_they_were(seed):
    rng = random.Random(seed)
    derived_inputs._KNOWN.clear()
    for _ in range(25):
        data = dict.fromkeys(rng.sample(POOL, rng.randrange(len(POOL))))
        local = dict.fromkeys(rng.sample(POOL, rng.randrange(len(POOL))))
        coordinator = SimpleNamespace(
            data=data,
            local_selection=SimpleNamespace(
                local_only_observations=lambda item=local: item
            ),
        )
        for pattern in rng.sample(PATTERNS, len(PATTERNS)):
            expected = tuple(
                sorted(path for path in {*data, *local} if pattern.fullmatch(path))
            )
            assert derived_inputs.known_paths(coordinator, pattern) == expected
            assert derived_inputs.known_paths(coordinator, pattern) == expected


# ------------------------------------------------------------------ changed()
def _old_markers(selection, observations):
    """The two readings dictionaries of changed(), built as they were."""
    readable = {
        o.request.path
        for o in observations
        if definition_for(o.request.path) is not None
        or (
            local_selection.pair_for_path(o.request.path) is None
            and not private_path(o.request.path)
        )
    }
    assert set(selection.local_only_observations()) == readable
    tracked = selection._tracked_local_only
    readings, local_only = {}, {}
    for observation in observations:
        path = observation.request.path
        if local_selection.pair_for_path(path) is not None:
            readings[path] = (LOCAL_BINDING, observation)
        if path in readable:
            local_only[path] = (LOCAL_BINDING, observation) if path in tracked else None
    return readings, local_only


@pytest.mark.parametrize("seed", range(80))
def test_changed_reports_what_it_reported_before(seed):
    rng = random.Random(seed)
    paths = [*OBSERVED[:10], *SIGNAL_PATHS]
    values = dict.fromkeys(paths, 21.25)
    present = dict.fromkeys(paths, True)
    runtime = FakeRuntime([])
    selection = LocalSelection(runtime, TARGET)
    previous = None
    mono = 1.0
    seen_change = seen_same = False
    for _ in range(60):
        action = rng.choice(
            ["same", "same", "value", "drop", "add", "track", "untrack"]
        )
        path = rng.choice(paths)
        if action == "value":
            values[path] += 1.5
        elif action == "drop":
            present[path] = False
        elif action == "add":
            present[path] = True
        elif action == "track":
            selection.track_local_only(path)
        elif action == "untrack" and selection._tracked_local_only[path] > 0:
            selection.untrack_local_only(path)
        mono += 1.0
        observations = [
            _obs(p, mono if p == path else 1.0, value=values[p])
            for p in paths
            if present[p]
        ]
        runtime.observations = observations
        result = selection.changed()
        markers = _old_markers(selection, observations)
        expected = not (
            previous is not None
            and _old_same_readings(previous[0], markers[0])
            and _old_same_readings(previous[1], markers[1])
        )
        expected_paths = (
            None
            if previous is None
            else frozenset(
                (
                    *_old_differing_paths(previous[0], markers[0]),
                    *_old_differing_paths(previous[1], markers[1]),
                )
            )
        )
        assert result is expected
        assert selection.changed_paths == expected_paths
        seen_change |= result and previous is not None
        seen_same |= not result
        previous = markers
    assert seen_change
