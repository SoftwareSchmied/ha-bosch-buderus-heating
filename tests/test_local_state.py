"""Recovery can retain old observations without publishing them as fresh."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    SourceFailure,
)
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_state import (
    LocalCondition,
    LocalSourceState,
)
from tests.test_connection_runtime import build, settle
from tests.test_local_read import payload


async def test_health_changes_preserve_context_and_observation_timestamps(hass):
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        context = harness.source("local", "local").context
        identity = context.identity_context(GatewayNamespace.K40_LOCAL)
        observation = decode_resource(
            request_for_path("/heatingCircuits/hc2/currentRoomSetpoint"),
            payload(),
            context=identity,
            observed_at=datetime.now(UTC),
            observed_monotonic=100,
        )
        state = LocalSourceState(identity, LocalCondition.READY, True, (observation,))
        assert context.publish_local(state)
        assert runtime.source_data("local") is state
        for condition in (
            LocalCondition.PAUSED,
            LocalCondition.AUTH_REQUIRED,
            LocalCondition.CERTIFICATE_CHANGED,
            LocalCondition.UNAVAILABLE,
            LocalCondition.INITIALIZING,
        ):
            updated = replace(state, condition=condition)
            assert context.publish_local(updated)
            assert context.configuration_current
            assert runtime.local_state("local").last_observations[0] is observation
            assert observation.observed_monotonic == 100
            assert runtime.source_data("local") is None
            assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert context.publish_local(state)
        assert runtime.source_data("local") is state
        wrong = replace(
            state,
            context=replace(identity, generation=identity.generation + 1),
            last_observations=(),
        )
        assert not context.publish_local(wrong)
    finally:
        await runtime.close()
    assert not context.configuration_current
    assert not context.publish_local(state)
    assert runtime.local_state("local") is None


async def test_local_state_is_rejected_for_cloud_binding(hass):
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        source = next(iter(harness.drivers[cloud][-1].sources.values()))[-1]
        state = LocalSourceState(
            source.context.identity_context(GatewayNamespace.K40_LOCAL),
            LocalCondition.INITIALIZING,
        )
        assert not source.context.publish_local(state)
    finally:
        await runtime.close()


async def test_unexpected_source_failure_keeps_only_historical_local_state(hass):
    _, _, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        source = harness.source("local", "local")
        state = LocalSourceState(
            source.context.identity_context(GatewayNamespace.K40_LOCAL),
            LocalCondition.READY,
            True,
        )
        assert source.context.publish_local(state)
        source.messages.put_nowait(SourceFailure())
        await settle(hass)
        assert runtime.source_data("local") is None
        assert runtime._local_states["local"].condition is LocalCondition.UNAVAILABLE
        assert not runtime._local_states["local"].identity_confirmed
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "condition,confirmed",
    [("ready", True), (LocalCondition.READY, False), (LocalCondition.READY, 1)],
)
def test_invalid_health_state_is_rejected(condition, confirmed):
    from tests.test_local_probe import CONTEXT

    with pytest.raises(ValueError):
        LocalSourceState(CONTEXT.identity, condition, confirmed)


def test_current_observations_are_worked_out_once_and_only_when_ready():
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        Freshness,
        ResourceView,
    )
    from tests.test_local_probe import CONTEXT

    request = request_for_path("/heatingCircuits/hc2/currentRoomSetpoint")
    observation = decode_resource(
        request,
        payload(),
        context=CONTEXT.identity,
        observed_at=datetime.now(UTC),
        observed_monotonic=100,
    )
    views = (
        ResourceView(request, Freshness.FRESH, True, 0.0, 60, None, None, observation),
    )
    state = LocalSourceState(
        CONTEXT.identity, LocalCondition.READY, True, resources=views
    )
    assert state.current_observations == (observation,)
    assert state.current_observations is state.current_observations
    # A pause keeps the current readings (SEL-1); other conditions do not.
    assert replace(state, condition=LocalCondition.PAUSED).current_observations == (
        observation,
    )
    assert (
        replace(
            state, condition=LocalCondition.PAUSED, identity_confirmed=False
        ).current_observations
        == ()
    )
    for condition in (
        LocalCondition.UNAVAILABLE,
        LocalCondition.AUTH_REQUIRED,
        LocalCondition.CERTIFICATE_CHANGED,
    ):
        assert replace(state, condition=condition).current_observations == ()
    assert (
        replace(state, condition=LocalCondition.INITIALIZING).current_observations == ()
    )
    unavailable = replace(views[0], available=False)
    assert replace(state, resources=(unavailable,)).current_observations == ()
    # Not part of the comparison: equal states stay equal.
    assert replace(state) == state
