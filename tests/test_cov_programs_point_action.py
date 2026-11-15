"""The action ``set_switch_points``: how failures of the write reach the caller
(translated, value-free) and that none of them sends a second request."""

# Fixtures are imported from the module that defines them.
# ruff: noqa: F811

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramChanged,
)
from custom_components.bosch_buderus_heating.writes import (
    ContractRejected,
    ControlUnused,
    WriteBusy,
)
from tests.test_switch_points_service import (  # noqa: F401 - fixtures and helpers
    WEEK,
    environment,
    gateway,
    run,
)


@pytest.mark.parametrize(
    ("error", "kind", "key"),
    [
        (
            SwitchProgramChanged(),
            ServiceValidationError,
            "switch_points_program_changed",
        ),
        (ControlUnused("unused"), ServiceValidationError, "switch_points_unused"),
        (
            ContractRejected("circuit_breaker"),
            HomeAssistantError,
            "switch_points_paused",
        ),
        (
            ContractRejected("program_mode_absolute"),
            ServiceValidationError,
            "switch_points_not_level_program",
        ),
        (
            ContractRejected("program_changed"),
            ServiceValidationError,
            "switch_points_program_changed",
        ),
        (
            WriteValidationError("refused"),
            ServiceValidationError,
            "switch_points_program_changed",
        ),
        (WriteNotConfirmed("n"), HomeAssistantError, "switch_points_not_confirmed"),
        (WriteBusy("b"), HomeAssistantError, "write_busy"),
        (AuthenticationError("a"), HomeAssistantError, "write_authentication_failed"),
        (RateLimited(retry_after=30), HomeAssistantError, "write_rate_limited"),
        (UnexpectedHttpStatus(409), HomeAssistantError, "switch_points_rejected"),
        (PointTError("p"), HomeAssistantError, "write_failed"),
    ],
)
async def test_failures_of_the_write_are_translated(gateway, error, kind, key):
    gateway.coordinator.async_plan_switch_program = AsyncMock(side_effect=error)
    with pytest.raises(kind) as raised:
        await run(gateway, switch_points=WEEK)
    assert raised.value.translation_key == key
    assert gateway.puts == 0


async def test_a_rejected_status_is_named_in_the_error(gateway):
    gateway.coordinator.async_plan_switch_program = AsyncMock(
        side_effect=UnexpectedHttpStatus(409)
    )
    with pytest.raises(HomeAssistantError) as raised:
        await run(gateway, switch_points=WEEK)
    assert raised.value.translation_placeholders == {"status": "409"}


async def test_a_refusal_while_the_cloud_is_down_names_the_outage_not_the_program(
    gateway,
):
    from time import monotonic

    async def refuse(path, plan):
        # The pause starts after the first check: the write is refused then.
        gateway.coordinator._cloud_backoff_until = monotonic() + 600
        raise WriteValidationError("cloud paused")

    gateway.coordinator.async_plan_switch_program = refuse
    with pytest.raises(HomeAssistantError) as raised:
        await run(gateway, switch_points=WEEK)
    assert raised.value.translation_key.startswith("command_cloud_")
    assert gateway.puts == 0
