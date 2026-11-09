"""Native HA dialogs for new, retained and independently managed cloud access."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryDisabler
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.cloud_configuration import (
    CLAIM,
    CLAIM_ORPHANED_SECONDS,
    CLAIMED_AT,
    TAKEN_OVER,
    check_new_cloud,
    ensure_active_cloud_flow,
)
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import (
    tokens_from_data,
    tokens_to_data,
)
from custom_components.bosch_buderus_heating.pointt import (
    Brand,
    Gateway,
    RateLimited,
    ServiceUnavailable,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_config_flow import _redirect_from_result
from tests.test_configuration import legacy
from tests.test_connection_runtime import entry_for
from tests.test_managed_connections import choose_connection

ROOT = "custom_components.bosch_buderus_heating"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def fresh_tokens():
    return tokens_from_data({**legacy(), "expires_at": 10**12})


def empty_entry(hass, entry_id="empty-owner"):
    snapshot = load_configuration(
        entry_id,
        {
            "revision": 0,
            "connections": {},
            "targets": {},
            "bindings": {},
            "legacy_extra": {},
        },
        {"connections": {}, "targets": {}, "bindings": {}},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        unique_id=entry_id,
        version=4,
        data=snapshot.to_data(),
        options=snapshot.to_options(),
    )
    entry.add_to_hass(hass)
    return entry


def existing(hass):
    entry, cloud, target = entry_for(hass)
    data = deepcopy(dict(entry.data))
    data["connections"][cloud]["credentials"] = tokens_to_data(fresh_tokens())
    hass.config_entries.async_update_entry(entry, data=data)
    return entry, cloud, target


@pytest.fixture(autouse=True)
def active_storage_contract():
    assert BoschBuderusConfigFlow.VERSION == 4


@pytest.fixture
def backend():
    with ExitStack() as stack:
        gateways = stack.enter_context(
            patch(
                ROOT + ".pointt.client.PointTClient.get_gateways",
                AsyncMock(
                    return_value=(
                        Gateway("000123"),
                        Gateway("gateway-b"),
                        Gateway("new-gateway"),
                    )
                ),
            )
        )
        devices = AsyncMock(
            return_value=(("softener", "new-device"), legacy()["bacon_account"])
        )
        stack.enter_context(patch(ROOT + ".bacon.flow.discover", devices))
        stack.enter_context(patch(ROOT + ".cloud_configuration.discover", devices))
        oauth = stack.enter_context(
            patch(
                ROOT + ".pointt.auth.OAuthClient.exchange_code",
                AsyncMock(return_value=fresh_tokens()),
            )
        )
        stack.enter_context(
            patch(ROOT + ".async_setup_entry", AsyncMock(return_value=True))
        )
        stack.enter_context(
            patch(ROOT + ".async_unload_entry", AsyncMock(return_value=True))
        )
        yield SimpleNamespace(gateways=gateways, devices=devices, oauth=oauth)


async def new_setup(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    assert result["step_id"] == "gateways"
    return result


async def connect_dialog(hass, entry, *, authenticate=True):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "connection_add_cloud" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_add_cloud"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    assert result["step_id"] == "cloud_auth"
    if authenticate:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
        )
    return result


@pytest.mark.parametrize(
    "gateways,devices",
    [(["000123"], []), ([], ["softener"]), (["000123"], ["softener"])],
)
async def test_native_new_setup_uses_v4_storage(hass, backend, gateways, devices):
    result = await new_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": gateways, "bacon_device_ids": devices}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    snapshot = load_configuration(entry.entry_id, entry.data, entry.options)
    cloud = entry.data["default_cloud_connection_id"]
    assert snapshot.connections[cloud].kind == "singlekey_cloud"
    selected = EntryConfiguration(entry, cloud).cloud_data()
    assert set(selected["gateway_ids"]) == set(gateways)
    assert set(selected["bacon_device_ids"]) == set(devices)
    assert "access_token" not in entry.data and entry.version == 4
    assert len(snapshot.targets) == len(gateways) + len(devices)
    assert all(
        t.to_data()["identities"][0]["evidence"] == "authenticated_listing"
        for t in snapshot.targets.values()
    )


@pytest.mark.parametrize("disabled", [False, True])
async def test_additional_cloud_blocked_before_oauth_even_if_disabled(
    hass, backend, disabled
):
    entry, cloud, _ = existing(hass)
    if disabled:
        management = ConfigurationManagement(hass, entry)
        management.set_enabled(management.edit(cloud), False)
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["reason"] == "cloud_already_connected"
    backend.oauth.assert_not_awaited()
    backend.gateways.assert_not_awaited()


def flow_context(hass, flow_id):
    """The live context of a dialog, as the claim checks see it."""
    for manager in (hass.config_entries.flow, hass.config_entries.options):
        for flow in manager.async_progress(include_uninitialized=True):
            if flow["flow_id"] == flow_id:
                return flow["context"]
    return None


async def open_cloud_dialog(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )


async def test_new_dialog_takes_over_abandoned_cloud_setup(hass, backend):
    """FLOW-1: a reloaded app leaves its dialog behind; a new one must win."""
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    assert flow_context(hass, first["flow_id"])[CLAIM] is True
    second = await open_cloud_dialog(hass)
    assert second["type"] is FlowResultType.FORM and second["step_id"] == "cloud"
    # The older dialog stays for a tab that is really open, but without its
    # claim; it ends at its next step without a cloud request.
    old = flow_context(hass, first["flow_id"])
    assert CLAIM not in old and old[TAKEN_OVER] is True
    ended = await hass.config_entries.options.async_configure(
        first["flow_id"],
        {"redirect_url": _redirect_from_result(first, Brand.BUDERUS)},
    )
    assert ended["type"] is FlowResultType.ABORT
    assert ended["reason"] == "cloud_setup_taken_over"
    backend.oauth.assert_not_awaited()
    assert flow_context(hass, first["flow_id"]) is None
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"brand": "buderus"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ConfigurationManagement(hass, entry).current().connections == {}


async def test_taken_over_dialog_cannot_register_its_signed_in_cloud(hass, backend):
    """The commit of a replaced dialog is refused even after its sign-in."""
    signed_in = await new_setup(hass)
    entry = empty_entry(hass)
    newer = await connect_dialog(hass, entry, authenticate=False)
    assert newer["step_id"] == "cloud_auth"
    with patch(
        ROOT + ".config_flow.new_cloud_configuration",
        side_effect=AssertionError("must not register"),
    ):
        ended = await hass.config_entries.flow.async_configure(
            signed_in["flow_id"], {"gateway_ids": ["000123"]}
        )
    assert ended["type"] is FlowResultType.ABORT
    assert ended["reason"] == "cloud_setup_taken_over"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    result = await hass.config_entries.options.async_configure(
        newer["flow_id"],
        {"redirect_url": _redirect_from_result(newer, Brand.BUDERUS)},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(ConfigurationManagement(hass, entry).current().connections) == 1


@pytest.mark.parametrize(
    "step", ["auth", "auth_wait", "retry", "selection_expired", "gateways"]
)
async def test_every_cloud_step_of_a_replaced_dialog_aborts(hass, backend, step):
    result = await open_cloud_dialog(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    await open_cloud_dialog(hass)
    ended = await getattr(flow, "async_step_" + step)({})
    assert ended["type"] is FlowResultType.ABORT
    assert ended["reason"] == "cloud_setup_taken_over"
    backend.oauth.assert_not_awaited()
    backend.gateways.assert_not_awaited()


@pytest.mark.parametrize("step", ["cloud_auth", "cloud_retry", "cloud_select"])
async def test_every_step_of_a_replaced_options_dialog_aborts(hass, backend, step):
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    flow = hass.config_entries.options._progress[first["flow_id"]]
    await open_cloud_dialog(hass)
    ended = await getattr(flow, "async_step_" + step)({})
    assert ended["type"] is FlowResultType.ABORT
    assert ended["reason"] == "cloud_setup_taken_over"
    with pytest.raises(ConfigurationError, match="configuration_changed"):
        ensure_active_cloud_flow(hass, first["flow_id"])
    # The replaced dialog itself can no longer claim the setup back.
    with pytest.raises(ConfigurationError, match="cloud_setup_taken_over"):
        check_new_cloud(hass, first["flow_id"])
    ended = await flow.async_step_connection_add_cloud()
    assert ended["reason"] == "cloud_setup_taken_over"


async def test_orphaned_claim_is_removed_and_a_live_one_blocks_registration(hass):
    """A claim idle beyond the sign-in lifetime counts as orphaned."""
    empty_entry(hass)
    first = await open_cloud_dialog(hass)
    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    # Registration still refuses any other live claim.
    claim = flow_context(hass, first["flow_id"])
    with pytest.raises(ConfigurationError, match="cloud_setup_in_progress"):
        check_new_cloud(hass, second["flow_id"])
    assert flow_context(hass, first["flow_id"]) is claim
    claim[CLAIMED_AT] -= CLAIM_ORPHANED_SECONDS + 1
    check_new_cloud(hass, second["flow_id"])
    assert flow_context(hass, first["flow_id"]) is None
    # An orphan is removed when a new dialog takes over, not left behind.
    third = await open_cloud_dialog(hass)
    flow_context(hass, third["flow_id"])[CLAIMED_AT] -= CLAIM_ORPHANED_SECONDS + 1
    fourth = await open_cloud_dialog(hass)
    assert fourth["step_id"] == "cloud"
    assert flow_context(hass, third["flow_id"]) is None
    # Activity of the claiming dialog renews its claim.
    flow_context(hass, fourth["flow_id"])[CLAIMED_AT] -= CLAIM_ORPHANED_SECONDS + 1
    check_new_cloud(hass, fourth["flow_id"])
    fifth = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with pytest.raises(ConfigurationError, match="cloud_setup_in_progress"):
        check_new_cloud(hass, fifth["flow_id"])
    for flow_id in (second["flow_id"], fourth["flow_id"], fifth["flow_id"]):
        hass.config_entries.flow.async_abort(flow_id)


async def test_live_dialog_claim_released_on_cancel(hass, backend):
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    hass.config_entries.options.async_abort(first["flow_id"])
    assert flow_context(hass, first["flow_id"]) is None
    allowed = await open_cloud_dialog(hass)
    assert allowed["step_id"] == "cloud"
    hass.config_entries.flow.async_abort(allowed["flow_id"])


async def test_retained_targets_cannot_be_claimed_by_new_entry(hass, backend):
    entry, cloud, _ = existing(hass)
    management = ConfigurationManagement(hass, entry)
    management.remove(management.edit(cloud))
    before = deepcopy(dict(entry.data))
    result = await new_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["reason"] == "cloud_target_owned"
    assert entry.data == before and len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_an_unreadable_entry_keeps_its_gateways_claimed(hass, backend):
    broken = MockConfigEntry(
        domain=DOMAIN,
        entry_id="broken-owner",
        version=4,
        minor_version=1,
        data={
            "targets": {
                "t": {
                    "identities": [{"namespace": "pointt_gateway", "value": "000123"}]
                }
            },
            "bindings": "damaged",
        },
        options={},
    )
    broken.add_to_hass(hass)
    result = await new_setup(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["reason"] == "cloud_target_owned"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_unreadable_entries_claim_what_their_raw_data_names(hass, caplog):
    from custom_components.bosch_buderus_heating.cloud_configuration import (
        cloud_snapshots,
        foreign_identities,
        unreadable_gateway_ids,
    )

    assert unreadable_gateway_ids(
        {
            "gateway_ids": ["legacy-gw", 5],
            "targets": {
                "a": {
                    "identities": [
                        {"namespace": "local_system_gateway", "value": "local-gw"},
                        {"namespace": "bacon_device", "value": "softener"},
                        "damaged",
                    ],
                    "legacy_identity": {"gateway_id": "old-gw"},
                },
                "b": "damaged",
            },
            "bindings": {
                "x": {"locator": {"namespace": "pointt_gateway", "value": "bound-gw"}},
                "y": {"locator": {"namespace": "bacon_device", "value": "dev"}},
            },
        }
    ) == {"legacy-gw", "local-gw", "old-gw", "bound-gw"}
    assert unreadable_gateway_ids({"targets": [], "bindings": None}) == set()
    MockConfigEntry(
        domain=DOMAIN, entry_id="legacy-broken", version=3, data={"gateway_ids": ["9"]}
    ).add_to_hass(hass)
    assert cloud_snapshots(hass) == ()
    assert "cannot be read" not in caplog.text
    assert foreign_identities(hass, "other") == {("pointt_gateway", "", "9")}
    assert foreign_identities(hass, "legacy-broken") == set()
    # Nothing to protect: still skipped, but never silently.
    MockConfigEntry(
        domain=DOMAIN, entry_id="empty-broken", version=4, minor_version=1, data={}
    ).add_to_hass(hass)
    assert cloud_snapshots(hass) == ()
    assert "Configuration entry empty-broken cannot be read" in caplog.text


@pytest.mark.parametrize("native", [False, True])
async def test_remove_and_reconnect_preserves_targets_names_and_connection_id(
    hass, backend, native
):
    if native:
        result = await new_setup(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"gateway_ids": ["000123"], "bacon_device_ids": ["softener"]},
        )
        entry = result["result"]
        cloud = entry.data["default_cloud_connection_id"]
    else:
        entry, cloud, _ = existing(hass)
    title, identity = entry.title, entry.unique_id
    targets = deepcopy(entry.data["targets"])
    management = ConfigurationManagement(hass, entry)
    management.remove(management.edit(cloud))
    result = await connect_dialog(hass, entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "my_notification_setting": True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"], "bacon_device_ids": ["softener"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["targets"] == targets
    assert cloud in entry.data["connections"]
    assert entry.options["my_notification_setting"] is True
    assert (entry.title, entry.unique_id) == (title, identity)


async def test_discovery_adds_new_devices_and_keeps_deselected_entities(hass, backend):
    entry, cloud, _ = existing(hass)
    old = deepcopy(dict(entry.data))
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["step_id"] == "connection_discover" and entry.data == old
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"gateway_ids": ["000123", "new-gateway"], "bacon_device_ids": ["new-device"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert all(
        entry.data["targets"][key] == value for key, value in old["targets"].items()
    )
    assert len(entry.data["targets"]) == len(old["targets"]) + 2
    for key, binding in entry.data["bindings"].items():
        if binding["connection_id"] == cloud:
            expected = binding["locator"]["value"] in {
                "000123",
                "new-gateway",
                "new-device",
            }
            assert entry.options["bindings"][key]["enabled"] == expected
    assert entry.data["connections"]["local"] == old["connections"]["local"]


async def test_temporarily_missing_existing_device_remains_selectable(hass, backend):
    entry, cloud, _ = existing(hass)
    backend.gateways.return_value = (Gateway("000123"),)
    backend.devices.return_value = ((), legacy()["bacon_account"])
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"gateway_ids": ["gateway-b"], "bacon_device_ids": ["softener"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("profile", ["standard", "cloud_friendly"])
async def test_polling_profile_is_scoped_and_available_offline(hass, backend, profile):
    entry, cloud, _ = existing(hass)
    before = deepcopy(dict(entry.options))
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_polling"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"polling_profile": profile}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["connections"][cloud]["polling_profile"] == profile
    assert entry.options["connections"]["local"] == before["connections"]["local"]
    backend.gateways.assert_not_awaited()


@pytest.mark.parametrize("step", ["connection_polling", "connection_discover"])
async def test_stale_dialog_does_not_apply_changes(hass, backend, step):
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    management = ConfigurationManagement(hass, entry)
    management.remove(management.edit(cloud))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["reason"] == "configuration_changed"
    backend.gateways.assert_not_awaited()


async def test_disabled_cloud_discovery_does_not_send_requests(hass, backend):
    entry, cloud, _ = existing(hass)
    management = ConfigurationManagement(hass, entry)
    management.set_enabled(management.edit(cloud), False)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["reason"] == "cloud_connection_disabled"
    backend.gateways.assert_not_awaited()


@pytest.mark.parametrize("error", [ServiceUnavailable(), RateLimited(300)])
async def test_oauth_failure_never_updates_entry_and_rate_limit_waits(
    hass, backend, error
):
    entry = empty_entry(hass)
    before = deepcopy(dict(entry.data))
    result = await connect_dialog(hass, entry, authenticate=False)
    backend.oauth.side_effect = error
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    assert result["step_id"] == "cloud_retry" and entry.data == before
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == (
        "cloud_retry" if isinstance(error, RateLimited) else "cloud_auth"
    )
    backend.oauth.assert_awaited_once()


async def test_expired_selection_requires_a_fresh_listing(hass, backend):
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    before = deepcopy(dict(entry.data))
    with patch(ROOT + ".cloud_configuration.monotonic", return_value=10**12):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"gateway_ids": ["new-gateway"]}
        )
    assert result["step_id"] == "connection_discover_retry" and entry.data == before
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "connection_discover"
    assert backend.gateways.await_count == 2


@pytest.mark.parametrize("phase", ["oauth", "discovery", "selection"])
async def test_cancelled_reconnect_keeps_retained_configuration(hass, backend, phase):
    import asyncio

    entry = empty_entry(hass)
    before = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    if phase == "selection":
        result = await connect_dialog(hass, entry)
        hass.config_entries.options.async_abort(result["flow_id"])
    else:
        result = await connect_dialog(hass, entry, authenticate=False)
        gate, started = asyncio.Event(), asyncio.Event()

        async def wait(*args):
            started.set()
            await gate.wait()

        (backend.oauth if phase == "oauth" else backend.gateways).side_effect = wait
        task = asyncio.create_task(
            hass.config_entries.options.async_configure(
                result["flow_id"],
                {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hass.config_entries.options.async_abort(result["flow_id"])
    assert (entry.data, entry.options) == before
    assert not hass.config_entries.options.async_progress()


@pytest.mark.parametrize("change", ["remove", "disable", "reauth", "rotate"])
async def test_selection_checks_current_context_but_keeps_rotated_tokens(
    hass, backend, change
):
    entry, cloud, _ = existing(hass)
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    management = ConfigurationManagement(hass, entry)
    if change == "remove":
        management.remove(management.edit(cloud))
    elif change == "disable":
        management.set_enabled(management.edit(cloud), False)
    else:
        data = deepcopy(dict(entry.data))
        data["connections"][cloud]["credentials"]["refresh_token"] = (
            "new-synthetic-refresh"
        )
        if change == "reauth":
            data["connections"][cloud]["auth_epoch"] += 1
        hass.config_entries.async_update_entry(entry, data=data)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": ["new-gateway"]}
    )
    if change == "rotate":
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert (
            entry.data["connections"][cloud]["credentials"]["refresh_token"]
            == "new-synthetic-refresh"
        )
    else:
        assert result["reason"] == "configuration_changed"
        assert entry.data == before


async def test_removing_entry_while_discovery_runs_never_restores_it(hass, backend):
    entry = empty_entry(hass)
    result = await connect_dialog(hass, entry, authenticate=False)

    async def removed(*args):
        await hass.config_entries.async_remove(entry.entry_id)
        return (Gateway("000123"),)

    backend.gateways.side_effect = removed
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"]}
    )
    assert result["reason"] == "configuration_changed"
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("stage", ["pointt", "bacon"])
async def test_existing_discovery_auth_failure_starts_scoped_reauthentication(
    hass, backend, stage
):
    from homeassistant.config_entries import SOURCE_REAUTH

    from custom_components.bosch_buderus_heating.bacon.models import BaconError
    from custom_components.bosch_buderus_heating.pointt import AuthenticationError

    entry, cloud, _ = existing(hass)
    if stage == "pointt":
        backend.gateways.side_effect = AuthenticationError()
    else:
        backend.devices.side_effect = BaconError("authentication")
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["reason"] == "connection_reauth_started"
    await hass.async_block_till_done()
    flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(flows) == 1 and flows[0]["context"]["connection_id"] == cloud
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])


@pytest.mark.parametrize("phase", ["new", "reconnect"])
async def test_expired_new_listing_cannot_register_connection(hass, backend, phase):
    if phase == "new":
        result = await new_setup(hass)
        manager = hass.config_entries.flow
    else:
        entry = empty_entry(hass)
        result = await connect_dialog(hass, entry)
        manager = hass.config_entries.options
    with patch(ROOT + ".cloud_configuration.monotonic", return_value=10**12):
        result = await manager.async_configure(
            result["flow_id"], {"gateway_ids": ["000123"]}
        )
    if phase == "new":
        # A slow selection lists the devices again, without a new sign-in.
        assert result["step_id"] == "selection_expired"
        assert not hass.config_entries.async_entries(DOMAIN)
        assert backend.gateways.await_count == 1
        result = await manager.async_configure(result["flow_id"], {})
        assert result["step_id"] == "gateways"
        assert backend.gateways.await_count == 2
        result = await manager.async_configure(
            result["flow_id"], {"gateway_ids": ["000123"]}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["step_id"] == "cloud_retry" and not entry.data["connections"]
        result = await manager.async_configure(result["flow_id"], {})
        assert result["step_id"] == "cloud_select"
        assert backend.gateways.await_count == 2


async def test_native_discovery_rate_limit_blocks_immediate_retry(hass, backend):
    backend.gateways.side_effect = RateLimited(300)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"brand": "buderus"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
    )
    assert result["errors"] == {"base": "cloud_rate_limited"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cloud_rate_limited"}
    backend.gateways.assert_awaited_once()


async def test_reconnect_empty_listing_can_be_retried_without_new_login(hass, backend):
    entry = empty_entry(hass)
    backend.gateways.return_value = ()
    backend.devices.return_value = ((), "")
    result = await connect_dialog(hass, entry)
    assert result["step_id"] == "cloud_retry" and result["errors"] == {
        "base": "no_gateways"
    }
    backend.gateways.return_value = (Gateway("000123"),)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "cloud_select"
    backend.oauth.assert_awaited_once()


@pytest.mark.parametrize("version", [3, 5])
async def test_other_entry_blocks_registration_without_interpreting_future_storage(
    hass, backend, version
):
    entry = MockConfigEntry(domain=DOMAIN, version=version, data=legacy())
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    assert result["reason"] == (
        "cloud_already_connected" if version == 3 else "configuration_changed"
    )
    assert entry.data == before
    backend.oauth.assert_not_awaited()


async def test_first_bacon_selection_adds_account_without_touching_pointt_identity(
    hass, backend
):
    from custom_components.bosch_buderus_heating.configuration_migration import (
        convert_legacy_configuration,
    )

    snapshot = convert_legacy_configuration("pointt-only", legacy(["000123"], []), {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="pointt-only",
        version=4,
        data=snapshot.to_data(),
        options=snapshot.to_options(),
    )
    entry.add_to_hass(hass)
    cloud = next(iter(snapshot.connections))
    original = deepcopy(entry.data["targets"])
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gateway_ids": ["000123"], "bacon_device_ids": ["softener"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert all(entry.data["targets"][key] == value for key, value in original.items())
    assert (
        EntryConfiguration(entry, cloud).cloud_data()["bacon_account"]
        == legacy()["bacon_account"]
    )


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("socket_enabled")
async def test_real_ha_reconnect_restores_same_devices_entities_and_statistics(
    hass, native
):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from tests.test_entry_dispatch import legacy_entry, loopback
    from tests.test_managed_platforms import NUMBER, available, registered
    from tests.test_managed_pointt import Backend, eventually

    backend = Backend()
    async with loopback(backend):
        # Both interactive discovery and runtime clients use the loopback backend.
        from custom_components.bosch_buderus_heating import managed_cloud

        with (
            patch(ROOT + ".config_flow.PointTClient", managed_cloud.PointTClient),
            patch(
                ROOT + ".cloud_options_flow.PointTClient", managed_cloud.PointTClient
            ),
            patch(ROOT + ".bacon.flow.discover", AsyncMock(return_value=((), ""))),
            patch(
                ROOT + ".pointt.auth.OAuthClient.exchange_code",
                AsyncMock(return_value=fresh_tokens()),
            ),
        ):
            if native:
                result = await new_setup(hass)
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], {"gateway_ids": ["000123"]}
                )
                entry = result["result"]
            else:
                entry = legacy_entry(hass)
                assert await hass.config_entries.async_setup(entry.entry_id)
            await eventually(
                lambda: (
                    hasattr(entry, "runtime_data")
                    and available(entry.runtime_data, "number", NUMBER)
                )
            )
            owner = entry.runtime_data
            cloud = next(iter(entry.data["connections"]))
            original = registered(hass, "number", NUMBER)
            registry = er.async_get(hass)
            registry.async_update_entity(
                original.entity_id, name="My room", icon="mdi:home"
            )
            device_id = original.device_id
            dr.async_get(hass).async_update_device(device_id, name_by_user="My heating")
            metrics_uid = (
                entry.entry_id
                + (f":connection:{cloud}" if native else "")
                + ":request_metrics:pointt_api_requests_total"
            )
            metrics = registered(hass, "sensor", metrics_uid)
            assert metrics is not None
            registry.async_update_entity(metrics.entity_id, name="My request counter")
            management = ConfigurationManagement(hass, entry)
            management.remove(management.edit(cloud))
            await eventually(lambda: not available(owner, "number", NUMBER))
            assert hass.states.get(original.entity_id).state == "unavailable"
            result = await connect_dialog(hass, entry)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"gateway_ids": ["000123"]}
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            await eventually(lambda: available(owner, "number", NUMBER))
            restored = registered(hass, "number", NUMBER)
            assert (
                restored.entity_id == original.entity_id
                and restored.device_id == device_id
            )
            assert restored.name == "My room" and restored.icon == "mdi:home"
            assert dr.async_get(hass).async_get(device_id).name_by_user == "My heating"
            assert (
                registered(hass, "sensor", metrics_uid).entity_id == metrics.entity_id
            )
            assert registry.async_get(metrics.entity_id).name == "My request counter"
            assert cloud in entry.data["connections"] and entry.runtime_data is owner
            assert not any(call[0] in {"PUT", "DELETE"} for call in backend.calls)
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("change", ["remove", "disable"])
async def test_changed_access_stops_discovery_before_second_backend(
    hass, backend, change
):
    entry, cloud, _ = existing(hass)
    management = ConfigurationManagement(hass, entry)

    async def changed():
        if change == "remove":
            management.remove(management.edit(cloud))
        else:
            management.set_enabled(management.edit(cloud), False)
        return (Gateway("000123"),)

    backend.gateways.side_effect = changed
    result = await choose_connection(hass, entry, cloud)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_discover"}
    )
    assert result["reason"] == "configuration_changed"
    backend.devices.assert_not_awaited()


def _link(result):
    return result["description_placeholders"]["authorization_url"]


async def test_options_sign_in_keeps_its_link_after_a_paste_mistake(hass, backend):
    """FLOW-2: a mistyped address must not void the completed browser sign-in."""
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    assert first["description_placeholders"]["redirect_scheme"] == (
        urlparse(Brand.BUDERUS.redirect_uri).scheme
    )
    good = _redirect_from_result(first, Brand.BUDERUS)
    manager = hass.config_entries.options
    for wrong in (
        "x",
        "http://[broken",
        f"{Brand.BUDERUS.redirect_uri}?code=one-time-code&state=other-attempt",
    ):
        result = await manager.async_configure(
            first["flow_id"], {"redirect_url": wrong}
        )
        assert result["step_id"] == "cloud_auth"
        assert result["errors"] == {"base": "invalid_redirect"}
        assert _link(result) == _link(first)
    backend.oauth.assert_not_awaited()
    result = await manager.async_configure(first["flow_id"], {"redirect_url": good})
    assert result["step_id"] == "cloud_select"
    backend.oauth.assert_awaited_once()


@pytest.mark.parametrize("submit", [False, True])
async def test_options_sign_in_reports_an_expired_attempt(hass, backend, submit):
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    good = _redirect_from_result(first, Brand.BUDERUS)
    manager = hass.config_entries.options
    with patch(ROOT + ".pointt.auth.monotonic", return_value=10**12):
        result = await manager.async_configure(
            first["flow_id"], {"redirect_url": good} if submit else None
        )
        assert result["step_id"] == "cloud_auth"
        assert result["errors"] == {"base": "auth_expired"}
        assert _link(result) != _link(first)
        backend.oauth.assert_not_awaited()
        result = await manager.async_configure(
            result["flow_id"],
            {"redirect_url": _redirect_from_result(result, Brand.BUDERUS)},
        )
    assert result["step_id"] == "cloud_select"


async def test_options_sign_in_restarts_a_consumed_attempt(hass, backend):
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    good = _redirect_from_result(first, Brand.BUDERUS)
    flow = hass.config_entries.options._progress[first["flow_id"]]
    flow._oauth_flow.consume_redirect(good)
    result = await hass.config_entries.options.async_configure(
        first["flow_id"], {"redirect_url": good}
    )
    assert result["errors"] == {"base": "auth_restart"}
    assert _link(result) != _link(first)
    backend.oauth.assert_not_awaited()


@pytest.mark.parametrize("error", [ValueError("bad"), ServiceUnavailable()])
async def test_options_sign_in_failed_exchange_issues_a_new_link(hass, backend, error):
    entry = empty_entry(hass)
    first = await connect_dialog(hass, entry, authenticate=False)
    backend.oauth.side_effect = error
    result = await hass.config_entries.options.async_configure(
        first["flow_id"], {"redirect_url": _redirect_from_result(first, Brand.BUDERUS)}
    )
    if isinstance(error, ServiceUnavailable):
        assert result["step_id"] == "cloud_retry"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    else:
        assert result["errors"] == {"base": "invalid_auth"}
    assert result["step_id"] == "cloud_auth"
    assert _link(result) != _link(first)


def test_both_sign_in_steps_share_the_guide():
    import json
    from pathlib import Path

    from custom_components.bosch_buderus_heating.cloud_options_flow import (
        CloudOptionsFlow,
    )
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusOptionsFlow,
    )

    assert BoschBuderusConfigFlow._show_auth_form is CloudOptionsFlow._show_auth_form
    assert BoschBuderusOptionsFlow.async_step_auth is (
        BoschBuderusConfigFlow.async_step_auth
    )
    folder = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        strings = json.loads((folder / name).read_text(encoding="utf-8"))
        assert (
            strings["options"]["step"]["cloud_auth"]
            == (strings["config"]["step"]["auth"])
        )
        assert (
            "{redirect_scheme}"
            in strings["options"]["step"]["cloud_auth"]["description"]
        )
        for key in ("auth_expired", "auth_restart", "invalid_redirect"):
            assert strings["options"]["error"][key] == strings["config"]["error"][key]
