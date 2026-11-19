"""Access status and the explicit connection test: accesses that vanished,
sources that cannot test, and each answer a LAN source can give."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import connection_status as status
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.pointt.exceptions import SourceStopped
from tests.test_connection_status import (
    CLOUD,
    LAN,
    Runtime,
    entry_with,
    ready_local,
)


async def test_an_unknown_access_is_not_loaded_and_never_tested(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass)
    entry = entry_with(runtime)
    assert status.access_status(entry, "removed").state == "not_loaded"
    assert await status.test_connection(hass, entry, "removed") == ("not_running", {})


async def test_an_entry_that_does_not_run_has_no_status_and_no_pauses(
    hass: HomeAssistant,
) -> None:
    entry = SimpleNamespace(runtime_data=None, options={})
    assert status.access_status(entry, LAN).state == "not_loaded"
    assert status.local_pause_reasons(entry, LAN) == frozenset()
    assert await status.test_connection(hass, entry, LAN) == ("not_running", {})


async def test_pause_reasons_come_only_from_the_access_own_sources(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass, local=ready_local())
    runtime.snapshot.bindings["b-other"] = SimpleNamespace(
        connection_id=CLOUD, target_id="t"
    )
    runtime.local_driver.pause_reason = "rate_limited"
    entry = entry_with(runtime)
    assert status.local_pause_reasons(entry, LAN) == {"rate_limited"}
    # A reason that is not a fixed code string is ignored.
    runtime.local_driver.pause_reason = 5
    assert status.local_pause_reasons(entry, LAN) == frozenset()


async def test_a_cloud_access_without_a_client_is_not_tested(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass)
    runtime.cloud_driver = SimpleNamespace(backoff=SimpleNamespace(remaining=0.0))
    assert await status.test_connection(hass, entry_with(runtime), CLOUD) == (
        "not_running",
        {},
    )


async def test_a_stopped_cloud_source_is_not_running_rather_than_an_error(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass)
    runtime.client.get_gateways.side_effect = SourceStopped()
    assert await status.test_connection(hass, entry_with(runtime), CLOUD) == (
        "not_running",
        {},
    )


async def test_a_lan_access_with_no_testable_source_is_not_running(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass, local=ready_local())
    runtime.local_driver = SimpleNamespace()  # no test_connection
    entry = entry_with(runtime)
    assert await status.test_connection(hass, entry, LAN) == ("not_running", {})
    # A source of another access is not asked for this one.
    runtime.snapshot.bindings.clear()
    assert await status.test_connection(hass, entry, LAN) == ("not_running", {})


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (("auth_required", 3), ("auth_local", {})),
        (("busy", 7), ("busy", {"ms": "7"})),
        (("certificate_changed", 9), ("certificate_changed", {"ms": "9"})),
        (("unreachable", 40), ("unreachable_local", {"ms": "40"})),
    ],
)
async def test_each_answer_of_a_lan_source_has_its_outcome(
    hass: HomeAssistant, answer, outcome
) -> None:
    runtime = Runtime(hass, local=ready_local())
    runtime.local_driver.test_connection.return_value = answer
    assert await status.test_connection(hass, entry_with(runtime), LAN) == outcome


async def test_a_source_that_stopped_while_the_access_is_unreachable_is_unreachable(
    hass: HomeAssistant,
) -> None:
    local = ready_local()
    local.condition = LocalCondition.UNAVAILABLE
    local.problem = None
    runtime = Runtime(hass, local=local)
    runtime.local_driver.test_connection.return_value = ("not_running", 0)
    code, values = await status.test_connection(hass, entry_with(runtime), LAN)
    assert code == "unreachable_local"
    assert values == {"ms": "0"}


async def test_a_pause_that_starts_during_the_test_is_reported_as_paused(
    hass: HomeAssistant,
) -> None:
    runtime = Runtime(hass, local=ready_local())
    runtime.local_driver.test_connection.return_value = ("paused", 5)
    code, values = await status.test_connection(hass, entry_with(runtime), LAN)
    # The access shows no end of the pause: an open pause.
    assert code == "paused_open"
    assert values == {}
