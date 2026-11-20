"""Sign-in outage category, rejection hint, pause counter, gateway reachable.

The cycle tests run the coordinator's own refresh with the simulated clock of
the plant replay (``tests/test_cloud_outages_plant.py``): a failing sign-in
and a failing gateway are separate outages.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import managed_local as local_module
from custom_components.bosch_buderus_heating.auth_diagnostics import (
    MAX_FAILED_ATTEMPTS,
    async_load_auth_histories,
    loaded_auth_history,
)
from custom_components.bosch_buderus_heating.configuration_migration import (
    legacy_cloud_connection_id,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    CIRCUIT_FAILURE_THRESHOLD,
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.gateway_reachable import (
    GatewayReachableBinarySensor,
    cloud_access,
    combine,
    local_access,
)
from custom_components.bosch_buderus_heating.local_pause_stats import (
    MAX_REASONS,
    PauseCounter,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    RefreshAttempt,
    RefreshRefused,
    RefreshTokenRejected,
    RequestTimeout,
    SignInUnavailable,
    TokenEndpointRejection,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ServiceUnavailable,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_outages_plant import (
    INTERVAL,
    _plant,
    _refresh,
)
from tests.test_cloud_polling_plan import Clock

WARNING_TEXT = "the sign-in service did not answer"


def _all_due(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock) -> None:
    for group in coordinator._poll_intervals:
        coordinator._next_update[group] = clock.now


async def _cycle(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock) -> None:
    clock.now += INTERVAL
    _all_due(coordinator, clock)
    await _refresh(coordinator)


def _sign_in_fails(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise SignInUnavailable("timeout", retry_after=30.0)

    coordinator.client.get_resources_bulk.side_effect = fail


def _gateway_fails(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise ServiceUnavailable(503)

    coordinator.client.get_resources_bulk.side_effect = fail
    coordinator.client.get_resource.side_effect = ServiceUnavailable(503)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == coordinator_module.__name__
    ]


# ---------------------------------------------------------------- NX-04


async def test_failing_sign_in_is_no_gateway_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    with _plant(hass, clock) as (coordinator, cloud):
        _sign_in_fails(coordinator)
        gets_before = len(cloud.gets)
        for _ in range(CIRCUIT_FAILURE_THRESHOLD + 2):
            await _cycle(coordinator, clock)
            assert not coordinator.last_update_success
        # No count for the gateway breaker, no protection pause, no fallback.
        assert coordinator._gateway_failure_count == 0
        assert coordinator.cloud_down_reason() == "cloud_unreachable"
        coordinator.client.get_resource.assert_not_called()
        assert len(cloud.gets) == gets_before
        report = coordinator.last_failed_refresh.diagnostics()
        assert report is not None
        assert report["category"] == "sign_in_unavailable"
        assert report["cycle_failed"] is True
        assert {item["category"] for item in report["paths"]} == {"sign_in_unavailable"}
        # One warning with its own text, none about the batch or the breaker.
        warnings = _messages(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert WARNING_TEXT in warnings[0]
        assert "gateway is not affected" in warnings[0]
        assert not any("batch refresh" in text for text in warnings)
        assert not any("circuit breaker" in text for text in warnings)
        assert any(WARNING_TEXT in text for text in _messages(caplog, logging.DEBUG))


async def test_sign_in_recovery_ends_the_outage_and_warns_again_later(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    caplog.set_level(logging.WARNING, logger=coordinator_module.__name__)
    with _plant(hass, clock) as (coordinator, _cloud):
        original = coordinator.client.get_resources_bulk.side_effect
        _sign_in_fails(coordinator)
        await _cycle(coordinator, clock)
        assert len(_messages(caplog, logging.WARNING)) == 1
        coordinator.client.get_resources_bulk.side_effect = original
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        _sign_in_fails(coordinator)
        await _cycle(coordinator, clock)
        assert len(_messages(caplog, logging.WARNING)) == 2


async def test_failing_gateway_still_opens_the_breaker(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The contrast of the sign-in test: a gateway error counts and falls back."""
    clock = Clock()
    caplog.set_level(logging.WARNING, logger=coordinator_module.__name__)
    with _plant(hass, clock) as (coordinator, _cloud):
        _gateway_fails(coordinator)
        for _ in range(CIRCUIT_FAILURE_THRESHOLD):
            await _cycle(coordinator, clock)
        assert coordinator._gateway_failure_count == CIRCUIT_FAILURE_THRESHOLD
        assert coordinator.cloud_down_reason() == "protection_pause"
        coordinator.client.get_resource.assert_called()
        report = coordinator.last_failed_refresh.diagnostics()
        assert report is not None
        assert report["category"] == "service_unavailable"
        warnings = _messages(caplog, logging.WARNING)
        assert any("batch refresh failed" in text for text in warnings)
        assert not any(WARNING_TEXT in text for text in warnings)


