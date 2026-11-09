"""O13: the published local state is carried forward per reading.

Each publication must decide and contain exactly what a complete rebuild
would: the same changes, the same views (apart from the age of views that
did not change, which no reader of the published state uses).
"""

import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating.local_api.catalog import (
    make_request,
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_polling.inventory import Freshness
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_state import (
    LocalCondition,
    LocalSourceState,
)
from tests.test_local_budget import Clock
from tests.test_local_polling import REQUEST, missing, no_discovery, signals, success
from tests.test_local_probe import CONTEXT

PAIRED = request_for_path("/heatSources/returnTemperature")
OTHER = request_for_path("/heatSources/currentSupplySetpoint")
SIGNAL_NAMES = ("SRC.Some.Flag", "SRC.Some.Text", "SRC.Example")


def signal(name):
    return make_request(
        "/signals/{signalId}", parameters={"signalId": name}, signal_ids=(name,)
    )


class World:
    """A cloud that may pace paired readings, and entities that may be off."""

    def __init__(self, *, token=True):
        self.cloud = True
        self.disabled: frozenset[str] = frozenset()
        self.current = True
        self.cloud_mark, self.disabled_mark = object(), object()
        self.with_token = token

    def cap(self, request):
        return (
            60 if self.cloud and request.path in (PAIRED.path, REQUEST.path) else None
        )

    def floor(self, request):
        return 3600 if request.path in self.disabled else None

    def token(self):
        return (self.cloud_mark, self.disabled_mark)

    def switch_cloud(self, value):
        self.cloud, self.cloud_mark = value, object()

    def disable(self, paths):
        self.disabled, self.disabled_mark = frozenset(paths), object()


def make_service(world, clock, published):
    service = LocalPollingService(
        CONTEXT,
        SimpleNamespace(read=None, close=None),
        LocalReadBudget(CONTEXT, clock=clock),
        SimpleNamespace(),
        is_current=lambda: world.current,
        publish=published.append,
        interval=300,
        cap=world.cap,
        floor=world.floor,
        period_token=world.token if world.with_token else None,
        clock=clock,
    )
    no_discovery(service.planner)
    service.planner.hints((REQUEST, PAIRED, OTHER), clock())
    service.confirm_identity(CONTEXT.identity)
    return service


def signature(snapshot):
    """What the complete rebuild compared before O13."""
    return (
        snapshot.condition,
        snapshot.identity_confirmed,
        snapshot.problem,
        snapshot.discovery_cursor,
        snapshot.discovery_exhausted,
        snapshot.pending_explicit,
        bool(snapshot.discovery_pause_seconds),
        tuple(
            (v.request.target, v.freshness, v.available, v.problem, v.observation)
            for v in snapshot.resources
        ),
    )


def without_age(snapshot):
    return replace(
        snapshot,
        resources=tuple(replace(v, age_seconds=None) for v in snapshot.resources),
    )


class Reference:
    """The complete rebuild of every publication, as before O13."""

    def __init__(self, service, published):
        self.service, self.published = service, published
        self.signature = None
        self.count = 0

    def publish(self):
        expected = self.service.snapshot()
        changed = signature(expected) != self.signature
        before = len(self.published)
        self.service._publish()
        assert (len(self.published) > before) is changed
        if changed:
            self.signature = signature(expected)
            self.count += 1
            assert without_age(self.published[-1]) == without_age(expected)
        # Also an unchanged publication still describes the current state.
        assert signature(self.published[-1]) == signature(expected)


def state(snapshot):
    """The published local state as the display reads it."""
    return LocalSourceState(
        CONTEXT.identity,
        snapshot.condition,
        snapshot.identity_confirmed,
        tuple(v.observation for v in snapshot.resources if v.observation is not None),
        resources=snapshot.resources,
    )


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("token", [True, False])
async def test_carried_forward_state_equals_a_complete_rebuild(seed, token):
    rng = random.Random(seed)
    clock, published = Clock(), []
    world = World(token=token)
    service = make_service(world, clock, published)
    reference = Reference(service, published)
    reference.publish()
    signals(service.inventory, SIGNAL_NAMES, clock())
    requests = [REQUEST, PAIRED, OTHER, *(signal(name) for name in SIGNAL_NAMES)]
    for _ in range(250):
        action = rng.randrange(11)
        now = clock()
        request = rng.choice(requests)
        record = service.inventory.records.get(request.target)
        if action <= 2:
            clock.advance(rng.choice((0, 0.5, 30, 59.9995, 60, 61, 120, 180, 600)))
        elif action == 3 and record is not None:
            service.inventory.accept(success(request, now, value=rng.randrange(3)), now)
        elif action == 4 and record is not None:
            service.inventory.accept(
                missing(request, status=rng.choice((404, 500))), now
            )
        elif action == 5:
            world.switch_cloud(not world.cloud)
        elif action == 6:
            world.disable(
                rng.sample([r.path for r in requests], rng.randrange(len(requests)))
            )
            if rng.randrange(2):
                service.periods_changed()
        elif action == 7:
            world.current = not world.current
            if world.current and not service._identity_confirmed:
                service.confirm_identity(CONTEXT.identity)
        elif action == 8:
            names = rng.sample(SIGNAL_NAMES, rng.randrange(1, len(SIGNAL_NAMES) + 1))
            signals(service.inventory, names, now)
        elif action == 9:
            clock.advance(86400)
            service.inventory.prune(clock())
        elif action == 10 and not service.planner.operations:
            service.planner.explicit((OTHER,), now)
        reference.publish()
    # Not a trivial walk: many publications, also of unchanged readings.
    assert reference.count > 40


async def test_unchanged_readings_are_not_worked_out_again(monkeypatch):
    clock, published = Clock(), []
    world = World()
    service = make_service(world, clock, published)
    now = clock()
    for request in (REQUEST, PAIRED, OTHER):
        service.inventory.accept(success(request, now), now)
    service._publish()
    built = []
    original = service.inventory.timed_view
    monkeypatch.setattr(
        service.inventory,
        "timed_view",
        lambda record, *args, **kwargs: (
            built.append(record.request.path) or original(record, *args, **kwargs)
        ),
    )
    clock.advance(10)
    service._publish()
    assert built == [] and len(published) == 1
    # One new reading: only its view is worked out again and published.
    service.inventory.accept(success(OTHER, clock(), value=5), clock())
    service._publish()
    assert built == [OTHER.path] and len(published) == 2
    # Its freshness changes with time: worked out again at that moment.
    clock.advance(50)
    service._publish()
    assert REQUEST.path in built and PAIRED.path in built
    assert published[-1].resources[0].freshness is Freshness.LATE


async def test_display_without_cloud_follows_the_local_period_at_once():
    """Without the cloud the paired reading keeps its local period: shown
    again at the next publication, without waiting for a new reading."""
    clock, published = Clock(), []
    world = World()
    service = make_service(world, clock, published)
    service.inventory.accept(success(PAIRED, clock()), clock())
    service._publish()
    view = published[-1].resources[1]
    assert view.request == PAIRED and view.target_interval_seconds == 60
    assert view.available and PAIRED.target in {
        o.request.target for o in state(published[-1]).current_observations
    }
    # The cloud keeps the pace: after three of its intervals the local
    # reading no longer counts and the cloud value is shown.
    clock.advance(200)
    service._publish()
    view = published[-1].resources[1]
    assert view.freshness is Freshness.EXPIRED and not view.available
    assert not state(published[-1]).current_observations
    # The cloud stops: the local period applies (300 s), so the same reading
    # is current again and shown without the cloud.
    world.switch_cloud(False)
    service._publish()
    view = published[-1].resources[1]
    assert view.freshness is Freshness.FRESH and view.available
    assert view.target_interval_seconds == 300
    assert [o.request for o in state(published[-1]).current_observations] == [PAIRED]
    assert without_age(published[-1]) == without_age(service.snapshot())


async def test_source_switch_local_to_cloud_and_back():
    """A paused local access hands the display to the cloud; when it is ready
    again the same current reading is shown locally again."""
    clock, published = Clock(), []
    world = World()
    service = make_service(world, clock, published)
    service.inventory.accept(success(REQUEST, clock()), clock())
    service._publish()
    local = state(published[-1])
    assert local.condition is LocalCondition.READY
    assert [o.request for o in local.current_observations] == [REQUEST]
    await service.budget.pause(30, "connection")
    service._publish()
    paused = state(published[-1])
    assert paused.condition is LocalCondition.PAUSED
    assert not paused.current_observations  # the cloud value is shown
    assert not any(v.available for v in published[-1].resources)
    clock.advance(30)
    service._publish()
    again = state(published[-1])
    assert again.condition is LocalCondition.READY
    assert [o.request for o in again.current_observations] == [REQUEST]
    assert again.current_observations[0] is local.current_observations[0]
    # An unreachable access (no longer current) shows the cloud as well.
    world.current = False
    service._publish()
    assert state(published[-1]).condition is LocalCondition.UNAVAILABLE
    assert not state(published[-1]).current_observations
    assert without_age(published[-1]) == without_age(service.snapshot())


async def test_a_reading_added_again_publishes_the_new_order():
    """A record removed and added again moves to the end, also with the same
    published part: the complete rebuild published the new order."""
    clock, published = Clock(), []
    service = make_service(World(), clock, published)
    reference = Reference(service, published)
    reference.publish()
    del service.inventory.records[REQUEST.target]
    service.inventory.add(REQUEST, clock())
    reference.publish()
    assert [v.request for v in published[-1].resources] == [PAIRED, OTHER, REQUEST]
    assert reference.count == 2
