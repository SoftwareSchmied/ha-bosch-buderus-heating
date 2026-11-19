"""Holiday writes through the coordinator: nothing is sent while the data is
not current, the cloud pauses or the circuit is open, and each failure of the
gateway is recorded and handled the right way."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HolidayWriteValues,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    ServiceUnavailable,
    WriteValidationError,
)
from tests.test_coordinator import _coordinator, _holiday_resources, _snapshot

VALUES = HolidayWriteValues(
    start_date="2030-08-01T08:00:00",
    end_date="2030-08-08T18:00:00",
    heating_mode="FIX_TEMPERATURE",
    dhw_mode="OFF",
    ventilation_mode=None,
    assigned_to=("hc1", "dhw1"),
    name=None,
    thermal_disinfection=None,
    fix_temperature=17.0,
)


def _ready(hass: HomeAssistant):
    coordinator = _coordinator(hass, AsyncMock())
    periods, configuration = _holiday_resources()
    coordinator.resources = {r.path: r for r in (periods, configuration)}
    coordinator.data = {r.path: _snapshot(r) for r in (periods, configuration)}
    coordinator.last_update_success = True
    writer = coordinator._holiday_write_service = AsyncMock()
    return coordinator, writer, periods, configuration


async def test_a_paused_cloud_sends_no_holiday_write(hass: HomeAssistant) -> None:
    coordinator, writer, *_ = _ready(hass)
    coordinator._cloud_backoff_until = monotonic() + 600
    with pytest.raises(RateLimited):
        await coordinator.async_create_holiday(VALUES)
    writer.async_create.assert_not_awaited()


async def test_an_open_circuit_sends_no_holiday_write(hass: HomeAssistant) -> None:
    coordinator, writer, *_ = _ready(hass)
    coordinator._circuit_open_until = monotonic() + 600
    with pytest.raises(WriteValidationError, match="circuit breaker"):
        await coordinator.async_delete_holiday(7)
    writer.async_delete.assert_not_awaited()


@pytest.mark.parametrize("missing", [HOLIDAY_LIST_PATH, HOLIDAY_CONFIGURATION_PATH])
@pytest.mark.parametrize("problem", ["absent", "unavailable", "stale"])
async def test_holiday_data_that_is_not_current_is_never_written_over(
    hass: HomeAssistant, missing, problem
) -> None:
    coordinator, writer, *_ = _ready(hass)
    snapshot = coordinator.data[missing]
    if problem == "absent":
        del coordinator.data[missing]
    elif problem == "unavailable":
        coordinator.data[missing] = replace(snapshot, available=False)
    else:
        coordinator.data[missing] = replace(snapshot, freshness=Freshness.STALE)
    with pytest.raises(WriteValidationError, match="unavailable or not current"):
        await coordinator.async_create_holiday(VALUES)
    writer.async_create.assert_not_awaited()


async def test_a_gateway_without_a_complete_holiday_schema_is_refused(
    hass: HomeAssistant,
) -> None:
    coordinator, writer, _periods, configuration = _ready(hass)
    empty = replace(configuration, value={"values": {}})
    coordinator.resources[empty.path] = empty
    coordinator.data[empty.path] = _snapshot(empty)
    with pytest.raises(WriteValidationError, match="complete holiday calendar"):
        await coordinator.async_create_holiday(VALUES)
    writer.async_create.assert_not_awaited()


async def test_a_request_without_the_values_it_needs_is_refused(
    hass: HomeAssistant,
) -> None:
    coordinator, writer, *_ = _ready(hass)
    with pytest.raises(WriteValidationError, match="Invalid holiday write request"):
        await coordinator._async_write_holiday("update", None, None)
    with pytest.raises(WriteValidationError, match="Invalid holiday write request"):
        await coordinator._async_write_holiday("create", None, None)
    for call in (writer.async_create, writer.async_update, writer.async_delete):
        call.assert_not_awaited()


async def test_a_rate_limit_answer_pauses_holiday_writes(hass: HomeAssistant) -> None:
    coordinator, writer, *_ = _ready(hass)
    writer.async_create.side_effect = RateLimited(900)
    with pytest.raises(RateLimited):
        await coordinator.async_create_holiday(VALUES)
    metrics = coordinator.capability_metrics(HOLIDAY_LIST_PATH)
    assert metrics["attempts_by_source"] == {"write": 1}
    assert metrics["successful"] == 0
    # The pause now holds the next write back before anything is sent.
    assert coordinator._cloud_backoff_until > monotonic() + 800
    with pytest.raises(RateLimited):
        await coordinator.async_create_holiday(VALUES)
    writer.async_create.assert_awaited_once()


async def test_a_refused_sign_in_asks_for_a_new_one_and_nothing_else(
    hass: HomeAssistant,
) -> None:
    coordinator, writer, *_ = _ready(hass)
    writer.async_update.side_effect = AuthenticationError("expired")
    coordinator._request_reauthentication = Mock()  # type: ignore[method-assign]
    with pytest.raises(AuthenticationError):
        await coordinator.async_update_holiday(7, VALUES)
    coordinator._request_reauthentication.assert_called_once()
    assert coordinator.capability_metrics(HOLIDAY_LIST_PATH)["successful"] == 0
    # The list is not replaced by a guess.
    assert coordinator.data[HOLIDAY_LIST_PATH].freshness is not Freshness.STALE


async def test_a_gateway_error_is_recorded_and_the_error_reaches_the_caller(
    hass: HomeAssistant,
) -> None:
    coordinator, writer, _periods, _ = _ready(hass)
    before = coordinator.data[HOLIDAY_LIST_PATH]
    writer.async_delete.side_effect = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        await coordinator.async_delete_holiday(7)
    assert coordinator.capability_metrics(HOLIDAY_LIST_PATH)["attempts_by_source"] == {
        "write": 1
    }
    assert coordinator.data[HOLIDAY_LIST_PATH] is before
    assert isinstance(before, ResourceSnapshot)


async def test_a_validation_problem_or_busy_write_is_passed_on_unrecorded(
    hass: HomeAssistant,
) -> None:
    coordinator, writer, *_ = _ready(hass)
    writer.async_create.side_effect = WriteValidationError("invalid")
    with pytest.raises(WriteValidationError, match="invalid"):
        await coordinator.async_create_holiday(VALUES)
    assert coordinator.capability_metrics(HOLIDAY_LIST_PATH)["attempts_by_source"] == {}


# ------------------------------------------------ refused write targets


def _resource(path: str):
    from custom_components.bosch_buderus_heating.pointt import (
        Resource,
        ResourceMetadata,
    )

    return Resource(
        path=path, value={}, has_value=True, metadata=ResourceMetadata(writable=True)
    )


async def test_writes_to_paths_that_are_no_program_are_refused_unsent(
    hass: HomeAssistant,
) -> None:
    client = AsyncMock()
    coordinator = _coordinator(hass, client)
    other = _resource("/heatingCircuits/hc1/manualRoomSetpoint")
    for call, args in (
        (coordinator.async_edit_rrc_program, (other, 0, 0, 20.0)),
        (coordinator.async_write_rrc_day, (other, "mo", [])),
        (coordinator.async_write_program_name, (other, "Name")),
        (coordinator.async_write_switch_program, (other, ())),
    ):
        with pytest.raises(WriteValidationError, match="Unsupported"):
            await call(*args)
    with pytest.raises(WriteValidationError, match="Unsupported switch program"):
        await coordinator.async_plan_switch_program(other.path, lambda *a: None)
    # Nothing was read or written for any of them.
    assert client.mock_calls == []


# ------------------------------------------------------------- rescans


async def test_a_rescan_is_planned_only_when_the_cloud_answers_and_is_free(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, AsyncMock())
    coordinator.last_update_success = False
    assert coordinator.rescan_state() == "unavailable"
    assert coordinator.request_rescan() == "unavailable"
    assert coordinator._rescan_due is None
    coordinator.last_update_success = True
    coordinator._cloud_backoff_until = monotonic() + 600
    assert coordinator.request_rescan() == "paused"
    assert coordinator._rescan_due is None
    coordinator._cloud_backoff_until = 0.0
    coordinator._circuit_open_until = monotonic() + 600
    assert coordinator.rescan_state() == "paused"
    coordinator._circuit_open_until = 0.0
    assert coordinator.request_rescan() == "planned"
    assert coordinator.rescan_result == "planned"
    # A second request while one is planned does not start another.
    due = coordinator._rescan_due
    assert coordinator.request_rescan() == "running"
    assert coordinator._rescan_due == due