def test_sign_in_outage_is_its_own_transient_category() -> None:
    from custom_components.bosch_buderus_heating.discovery import error_category
    from custom_components.bosch_buderus_heating.discovery_inventory import (
        is_transient_result,
    )

    assert error_category(SignInUnavailable("timeout")) == "sign_in_unavailable"
    assert error_category(ServiceUnavailable(503)) == "service_unavailable"
    assert is_transient_result("sign_in_unavailable")
    assert coordinator_module._pointt_result(SignInUnavailable()) == (
        coordinator_module.SIGN_IN_UNAVAILABLE
    )
    assert coordinator_module._pointt_result(ServiceUnavailable()) == (
        "service_unavailable"
    )


# ------------------------------------------------------ X15, the sensor


async def test_sensor_follows_real_cycles(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        sensor = GatewayReachableBinarySensor(coordinator)
        original = coordinator.client.get_resources_bulk.side_effect
        await _cycle(coordinator, clock)
        sensor._evaluate()
        assert sensor.is_on is True
        assert sensor.extra_state_attributes == {
            "cloud": "reachable",
            "local": "not_in_use",
        }
        assert sensor.available is True

        # A failing sign-in says nothing about the gateway: unknown.
        _sign_in_fails(coordinator)
        await _cycle(coordinator, clock)
        sensor._evaluate()
        assert sensor.is_on is None
        assert sensor.extra_state_attributes == {
            "cloud": "sign_in_unavailable",
            "local": "not_in_use",
            "cloud_failure": "sign_in_unavailable",
        }
        assert sensor.available is True

        # A gateway that does not answer: off, with the failure category.
        _gateway_fails(coordinator)
        await _cycle(coordinator, clock)
        sensor._evaluate()
        assert sensor.is_on is False
        assert sensor.extra_state_attributes["cloud"] == "not_answering"
        assert sensor.extra_state_attributes["cloud_failure"] == ("service_unavailable")

        coordinator.client.get_resources_bulk.side_effect = original
        coordinator.client.get_resource.side_effect = None
        coordinator._circuit_open_until = 0.0
        await _cycle(coordinator, clock)
        sensor._evaluate()
        assert sensor.is_on is True


async def test_sensor_unique_id_and_registry_defaults(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        sensor = GatewayReachableBinarySensor(coordinator)
        assert sensor.unique_id == "gateway-1:gateway_reachable"
        assert sensor.entity_category is not None
        assert sensor.device_class == "connectivity"
        # A status sensor that sends nothing: enabled by default.
        assert sensor.entity_registry_enabled_default is True


@pytest.mark.parametrize(
    ("reason", "failure", "expected"),
    [
        (None, None, (True, "reachable")),
        (None, "service_unavailable", (True, "reachable")),
        ("auth_required", "timeout", (None, "sign_in_required")),
        ("cloud_stopped", None, (None, "not_in_use")),
        ("rate_limited", None, (None, "paused")),
        ("cloud_unreachable", "sign_in_unavailable", (None, "sign_in_unavailable")),
        ("protection_pause", "sign_in_unavailable", (None, "sign_in_unavailable")),
        ("cloud_unreachable", "timeout", (False, "not_answering")),
        ("cloud_unreachable", None, (False, "not_answering")),
        ("protection_pause", "service_unavailable", (False, "not_answering")),
    ],
)
def test_cloud_access_rules(reason, failure, expected) -> None:
    assert cloud_access(reason, failure) == expected


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        (None, (None, "not_in_use")),
        ((), (None, "not_in_use")),
        ((LocalCondition.READY,), (True, "ready")),
        ((LocalCondition.PAUSED,), (False, "paused")),
        ((LocalCondition.UNAVAILABLE,), (False, "not_ready")),
        ((LocalCondition.INITIALIZING,), (None, "starting")),
        ((LocalCondition.AUTH_REQUIRED,), (None, "sign_in_required")),
        ((LocalCondition.CERTIFICATE_CHANGED,), (None, "certificate_changed")),
        ((LocalCondition.PAUSED, LocalCondition.READY), (True, "ready")),
        ((LocalCondition.PAUSED, LocalCondition.UNAVAILABLE), (False, "not_ready")),
        ((LocalCondition.AUTH_REQUIRED, LocalCondition.PAUSED), (False, "not_ready")),
        ((None,), (None, "starting")),
    ],
)
def test_local_access_rules(conditions, expected) -> None:
    states = (
        None
        if conditions is None
        else tuple(SimpleNamespace(condition=item) for item in conditions)
    )
    assert local_access(states) == expected


