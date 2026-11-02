"""Bacon-only and mixed HA setup, selection, identity and diagnostics."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating import (
    _remove_unselected_gateway_entries,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.bosch_buderus_heating.bacon import flow as helper
from custom_components.bosch_buderus_heating.bacon.entity import BaconConnectivity
from custom_components.bosch_buderus_heating.bacon.models import (
    CONF_BACON_ACCOUNT,
    CONF_BACON_IDS,
    BaconError,
    account_key,
    device_key,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Brand, Gateway
from tests.test_bacon_contracts import token
from tests.test_bacon_session import emit, environment

ROOT = "custom_components.bosch_buderus_heating"


def flow(hass):
    result = BoschBuderusConfigFlow()
    result.hass = hass
    result.context = {}
    result._brand = Brand.BUDERUS
    result._tokens = AuthTokens(token(), "refresh", 4e9, scope=("bacon",))
    return result


def entry(hass, gateways=None):
    result = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: gateways or [],
            CONF_BACON_IDS: ["serial-A"],
            CONF_BACON_ACCOUNT: account_key("account-A"),
            **tokens_to_data(AuthTokens(token(), "refresh", 4e9, scope=("bacon",))),
        },
    )
    result.add_to_hass(hass)
    return result


@pytest.mark.parametrize("mixed", [False, True])
async def test_selection_discovers_separate_backends(hass, mixed):
    config = flow(hass)
    gateways = (Gateway("pointt"),) if mixed else ()
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(return_value=gateways),
        ),
        patch(ROOT + ".bacon.flow.BaconClient") as client,
    ):
        client.return_value.get_devices = AsyncMock(return_value=("serial-A",))
        client.return_value.subject = "account-A"
        shown = await config._async_discover_gateways()
    assert shown["step_id"] == "gateways"
    fields = {str(k): v for k, v in shown["data_schema"].schema.items()}
    assert CONF_BACON_IDS in fields
    assert (CONF_GATEWAY_IDS in fields) == mixed
    config.async_set_unique_id = AsyncMock()
    config._abort_if_unique_id_configured = Mock()
    result = await config.async_step_gateways(
        {
            CONF_BACON_IDS: ["serial-A"],
            CONF_GATEWAY_IDS: ["pointt"] if mixed else [],
        }
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACON_IDS] == ["serial-A"]
    assert result["data"][CONF_GATEWAY_IDS] == (["pointt"] if mixed else [])
    assert config._account_identity().startswith("buderus:")
    if not mixed:
        assert ":bacon:" in config._account_identity()


@pytest.mark.parametrize(
    "selection", [["unknown"], "serial-A", ["serial-A", "serial-A"]]
)
async def test_rejects_tampered_bacon_selection(hass, selection):
    config = flow(hass)
    config._bacon_ids = ("serial-A",)
    result = await config.async_step_gateways({CONF_BACON_IDS: selection})
    assert result["errors"] == {"base": "select_gateway"}


async def test_discovery_retries_and_authentication(hass):
    config = flow(hass)
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways", AsyncMock(return_value=())
        ) as pointt,
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(side_effect=BaconError("rate_limited", 120)),
        ) as bacon,
    ):
        first = await config._async_discover_gateways()
        second = await config._async_discover_gateways()
    assert first["errors"] == second["errors"] == {"base": "bacon_unavailable"}
    assert bacon.await_count == pointt.await_count == 1
    config._bacon_retry_at = 0
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways", AsyncMock(return_value=())
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(side_effect=BaconError("authentication")),
        ),
        patch.object(
            config, "_restart_authorization", return_value={"marker": True}
        ) as restart,
    ):
        assert await config._async_discover_gateways() == {"marker": True}
        restart.assert_called_once_with(error="invalid_auth")


@pytest.mark.parametrize(
    "subject,reason",
    [("account-A", "reauth_successful"), ("account-B", "reauth_wrong_account")],
)
async def test_bacon_only_reauthentication_checks_account(hass, subject, reason):
    config = flow(hass)
    config._reauth_entry = entry(hass)
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways", AsyncMock(return_value=())
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(return_value=(("serial-A",), account_key(subject))),
        ),
        patch.object(
            config,
            "_update_and_restart",
            return_value={"reason": "reauth_successful"},
        ),
    ):
        result = await config._async_discover_gateways()
    assert result["reason"] == reason


async def test_duplicate_account_and_deselection_are_explicit(hass):
    configured = entry(hass)
    config = flow(hass)
    config._bacon_account = account_key("account-A")
    assert config._find_duplicate_entry(set()) is configured
    config._bacon_account = account_key("account-B")
    assert config._find_duplicate_entry(set()) is None
    assert helper.config_data([], "") == {CONF_BACON_IDS: [], CONF_BACON_ACCOUNT: ""}


async def test_discover_without_scope_does_not_connect():
    with patch(ROOT + ".bacon.flow.BaconClient") as client:
        assert await helper.discover(None, AuthTokens("unused")) == ((), "")
        client.assert_not_called()
    with patch(ROOT + ".bacon.flow.BaconClient") as client:
        client.return_value.subject = "account-A"
        client.return_value.get_devices = AsyncMock(return_value=())
        assert await helper.discover(None, AuthTokens(token(), scope=("bacon",))) == (
            (),
            account_key("account-A"),
        )


@pytest.mark.parametrize("mixed", [False, True])
async def test_runtime_shares_tokens_and_keeps_pointt_separate(hass, mixed):
    configured = entry(hass, ["pointt"] if mixed else [])

    async def wait():
        await asyncio.Event().wait()

    with (
        patch(
            ROOT + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway("pointt"),)),
        ) as get,
        patch(ROOT + ".BoschBuderusDataUpdateCoordinator") as coordinator,
        patch(ROOT + "._async_load_startup_data", AsyncMock()),
        patch(ROOT + ".async_setup_notifications", AsyncMock(return_value=())),
        patch(ROOT + ".sync_firmware_compatibility_issue"),
        patch(ROOT + "._restore_registered_startup_paths"),
        patch(ROOT + "._enable_new_default_entities"),
        patch(ROOT + "._consolidate_logical_devices"),
        patch(ROOT + "._remove_retired_entities"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch(ROOT + ".BaconAccount.run", side_effect=wait),
    ):
        coordinator.return_value.async_load_fault_state = AsyncMock()
        coordinator.return_value.async_config_entry_first_refresh = AsyncMock()
        assert await async_setup_entry(hass, configured)
        runtime = configured.runtime_data
        assert runtime.bacon.client.tokens is runtime.token_manager
        assert runtime.bacon.selected == ("serial-A",)
        assert len(runtime.coordinators) == int(mixed)
        assert get.await_count == int(mixed)
        with patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ):
            assert await async_unload_entry(hass, configured)
        assert not runtime.bacon.connected


async def test_entity_registry_identity_and_redacted_diagnostics(hass):
    configured = entry(hass)
    account = environment()
    from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData

    configured.runtime_data = BoschBuderusRuntimeData(
        client=SimpleNamespace(metrics=SimpleNamespace(snapshot=lambda: {})),
        token_manager=None,
        gateways=(),
        coordinators=(),
        bacon=account,
    )
    entity = BaconConnectivity(account, "serial-A")
    assert entity.unique_id == device_key("account-A", "serial-A") + ":connectivity"
    assert entity.is_on is True
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=configured.entry_id,
        **entity.device_info,
    )
    registered = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        entity.unique_id,
        config_entry=configured,
        device_id=device.id,
    )
    er.async_get(hass).async_update_entity(registered.entity_id, name="My device")
    _remove_unselected_gateway_entries(hass, configured, frozenset())
    assert er.async_get(hass).async_get(registered.entity_id).name == "My device"
    entity.hass = hass
    entity.entity_id = registered.entity_id
    with patch.object(entity, "async_write_ha_state") as update:
        await entity.async_added_to_hass()
        emit(account, "topics/info", {"online": False, "timestamp": 2})
        update.assert_called_once()
        await entity.async_will_remove_from_hass()
    assert entity.is_on is False
    data = await async_get_config_entry_diagnostics(hass, configured)
    assert data["diagnostics_schema"] == 28
    assert data["bacon"]["selected_devices"] == 2
    serialized = json.dumps(data)
    for private in ("account-A", "serial-A", "My device", token()):
        assert private not in serialized
    account._disconnected()
    assert entity.is_on is None
    configured.runtime_data.bacon = None
    _remove_unselected_gateway_entries(hass, configured, frozenset())
    assert er.async_get(hass).async_get(registered.entity_id) is None
    assert dr.async_get(hass).async_get(device.id) is None


@pytest.mark.parametrize("category", ["connection", "authentication"])
async def test_reconfigure_classifies_bacon_errors_and_preserves_selection(
    hass, category
):
    config = flow(hass)
    config._reconfigure_entry = entry(hass)
    config._reconfigure_client = Mock(
        return_value=SimpleNamespace(get_gateways=AsyncMock(return_value=()))
    )
    with patch(
        ROOT + ".bacon.flow.discover", AsyncMock(side_effect=BaconError(category))
    ):
        result = await config._async_prepare_reconfigure()
        if category == "connection":
            assert result["step_id"] == "reconfigure_retry"
            assert (await config._async_prepare_reconfigure())["errors"] == result[
                "errors"
            ]
        else:
            assert result["reason"] == "reauth_required"
    assert config._reconfigure_entry.data[CONF_BACON_IDS] == ["serial-A"]


async def test_reconfigure_bacon_only_saves_exact_selection(hass):
    config = flow(hass)
    config._reconfigure_entry = entry(hass)
    config._bacon_ids = ("serial-A", "serial-B")
    config._bacon_account = account_key("account-A")
    form = config._show_reconfigure_form()
    assert CONF_BACON_IDS in {str(k) for k in form["data_schema"].schema}
    assert CONF_GATEWAY_IDS not in {str(k) for k in form["data_schema"].schema}
    with patch.object(
        config, "_update_and_restart", return_value={"ok": True}
    ) as update:
        assert await config.async_step_reconfigure(
            {
                CONF_BRAND: "buderus",
                CONF_BACON_IDS: ["serial-B"],
                "polling_profile": "standard",
            }
        ) == {"ok": True}
    assert update.call_args.kwargs["data_updates"][CONF_BACON_IDS] == ["serial-B"]


async def test_platform_creates_only_selected_connection_entities(hass):
    from custom_components.bosch_buderus_heating.binary_sensor import (
        async_setup_entry as setup,
    )

    configured = SimpleNamespace(
        runtime_data=SimpleNamespace(bacon=environment(), coordinators=()),
        async_on_unload=Mock(),
    )
    added = []
    await setup(hass, configured, added.extend)
    assert len(added) == 2
    assert all(isinstance(entity, BaconConnectivity) for entity in added)
    assert {entity.serial for entity in added} == {"serial-A", "serial-B"}


async def test_runtime_rejects_a_changed_account(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    configured = entry(hass)
    hass.config_entries.async_update_entry(
        configured,
        data={
            **configured.data,
            CONF_BACON_ACCOUNT: account_key("different-account"),
        },
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, configured)
    configured.runtime_data.startup.close()


@pytest.mark.parametrize("category", ["authentication", "connection", "rate_limited"])
async def test_runtime_token_failures_distinguish_signin_from_retry(hass, category):
    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

    configured = entry(hass)
    expected = (
        ConfigEntryAuthFailed if category == "authentication" else ConfigEntryNotReady
    )
    with (
        patch(
            ROOT + ".BaconClient.access_token",
            AsyncMock(side_effect=BaconError(category)),
        ),
        pytest.raises(expected),
    ):
        await async_setup_entry(hass, configured)
    configured.runtime_data.startup.close()


@pytest.mark.parametrize("kind", ["setup", "reauth", "reconfigure"])
async def test_pointt_choices_survive_bacon_backoff(hass, kind):
    config = flow(hass)
    existing = entry(hass, ["pointt"]) if kind != "setup" else None
    if existing:
        hass.config_entries.async_update_entry(
            existing,
            data={
                **existing.data,
                CONF_BACON_IDS: [],
            },
        )
    if kind == "reauth":
        config._reauth_entry = existing
    if kind == "reconfigure":
        config._reconfigure_entry = existing
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(
                return_value=(Gateway("pointt"),),
            ),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(
                side_effect=BaconError("rate_limited", 120),
            ),
        ) as cloud,
        patch.object(
            config,
            "_update_and_restart",
            return_value={"reason": "reauth_successful"},
        ),
    ):
        operation = (
            config._async_prepare_reconfigure
            if kind == "reconfigure"
            else config._async_discover_gateways
        )
        first, second = await operation(), await operation()
    assert cloud.await_count == 1
    for result in (first, second):
        if kind == "reauth":
            assert result["reason"] == "reauth_successful"
        else:
            assert result["step_id"] == (
                "reconfigure" if kind == "reconfigure" else "gateways"
            )
            assert result["errors"] == {"base": "bacon_selection_unavailable"}
            assert CONF_BACON_IDS not in {str(k) for k in result["data_schema"].schema}


async def test_mixed_reconfigure_preserves_hidden_selection_during_outage(hass):
    config = flow(hass)
    config._reconfigure_entry = existing = entry(hass, ["pointt"])
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(
                return_value=(Gateway("pointt"),),
            ),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(
                side_effect=BaconError("service_unavailable"),
            ),
        ),
    ):
        shown = await config._async_prepare_reconfigure()
    assert shown["step_id"] == "reconfigure"
    assert config._select_bacon_devices({}) == ["serial-A"]
    with pytest.raises(ValueError):
        config._select_bacon_devices({CONF_BACON_IDS: []})
    config._selected_bacon = config._select_bacon_devices({})
    with patch.object(config, "_update_and_restart", return_value={}) as save:
        config._finish_reconfigure(["pointt"])
    assert save.call_args.kwargs["data_updates"][CONF_BACON_IDS] == ["serial-A"]
    assert (
        save.call_args.kwargs["data_updates"][CONF_BACON_ACCOUNT]
        == existing.data[CONF_BACON_ACCOUNT]
    )


async def test_outage_cannot_transfer_existing_cloud_devices_to_another_account(hass):
    config = flow(hass)
    config._reconfigure_entry = entry(hass, ["pointt"])
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(
                return_value=(Gateway("another-gateway"),),
            ),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(
                side_effect=BaconError("service_unavailable"),
            ),
        ),
    ):
        shown = await config._async_prepare_reconfigure()
    assert shown["step_id"] == "reconfigure_retry"
    assert shown["errors"] == {"base": "bacon_unavailable"}


async def test_fallback_does_not_extend_cloud_backoff_and_recovers_choices(hass):
    config = flow(hass)
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(
                return_value=(Gateway("pointt"),),
            ),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(
                side_effect=[
                    BaconError("rate_limited", 120),
                    (("serial-A",), account_key("account-A")),
                ]
            ),
        ) as cloud,
        patch(ROOT + ".config_flow.monotonic", return_value=100) as clock,
    ):
        await config._async_discover_gateways()
        assert config._bacon_retry_at == 220
        clock.return_value = 219
        await config._async_discover_gateways()
        assert config._bacon_retry_at == 220
        clock.return_value = 221
        result = await config._async_discover_gateways()
    assert cloud.await_count == 2
    assert result["errors"] == {}
    assert CONF_BACON_IDS in {str(k) for k in result["data_schema"].schema}


async def test_cloud_only_reauth_stays_retryable_during_service_outage(hass):
    config = flow(hass)
    config._reauth_entry = entry(hass)
    with (
        patch(
            ROOT + ".config_flow.PointTClient.get_gateways",
            AsyncMock(
                return_value=(Gateway("unselected"),),
            ),
        ),
        patch(
            ROOT + ".bacon.flow.discover",
            AsyncMock(
                side_effect=BaconError("service_unavailable"),
            ),
        ),
    ):
        result = await config._async_discover_gateways()
    assert result["step_id"] == "retry"
    assert result["errors"] == {"base": "bacon_unavailable"}
