"""P21: state of each access in the dialog and the explicit connection test."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import connection_status as status
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.pointt import (
    AccessTokenRejected,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceMetadata,
)
from tests.test_validity import _coordinator

CLOUD, LAN = "cloud", "lan"


class Runtime:
    def __init__(
        self, hass, *, cloud_phase=ConnectionPhase.READY, local=None, paused=0.0
    ):
        self.snapshot = SimpleNamespace(
            connections={
                CLOUD: SimpleNamespace(
                    kind="singlekey_cloud", to_data=lambda: {"brand": "bosch"}
                ),
                LAN: SimpleNamespace(
                    kind="k40_local",
                    to_data=lambda: {"endpoint": {"host": "192.0.2.10"}},
                ),
            },
            bindings={
                "b-cloud": SimpleNamespace(connection_id=CLOUD, target_id="t"),
                "b-lan": SimpleNamespace(connection_id=LAN, target_id="t"),
            },
        )
        self.phases = {CLOUD: cloud_phase, LAN: ConnectionPhase.READY}
        self.source_phases = {}
        self.client = SimpleNamespace(get_gateways=AsyncMock(return_value=("a", "b")))
        self.cloud_driver = SimpleNamespace(
            backoff=SimpleNamespace(remaining=paused), client=self.client
        )
        self.coordinator = _coordinator(
            hass,
            Resource(
                "/heatSources/returnTemperature",
                value=30.0,
                has_value=True,
                metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
            ),
        )
        self.local = local
        self.local_driver = SimpleNamespace(
            test_connection=AsyncMock(return_value=("ok", 12))
        )

    def connection_phase(self, key):
        return self.phases[key]

    def source_phase(self, key):
        return self.source_phases.get(key, ConnectionPhase.READY)

    def connection_driver(self, key):
        return self.cloud_driver if key == CLOUD else None

    def source_data(self, key):
        return self.coordinator if key == "b-cloud" else None

    def local_state(self, key):
        return self.local if key == "b-lan" else None

    def source_driver(self, key):
        return self.local_driver if key == "b-lan" else None


def entry_with(runtime, *, local_values=True, cloud_enabled=True):
    owner = ManagedEntry.__new__(ManagedEntry)
    owner.connections = runtime
    return SimpleNamespace(
        runtime_data=owner,
        options={
            "connections": {
                CLOUD: {"enabled": cloud_enabled, "polling_profile": "cloud_friendly"},
                LAN: {"enabled": True, "local_interval": 60},
            },
            "targets": {"t": {"local_values": local_values}},
        },
    )


def ready_local():
    return SimpleNamespace(
        condition=LocalCondition.READY,
        current_observations=[SimpleNamespace(observed_at=datetime.now(UTC))],
        resources=[SimpleNamespace(available=True), SimpleNamespace(available=False)],
    )


def label(hass, entry, key, number):
    return status.connection_label(
        hass,
        entry,
        key,
        entry.runtime_data.connections.snapshot.connections[key],
        number,
    )


# ------------------------------------------------------- 1A: state shown


async def test_labels_name_kind_and_state_but_no_brand(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, local=ready_local())
    entry = entry_with(runtime)
    assert label(hass, entry, CLOUD, 1) == "Cloud-Zugang (1): verbunden"
    assert label(hass, entry, LAN, 2) == "LAN · 192.0.2.10 (2): verbunden"
    assert "bosch" not in label(hass, entry, CLOUD, 1).casefold()
    hass.config.language = "en"
    assert label(hass, entry, CLOUD, 1) == "Cloud access (1): connected"
    runtime.client.get_gateways.assert_not_awaited()  # opening never requests


@pytest.mark.parametrize(
    ("phase", "paused", "enabled", "expected"),
    [
        (ConnectionPhase.AUTH_REQUIRED, 0.0, True, "Anmeldung nötig"),
        (ConnectionPhase.READY, 600.0, True, "pausiert bis "),
        (ConnectionPhase.UNAVAILABLE, 0.0, True, "nicht erreichbar"),
        (ConnectionPhase.CONNECTING, 0.0, True, "startet"),
        (ConnectionPhase.READY, 0.0, False, "deaktiviert"),
    ],
)
async def test_cloud_states(
    hass: HomeAssistant, phase, paused, enabled, expected
) -> None:
    hass.config.language = "de"
    entry = entry_with(
        Runtime(hass, cloud_phase=phase, paused=paused), cloud_enabled=enabled
    )
    assert expected in label(hass, entry, CLOUD, 1)


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (LocalCondition.PAUSED, "pausiert"),
        (LocalCondition.AUTH_REQUIRED, "Anmeldung nötig"),
        (LocalCondition.CERTIFICATE_CHANGED, "Zertifikat geändert"),
        (LocalCondition.UNAVAILABLE, "nicht erreichbar"),
    ],
)
async def test_local_states(hass: HomeAssistant, condition, expected) -> None:
    hass.config.language = "de"
    local = ready_local()
    local.condition = condition
    entry = entry_with(Runtime(hass, local=local))
    assert expected in label(hass, entry, LAN, 2)


@pytest.mark.parametrize(
    ("problem", "expected"),
    [("resource_forbidden", "Zugriff verweigert"), ("timeout", "nicht erreichbar")],
)
async def test_a_refused_local_access_is_shown_as_denied(
    hass: HomeAssistant, problem, expected
) -> None:
    """P39: the gateway answers, but refuses access."""
    hass.config.language = "de"
    local = ready_local()
    local.condition = LocalCondition.UNAVAILABLE
    local.problem = problem
    entry = entry_with(Runtime(hass, local=local))
    assert expected in label(hass, entry, LAN, 2)


async def test_not_loaded_without_a_running_owner(hass: HomeAssistant) -> None:
    hass.config.language = "en"
    entry = SimpleNamespace(runtime_data=None, options={})
    connection = SimpleNamespace(kind="singlekey_cloud", to_data=dict)
    assert status.connection_label(hass, entry, CLOUD, connection, 1) == (
        "Cloud access (1): not loaded"
    )


async def test_menu_description_from_existing_state(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, local=ready_local())
    entry = entry_with(runtime)
    cloud = status.connection_description(hass, entry, CLOUD)
    assert cloud.startswith("Zustand: verbunden.")
    assert "Letzte erfolgreiche Cloud-Abfrage:" in cloud and "noch keine" not in cloud
    assert "Abfrageprofil: Cloud-schonend." in cloud
    lan = status.connection_description(hass, entry, LAN)
    assert "Lokal verfügbare Werte: 1 von 2." in lan
    assert "Zielintervall für Livewerte: 60 s." in lan
    assert "Lokale Werte für vorhandene Entitäten: ein." in lan
    runtime.client.get_gateways.assert_not_awaited()


# --------------------------------------------------- 2A: explicit test


async def test_cloud_test_reads_once_and_reports(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass)
    result = await status.test_connection(hass, entry_with(runtime), CLOUD)
    assert result.startswith("In Ordnung") and "2 Gateway(s)" in result
    runtime.client.get_gateways.assert_awaited_once()


async def test_cloud_test_sends_nothing_during_a_pause(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, paused=900.0)
    result = await status.test_connection(hass, entry_with(runtime), CLOUD)
    assert result.startswith("Nicht getestet: Abfragen sind pausiert bis")
    runtime.client.get_gateways.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AccessTokenRejected("x"), "Anmeldung nötig"),
        (RateLimited(120), "pausiert"),
        (RequestTimeout("x"), "Nicht erreichbar"),
    ],
)
async def test_cloud_test_failures(hass: HomeAssistant, error, expected) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass)
    runtime.client.get_gateways.side_effect = error
    assert expected in await status.test_connection(hass, entry_with(runtime), CLOUD)


async def test_local_test_uses_the_running_source(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, local=ready_local())
    entry = entry_with(runtime)
    assert (await status.test_connection(hass, entry, LAN)).startswith(
        "In Ordnung, Antwort nach 12 ms."
    )
    runtime.local_driver.test_connection.return_value = ("other_gateway", 30)
    assert "anderes Gateway" in await status.test_connection(hass, entry, LAN)


async def test_no_test_for_a_disabled_access(hass: HomeAssistant) -> None:
    hass.config.language = "en"
    runtime = Runtime(hass)
    result = await status.test_connection(
        hass, entry_with(runtime, cloud_enabled=False), CLOUD
    )
    assert result.startswith("Not tested: the access is not running")
    runtime.client.get_gateways.assert_not_awaited()


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        (None, "ok"),
        ("local_identity_conflict", "other_gateway"),
        ("auth_required", "auth_required"),
        ("certificate_changed", "certificate_changed"),
        ("paused", "paused"),
        ("request_timeout", "unreachable"),
        ("busy", "busy"),
    ],
)
async def test_local_source_maps_read_results(problem, expected) -> None:
    from custom_components.bosch_buderus_heating.local_read import ReadResult, ReadState
    from custom_components.bosch_buderus_heating.managed_local import LocalSource

    binding = SimpleNamespace(
        to_data=lambda: {"locator": {"namespace": "k40_local", "value": "gw"}}
    )
    source = LocalSource(SimpleNamespace(closed=False), "b", binding)
    assert await source.test_connection() == ("not_running", 0)
    source.context = SimpleNamespace(configuration_current=True)

    async def read(request):
        state = ReadState.SUCCESS if problem is None else ReadState.ACCESS_ERROR
        return ReadResult(request, state, problem=problem)

    source.client = SimpleNamespace(read=read)
    result, millis = await source.test_connection()
    assert result == expected and millis >= 0


# -------------------------------------------------- 4A: debug logging


async def test_source_change_is_logged_at_debug_level(caplog) -> None:
    from custom_components.bosch_buderus_heating.local_selection import LocalSelection
    from tests.test_local_selection import RETURN, TARGET, FakeRuntime, cloud, local

    runtime = FakeRuntime([local(RETURN, 31.5)])
    selection = LocalSelection(runtime, TARGET)
    snapshot = cloud(RETURN, 30.0)
    caplog.set_level(
        logging.DEBUG, "custom_components.bosch_buderus_heating.local_selection"
    )
    selection.select(RETURN, snapshot)
    runtime.observations = []
    selection.select(RETURN, snapshot)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Paired reading return_temperature is now shown from the cloud access"
    ]
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
    assert "30" not in messages[0] and "31" not in messages[0]


def test_times_show_the_date_when_not_today(freezer) -> None:
    # D48: a fixed time, so the first minute after midnight never fails.
    # 11:00 UTC stays on one local date for every offset from -12 to +14 h.
    freezer.move_to("2026-11-10T11:00:00+00:00")
    assert len(status._clock(datetime.now(UTC) - timedelta(minutes=1))) == 5
    assert len(status._clock(datetime.now(UTC) - timedelta(days=2))) == 16


# ------------------------------------------------------ review follow-ups


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("source_unavailable", "nicht erreichbar"),
        ("coordinator_failed", "nicht erreichbar"),
        ("all_disabled", "keiner Anlage zugeordnet"),
        ("idle", "keiner Anlage zugeordnet"),
        ("no_coordinator", "startet"),
    ],
)
async def test_cloud_state_follows_its_appliances(
    hass: HomeAssistant, change, expected
) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass)
    if change == "source_unavailable":
        runtime.source_phases["b-cloud"] = ConnectionPhase.UNAVAILABLE
    elif change == "coordinator_failed":
        runtime.coordinator.last_update_success = False
    elif change == "all_disabled":
        runtime.source_phases["b-cloud"] = ConnectionPhase.DISABLED
    elif change == "idle":
        runtime.phases[CLOUD] = ConnectionPhase.IDLE
    else:
        runtime.coordinator = None
    assert expected in label(hass, entry_with(runtime), CLOUD, 1)


async def test_cloud_last_success_survives_a_failing_read(hass: HomeAssistant) -> None:
    from dataclasses import replace

    hass.config.language = "de"
    runtime = Runtime(hass)
    for path, item in tuple(runtime.coordinator.data.items()):
        runtime.coordinator.data[path] = replace(item, available=False)
    text = status.connection_description(hass, entry_with(runtime), CLOUD)
    assert "noch keine" not in text


async def test_cloud_test_names_known_causes_without_a_request(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.pointt.exceptions import SourceStopped

    hass.config.language = "de"
    runtime = Runtime(hass, cloud_phase=ConnectionPhase.AUTH_REQUIRED)
    result = await status.test_connection(hass, entry_with(runtime), CLOUD)
    assert "Erneut anmelden" in result
    runtime.client.get_gateways.assert_not_awaited()
    runtime = Runtime(hass)
    runtime.client.get_gateways.side_effect = SourceStopped()
    assert (await status.test_connection(hass, entry_with(runtime), CLOUD)).startswith(
        "Nicht getestet: Der Zugang läuft gerade nicht"
    )


async def test_rate_limit_without_retry_after_uses_the_pause(
    hass: HomeAssistant,
) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass)

    async def limited():
        runtime.cloud_driver.backoff.remaining = 600.0
        raise RateLimited(None)

    runtime.client.get_gateways.side_effect = limited
    result = await status.test_connection(hass, entry_with(runtime), CLOUD)
    assert result.startswith("Nicht getestet: Abfragen sind pausiert bis")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (LocalCondition.AUTH_REQUIRED, "Lokalen Zugang ändern"),
        (LocalCondition.CERTIFICATE_CHANGED, "Zertifikat des Geräts"),
        (LocalCondition.PAUSED, "pausiert bis"),
    ],
)
async def test_local_test_names_known_causes_without_a_request(
    hass: HomeAssistant, condition, expected
) -> None:
    hass.config.language = "de"
    local = ready_local()
    local.condition = condition
    runtime = Runtime(hass, local=local)
    runtime.local_driver.service = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(gateway_pause_seconds=300.0)
    )
    result = await status.test_connection(hass, entry_with(runtime), LAN)
    assert expected in result
    runtime.local_driver.test_connection.assert_not_awaited()


async def test_local_state_before_the_first_publication(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, local=None)
    assert "startet" in label(hass, entry_with(runtime), LAN, 2)
    runtime.source_phases["b-lan"] = ConnectionPhase.UNAVAILABLE
    assert "nicht erreichbar" in label(hass, entry_with(runtime), LAN, 2)


async def test_local_last_answer_also_while_paused(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    local = ready_local()
    local.condition = LocalCondition.PAUSED
    local.last_observations = local.current_observations
    local.current_observations = []
    text = status.connection_description(
        hass, entry_with(Runtime(hass, local=local)), LAN
    )
    assert "noch keine" not in text


async def test_local_test_survives_a_closed_source() -> None:
    import asyncio

    from custom_components.bosch_buderus_heating.managed_local import LocalSource

    binding = SimpleNamespace(
        to_data=lambda: {"locator": {"namespace": "k40_local", "value": "gw"}}
    )
    source = LocalSource(SimpleNamespace(closed=False), "b", binding)
    source.context = SimpleNamespace(configuration_current=True)

    async def read(request):
        raise asyncio.CancelledError

    source.client = SimpleNamespace(read=read)
    assert await source.test_connection() == ("not_running", 0)


async def test_switching_the_option_off_is_logged(caplog) -> None:
    from custom_components.bosch_buderus_heating.local_selection import LocalSelection
    from tests.test_local_selection import RETURN, TARGET, FakeRuntime, cloud, local

    runtime = FakeRuntime([local(RETURN, 31.5)])
    selection = LocalSelection(runtime, TARGET)
    snapshot = cloud(RETURN, 30.0)
    caplog.set_level(
        logging.DEBUG, "custom_components.bosch_buderus_heating.local_selection"
    )
    selection.select(RETURN, snapshot)
    runtime.snapshot.enabled = False
    selection.select(RETURN, snapshot)
    assert [record.getMessage() for record in caplog.records] == [
        "Paired reading return_temperature is now shown from the cloud access"
    ]


async def test_options_dialog_shows_state_and_offers_the_test(
    hass, enable_custom_integrations
) -> None:
    from tests.test_connection_runtime import entry_for
    from tests.test_managed_connections import choose_connection

    hass.config.language = "en"
    entry, cloud, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    # The accesses are a menu with their labels and a way back.
    labels = [label for step, label in result["menu_options"].items() if step != "back"]
    assert list(result["menu_options"])[-1] == "back"
    assert any(item.startswith("Cloud access (") for item in labels)
    assert not any("bosch" in item.casefold() for item in labels)
    hass.config_entries.options.async_abort(result["flow_id"])

    result = await choose_connection(hass, entry, cloud)
    assert result["menu_options"][0] == "connection_test"
    assert result["description_placeholders"]["status"].startswith("State: ")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_test"}
    )
    assert result["step_id"] == "connection_test"
    assert result["description_placeholders"]["result"].startswith("Not tested")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "connection"


async def test_sign_in_wins_over_a_pause_and_an_appliance_can_report_it(
    hass: HomeAssistant,
) -> None:
    hass.config.language = "de"
    runtime = Runtime(hass, paused=600.0)
    runtime.source_phases["b-cloud"] = ConnectionPhase.AUTH_REQUIRED
    assert "Anmeldung nötig" in label(hass, entry_with(runtime), CLOUD, 1)


async def test_no_test_for_an_access_without_appliances(hass: HomeAssistant) -> None:
    hass.config.language = "en"
    runtime = Runtime(hass)
    runtime.source_phases["b-cloud"] = ConnectionPhase.DISABLED
    result = await status.test_connection(hass, entry_with(runtime), CLOUD)
    assert result.startswith("Not tested: the access is not running")
    runtime.client.get_gateways.assert_not_awaited()