@pytest.mark.parametrize(
    ("cloud", "local", "expected"),
    [
        (True, True, True),
        (True, False, True),
        (False, True, True),
        (False, False, False),
        (False, None, False),
        (None, False, False),
        (None, None, None),
        (True, None, True),
    ],
)
def test_the_accesses_combine(cloud, local, expected) -> None:
    assert combine(cloud, local) is expected


async def test_local_state_changes_update_the_sensor(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        listeners: list = []
        removed: list[bool] = []
        states = [SimpleNamespace(condition=LocalCondition.READY)]

        def subscribe(listener):
            listeners.append(listener)
            return lambda: removed.append(True)

        coordinator.local_selection = SimpleNamespace(
            local_states=lambda: tuple(states), subscribe_states=subscribe
        )
        coordinator.last_update_success = False
        coordinator._circuit_open_until = 0.0
        sensor = GatewayReachableBinarySensor(coordinator)
        sensor.hass = hass
        sensor.entity_id = "binary_sensor.test_gateway_reachable"
        sensor.async_write_ha_state = MagicMock()
        sensor._evaluate()
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["local"] == "ready"
        assert len(listeners) == 1

        # The same selection is not subscribed twice.
        sensor._evaluate()
        assert len(listeners) == 1

        states[:] = [SimpleNamespace(condition=LocalCondition.UNAVAILABLE)]
        listeners[0]()
        sensor.async_write_ha_state.assert_called_once()
        assert sensor.is_on is False
        assert sensor.extra_state_attributes["local"] == "not_ready"

        # An unchanged state writes nothing again.
        listeners[0]()
        sensor.async_write_ha_state.assert_called_once()

        await sensor.async_will_remove_from_hass()
        assert removed == [True]


# ---------------------------------------------------------------- NX-03


def test_pause_counter_counts_beyond_the_change_ring() -> None:
    counter = PauseCounter()
    assert counter.diagnostics() == {
        "pauses_total": 0,
        "pauses_by_reason": {},
        "last_pause": None,
    }
    at = datetime(2026, 11, 18, 18, 31, tzinfo=UTC)
    for _ in range(25):
        counter.record(at, "service_unavailable")
    counter.record(at.replace(minute=40), "rate_limited")
    counter.record(at.replace(minute=45), None)
    report = counter.diagnostics()
    assert report["pauses_total"] == 27
    assert report["pauses_by_reason"] == {
        "rate_limited": 1,
        "service_unavailable": 25,
        "unknown": 1,
    }
    assert report["last_pause"] == {
        "at": "2026-11-18T18:45:00+00:00",
        "reason": "unknown",
    }


def test_pause_counter_is_bounded_and_keeps_only_fixed_codes() -> None:
    counter = PauseCounter()
    at = datetime(2026, 11, 18, 18, 31, tzinfo=UTC)
    for number in range(MAX_REASONS + 5):
        counter.record(at, f"reason_{number}")
    counter.record(at, "Not A Code with spaces and 192.168.0.1")
    reasons = counter.diagnostics()["pauses_by_reason"]
    assert isinstance(reasons, dict)
    assert len(reasons) == MAX_REASONS + 1
    assert reasons["other"] == 6
    assert "192.168.0.1" not in repr(counter.diagnostics())


def test_local_source_counts_every_pause_and_the_ring_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = local_module.LocalSource(SimpleNamespace(closed=False), "local", None)
    source._note(LocalCondition.READY, None)
    for _ in range(local_module.CONDITION_CHANGES):
        source._note(LocalCondition.PAUSED, "service_unavailable")
        source._note(LocalCondition.READY, None)
    source._note(LocalCondition.PAUSED, "rate_limited")
    # A reason that changes during one pause is no new pause.
    source._note(LocalCondition.PAUSED, "service_unavailable")
    source._note(LocalCondition.READY, None)
    assert len(source._changes) == local_module.CONDITION_CHANGES
    report = source.diagnostics()
    assert report["pauses_total"] == local_module.CONDITION_CHANGES + 1
    assert report["pauses_by_reason"] == {
        "rate_limited": 1,
        "service_unavailable": local_module.CONDITION_CHANGES,
    }
    assert report["last_pause"]["reason"] == "rate_limited"
    # Other conditions are no pause.
    other = local_module.LocalSource(SimpleNamespace(closed=False), "local", None)
    other._note(LocalCondition.UNAVAILABLE, "timeout")
    other._note(LocalCondition.READY, None)
    assert other.diagnostics()["pauses_total"] == 0


# ---------------------------------------------------------------- NX-05


def _attempt(
    token: str, error: BaseException | None, *, duration: float = 1.0, outcome="failed"
) -> RefreshAttempt:
    return RefreshAttempt(
        trigger="expiry",
        outcome=outcome,
        started_at=1_800_000_000.0,
        duration=duration,
        access_token_remaining=0.0,
        presented_refresh_token=token,
        error=error,
    )


async def _recorder(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, data=tokens_to_data(AuthTokens("access-0", "refresh-0", 0))
    )
    entry.add_to_hass(hass)
    await async_load_auth_histories(hass, entry)
    recorder = loaded_auth_history(
        hass, entry.entry_id, legacy_cloud_connection_id(entry.entry_id)
    )
    assert recorder is not None
    recorder.credentials_loaded(AuthTokens("access-0", "refresh-0", 0), reason="start")
    return recorder


def _rejected() -> RefreshTokenRejected:
    return RefreshTokenRejected(
        rejection=TokenEndpointRejection(
            400, "invalid_grant", "Token is not active", "json"
        )
    )


async def test_rejection_after_failed_attempts_gets_a_hint_and_a_compact_list(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    recorder = await _recorder(hass)
    recorder.refresh_finished(
        _attempt("refresh-0", RequestTimeout("slow"), duration=90.0)
    )
    recorder.refresh_finished(
        _attempt(
            "refresh-0",
            RefreshRefused(503, rejection=TokenEndpointRejection(503)),
            duration=0.12,
        )
    )
    recorder.refresh_finished(_attempt("refresh-0", _rejected(), outcome="rejected"))

    event = recorder.diagnostics(None)["events"][-1]
    assert event["oauth_error"] == "invalid_grant"
    assert event["failed_before_rejection"] == [
        {
            "outcome": "failed",
            "error_type": "RequestTimeout",
            "duration_ms": 90000,
        },
        {
            "outcome": "failed",
            "error_type": "RefreshRefused",
            "http_status": 503,
            "duration_ms": 120,
        },
    ]
    assert event["failed_before_rejection_total"] == 2
    rejected = [r for r in caplog.records if "rejected the token refresh" in r.message]
    assert len(rejected) == 1
    text = rejected[0].getMessage()
    assert "without a usable answer" in text
    assert "RequestTimeout after 90000 ms" in text
    assert "RefreshRefused HTTP 503 after 120 ms" in text
    assert "signing in again is required" in text
    assert "refresh-0" not in text


async def test_rejection_without_earlier_failures_has_no_hint(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    recorder = await _recorder(hass)
    recorder.refresh_finished(_attempt("refresh-0", _rejected(), outcome="rejected"))
    event = recorder.diagnostics(None)["events"][-1]
    assert "failed_before_rejection" not in event
    assert "failed_before_rejection_total" not in event
    text = next(
        r.getMessage() for r in caplog.records if "rejected the token" in r.message
    )
    assert "without a usable answer" not in text


async def test_failed_attempts_are_bounded_and_survive_a_restart(
    hass: HomeAssistant,
) -> None:
    recorder = await _recorder(hass)
    for number in range(MAX_FAILED_ATTEMPTS + 3):
        recorder.refresh_finished(
            _attempt("refresh-0", RequestTimeout("slow"), duration=number)
        )
    recorder.refresh_finished(_attempt("refresh-0", _rejected(), outcome="rejected"))
    event = recorder.diagnostics(None)["events"][-1]
    assert len(event["failed_before_rejection"]) == MAX_FAILED_ATTEMPTS
    assert event["failed_before_rejection_total"] == MAX_FAILED_ATTEMPTS + 3
    # The newest ones are kept.
    assert event["failed_before_rejection"][-1]["duration_ms"] == (
        (MAX_FAILED_ATTEMPTS + 2) * 1000
    )
    # Damaged stored values are ignored, only safe fields are exported.
    snapshot = recorder._snapshot()
    snapshot["issued"][0]["failed"] = [
        "text",
        {"error_type": "Bad Value With Spaces", "duration_ms": True, "secret": "x"},
        {"outcome": "failed", "http_status": 503},
    ]
    restored = type(recorder)(recorder.store, snapshot)
    restored.refresh_finished(_attempt("refresh-0", _rejected(), outcome="rejected"))
    last = restored.diagnostics(None)["events"][-1]
    assert last["failed_before_rejection"] == [
        {"error_type": "other"},
        {"outcome": "failed", "http_status": 503},
    ]
    assert "secret" not in repr(last)


async def test_another_token_does_not_inherit_the_failed_attempts(
    hass: HomeAssistant,
) -> None:
    recorder = await _recorder(hass)
    recorder.refresh_finished(_attempt("refresh-0", RequestTimeout("slow")))
    recorder.credentials_loaded(AuthTokens("access-1", "refresh-1", 0), reason="next")
    recorder.refresh_finished(_attempt("refresh-1", _rejected(), outcome="rejected"))
    event = recorder.diagnostics(None)["events"][-1]
    assert "failed_before_rejection" not in event


# --------------------------------------------- cloud status after long outages


async def test_rejected_sign_in_after_a_long_outage_reaches_the_sensor_by_listener(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Home Assistant skips the listeners after consecutive failures and plans
    no refresh after an authentication error: the coordinator announces the
    rejection itself, the sensor needs no manual evaluation."""
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        sensor = GatewayReachableBinarySensor(coordinator)
        remove = coordinator.async_add_listener(sensor._evaluate)
        _gateway_fails(coordinator)
        for _ in range(8):
            await _cycle(coordinator, clock)
        assert sensor.is_on is False
        assert sensor.extra_state_attributes["cloud"] == "not_answering"

        async def rejected(*_args: object, **_kwargs: object) -> None:
            raise _rejected()

        coordinator.client.get_resources_bulk.side_effect = rejected
        coordinator._circuit_open_until = 0.0
        await _cycle(coordinator, clock)
        assert coordinator.sign_in_rejected is True
        assert sensor.is_on is None
        assert sensor.extra_state_attributes["cloud"] == "sign_in_required"
        remove()


async def test_failed_discovery_names_its_own_category_not_an_older_one(
    hass: HomeAssistant,
) -> None:
    """The report of an earlier outage must not explain a failed discovery."""
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        sensor = GatewayReachableBinarySensor(coordinator)
        original = coordinator.client.get_resources_bulk.side_effect
        _sign_in_fails(coordinator)
        await _cycle(coordinator, clock)
        coordinator.client.get_resources_bulk.side_effect = original
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        # Now a discovery (rescan, firmware change) fails first.
        with (
            patch.object(
                coordinator_module,
                "async_discover_resources",
                AsyncMock(side_effect=ServiceUnavailable(503)),
            ),
            pytest.raises(UpdateFailed),
        ):
            await coordinator._async_discover_data()
        coordinator.last_update_success = False
        sensor._evaluate()
        assert sensor.is_on is False
        assert sensor.extra_state_attributes["cloud"] == "not_answering"
        assert sensor.extra_state_attributes["cloud_failure"] == "service_unavailable"


def test_every_cloud_failure_category_is_translated_in_all_languages() -> None:
    import json
    from pathlib import Path

    from custom_components.bosch_buderus_heating.gateway_reachable import (
        CLOUD_FAILURE_CATEGORIES,
    )

    folder = Path(coordinator_module.__file__).parent
    files = [folder / "strings.json", *sorted((folder / "translations").glob("*.json"))]
    assert len(files) == 20
    for path in files:
        state = json.loads(path.read_text(encoding="utf-8"))["entity"]["binary_sensor"][
            "gateway_reachable"
        ]["state_attributes"]["cloud_failure"]["state"]
        assert set(state) == set(CLOUD_FAILURE_CATEGORIES), path.name
        assert all(isinstance(text, str) and text for text in state.values())


def test_cloud_failure_categories_cover_everything_a_cycle_can_report() -> None:
    from custom_components.bosch_buderus_heating.gateway_reachable import (
        CLOUD_FAILURE_CATEGORIES,
    )

    reported = {
        coordinator_module._result_category(error, status)
        for error in (
            None,
            SignInUnavailable("x"),
            ServiceUnavailable(503),
            RequestTimeout("x"),
            RefreshTokenRejected(
                rejection=TokenEndpointRejection(400, "x", "y", "json")
            ),
        )
        for status in (None, 200, 403, 404, 429, 500, 502, 503, 504)
    }
    reported -= {"success", "http_500"}
    assert reported <= set(CLOUD_FAILURE_CATEGORIES)
    assert coordinator_module._pointt_result(None) in CLOUD_FAILURE_CATEGORIES
    assert {"rate_limited", "forbidden", "not_found"} <= set(CLOUD_FAILURE_CATEGORIES)


async def test_an_unknown_failure_category_shows_as_unknown(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, _cloud):
        sensor = GatewayReachableBinarySensor(coordinator)
        coordinator.last_failed_refresh.cycle_failure("something_new")
        coordinator.last_update_success = False
        sensor._evaluate()
        assert sensor.extra_state_attributes["cloud_failure"] == "unknown"
