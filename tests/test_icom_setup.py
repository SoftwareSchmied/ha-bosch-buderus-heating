"""Setting up the local access of an iCom gateway: check, configuration, dialog."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import icom_setup
from custom_components.bosch_buderus_heating.configuration import (
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.icom_access import (
    ICOM_CONTRACT,
    load_icom_access,
)
from custom_components.bosch_buderus_heating.icom_identity import CloudReading
from custom_components.bosch_buderus_heating.icom_setup import (
    IcomSetupError,
    IcomVerification,
    cloud_readings,
    commit_icom_setup,
    normalize_icom_host,
    prepare_icom_configuration,
    verify_icom_access,
)
from tests.helpers.icom_gateway import SimulatedIcomGateway
from tests.helpers.options_menu import outcome
from tests.test_local_flow import supported as supported
from tests.test_local_setup import OWNER, registered, starting

GATEWAY_PASSWORD = "abcd-efgh-ijkl-mnop"
PRIVATE_PASSWORD = "private-secret"
pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


@pytest.fixture
async def gateway(monkeypatch):
    simulated = SimulatedIcomGateway(
        gateway_password=GATEWAY_PASSWORD, private_password=PRIVATE_PASSWORD
    )
    await simulated.start()
    # The simulated gateway listens on the loopback address, which a real
    # entry never accepts.
    monkeypatch.setattr(icom_setup, "normalize_icom_host", lambda value: str(value))
    yield simulated
    await simulated.stop()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as value:
        yield value


async def verify(session, gateway, **changes):
    arguments = {
        "target_id": "target",
        "gateway_id": "000123",
        "host": "127.0.0.1",
        "gateway_password": GATEWAY_PASSWORD,
        "private_password": PRIVATE_PASSWORD,
        "port": gateway.port,
        **changes,
    }
    return await verify_icom_access(session, **arguments)


# ------------------------------------------------------------------ check


async def test_the_check_reads_the_identity_and_keeps_the_proof(gateway, session):
    proof = await verify(session, gateway)
    assert (proof.gateway_id, proof.local_id) == ("000123", "000123")
    assert gateway.requests == ["/gateway/uuid"]
    assert "private" not in repr(proof) and "abcd" not in repr(proof)


async def test_the_cloud_s_own_reading_of_the_id_may_be_the_proof(gateway, session):
    gateway.documents["/gateway/uuid"]["value"] = "ab-99-77"
    # Without it the ID is not the cloud's; with no values to compare the
    # access is only set up unverified (X17).
    unverified = await verify(session, gateway)
    assert unverified.method == "unverified"
    assert unverified.check["uuid_matches_cloud"] is False
    proof = await verify(session, gateway, also_accepted=("AB9977",))
    assert proof.local_id == "ab-99-77" and proof.gateway_id == "000123"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("password", "icom_invalid_auth"),
        ("busy", "icom_busy"),
        ("hang", "icom_cannot_connect"),
        ("error", "icom_cannot_connect"),
        ("html", "icom_cannot_connect"),
    ],
)
async def test_a_failed_check_names_one_fixed_cause_and_saves_nothing(
    gateway, session, monkeypatch, change, code
):
    arguments = {}
    if change == "password":
        arguments["private_password"] = "wrong"
    elif change == "busy":
        gateway.status = 503
    elif change == "hang":
        gateway.hang = True
        from custom_components.bosch_buderus_heating import icom_client

        monkeypatch.setattr(icom_client, "REQUEST_TIMEOUT", 0.3)
    elif change == "error":
        gateway.status = 500
    elif change == "html":
        gateway.raw = b"<html>login</html>"
    with pytest.raises(IcomSetupError) as raised:
        await verify(session, gateway, **arguments)
    assert str(raised.value) == code
    for text in (GATEWAY_PASSWORD, PRIVATE_PASSWORD, "wrong"):
        assert text not in str(raised.value)


async def test_invalid_passwords_are_named_before_any_request(gateway, session):
    with pytest.raises(IcomSetupError, match="invalid_gateway_password"):
        await verify(session, gateway, gateway_password="")
    with pytest.raises(IcomSetupError, match="invalid_private_password"):
        await verify(session, gateway, private_password="")
    assert gateway.requests == []


@pytest.mark.parametrize(
    "host", ["", "127.0.0.1", "localhost", "fe80::1%eth0", "http://x", "a b", None]
)
def test_the_address_must_be_a_usable_local_address(host):
    with pytest.raises(IcomSetupError, match="icom_invalid_host"):
        normalize_icom_host(host)


def test_ordinary_addresses_are_accepted():
    assert normalize_icom_host(" 192.168.1.5 ") == "192.168.1.5"
    assert normalize_icom_host("Gateway.Example.lan") == "gateway.example.lan"


# ----------------------------------------------------------- configuration


def proof(target, **changes):
    values = {
        "target_id": target,
        "gateway_id": "000123",
        "local_id": "000123",
        "host": "192.168.1.20",
        "port": 80,
        "gateway_password": "abcdefghijklmnop",
        "private_password": "private",
        "verified_at": 5.0,
        **changes,
    }
    return IcomVerification(**values)


def test_the_access_is_added_to_the_plant_of_the_cloud():
    before, cloud, target = starting()
    after = prepare_icom_configuration(before, proof(target))
    new = [key for key in after.connections if key not in before.connections]
    assert len(new) == 1
    connection = after.connections[new[0]].to_data()
    assert connection["kind"] == "k40_local"
    assert connection["credentials"] == {
        "gateway_password": "abcdefghijklmnop",
        "private_password": "private",
    }
    assert load_icom_access(connection).host == "192.168.1.20"
    binding = next(b for b in after.bindings.values() if b.connection_id == new[0])
    assert (binding.target_id, binding.adapter) == (target, "k40_local")
    stored = after.to_data()
    identity = stored["targets"][target]["identities"][-1]
    assert identity["namespace"] == "local_system_gateway"
    assert identity["comparison_contract"] == ICOM_CONTRACT
    options = after.to_options()
    assert options["targets"][target]["read_order"][-1] in after.bindings
    # Local values stay off until the user switches them on.
    assert "local_values" not in options["targets"][target]
    # The cloud connection is untouched.
    assert after.connections[cloud] == before.connections[cloud]
    assert after.revision == before.revision + 1


def test_new_data_for_an_existing_icom_access_replaces_it_in_place():
    before, _, target = starting()
    first = prepare_icom_configuration(before, proof(target))
    second = prepare_icom_configuration(
        first, proof(target, host="192.168.1.99", private_password="new-one")
    )
    assert second.connections.keys() == first.connections.keys()
    assert second.bindings.keys() == first.bindings.keys()
    key = next(key for key in second.connections if key not in before.connections)
    data = second.connections[key].to_data()
    assert data["auth_epoch"] == first.connections[key].auth_epoch + 1
    assert data["config_revision"] == first.connections[key].config_revision + 1
    access = load_icom_access(data)
    assert (access.host, access.private_password) == ("192.168.1.99", "new-one")
    order = second.to_options()["targets"][target]["read_order"]
    assert len(order) == len(set(order))


def test_another_kind_of_local_access_is_never_replaced():
    before, _, target = starting(local=True)
    with pytest.raises(IcomSetupError, match="local_already_connected"):
        prepare_icom_configuration(before, proof(target))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"target_id": "unknown"}, "local_target_unavailable"),
        ({"gateway_id": "000999"}, "identity_contract_scope_changed"),
    ],
)
def test_the_plant_must_still_be_the_one_that_was_checked(changes, code):
    before, _, target = starting()
    with pytest.raises(IcomSetupError, match=code):
        prepare_icom_configuration(
            before,
            proof(
                changes.get("target_id", target),
                **{k: v for k, v in changes.items() if k != "target_id"},
            ),
        )


def test_stored_data_with_a_mismatching_gateway_is_rejected():
    before, _, target = starting()
    after = prepare_icom_configuration(before, proof(target))
    data, options = after.to_data(), after.to_options()
    key = next(k for k in data["connections"] if k not in before.connections)
    data["connections"][key]["icom_access"]["gateway_id"] = "000999"
    with pytest.raises(ConfigurationError, match="local_identity_conflict"):
        load_configuration(OWNER, data, options, minor_version=2)


def test_stored_data_needs_the_local_storage_level():
    before, _, target = starting()
    after = prepare_icom_configuration(before, proof(target))
    with pytest.raises(ConfigurationError, match="unsupported_local_storage_version"):
        load_configuration(OWNER, after.to_data(), after.to_options(), minor_version=1)


def test_stored_data_with_invalid_access_is_rejected_with_a_fixed_code():
    before, _, target = starting()
    after = prepare_icom_configuration(before, proof(target))
    data = after.to_data()
    key = next(k for k in data["connections"] if k not in before.connections)
    data["connections"][key]["endpoint"]["port"] = 0
    with pytest.raises(ConfigurationError, match="invalid_icom_endpoint"):
        load_configuration(OWNER, data, after.to_options(), minor_version=2)


async def test_committing_saves_the_access_in_the_entry(hass, supported):
    entry, management, _cloud, target = registered(hass)
    before = management.current()
    saved = commit_icom_setup(management, proof(target))
    assert management.current() == saved != before
    assert entry.minor_version == 2
    assert any("icom_access" in item for item in entry.data["connections"].values())
    assert "private" not in json.dumps(management.current().to_options())


async def test_removing_the_cloud_never_turns_an_icom_system_into_a_local_plant(
    hass, supported
):
    _entry, management, cloud, target = registered(hass)
    commit_icom_setup(management, proof(target))
    management.remove(management.edit(cloud))
    options = management.current().to_options()
    assert "local_plant" not in options["targets"][target]
    # The access stays configured, only without a system to supply.
    assert any(
        binding.adapter == "k40_local"
        for binding in management.current().bindings.values()
    )


async def test_committing_is_refused_while_the_storage_level_is_not_enabled(
    hass, monkeypatch
):
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusConfigFlow,
    )

    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 1)
    entry, management, _, target = registered(hass)
    before = management.current()
    with pytest.raises(ConfigurationError, match="local_setup_not_enabled"):
        commit_icom_setup(management, proof(target))
    assert management.current() == before and entry.minor_version == 1


async def test_committing_needs_the_owner_of_the_entry():
    with pytest.raises(ConfigurationError, match="invalid_configuration_owner"):
        commit_icom_setup(object(), proof("target"))


# ------------------------------------------------------------------ dialog


async def open_icom(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    assert "icom_setup" in result["menu_options"]
    return manager, await manager.async_configure(
        result["flow_id"], {"next_step_id": "icom_setup"}
    )


async def test_the_dialog_saves_a_checked_access_and_reports_it(
    hass, supported, gateway
):
    entry, management, _cloud, _target = registered(hass)
    manager, result = await open_icom(hass, entry)
    # One plant fits: the dialog goes straight to the address and passwords.
    assert result["step_id"] == "icom_access"
    form = {
        "host": "127.0.0.1",
        "gateway_password": GATEWAY_PASSWORD,
        "private_password": PRIVATE_PASSWORD,
    }
    # The simulated gateway answers on its own port, not 80.
    original = icom_setup.verify_icom_access

    async def on_test_port(*args, **kwargs):
        return await original(*args, **{**kwargs, "port": gateway.port})

    import custom_components.bosch_buderus_heating.icom_flow as dialog

    dialog_verify = dialog.verify_icom_access
    dialog.verify_icom_access = on_test_port
    try:
        wrong = await manager.async_configure(
            result["flow_id"], {**form, "private_password": "wrong"}
        )
        assert wrong["step_id"] == "icom_access"
        assert wrong["errors"] == {"base": "icom_invalid_auth"}
        assert len(management.current().connections) == 1
        done = await manager.async_configure(wrong["flow_id"], form)
    finally:
        dialog.verify_icom_access = dialog_verify
    assert await outcome(hass, done) == "icom_connected"
    saved = management.current()
    assert len(saved.connections) == 2
    access = next(
        load_icom_access(c.to_data())
        for c in saved.connections.values()
        if "icom_access" in c.to_data()
    )
    assert (access.gateway_id, access.private_password) == ("000123", PRIVATE_PASSWORD)


async def test_the_dialog_names_an_invalid_address_and_shows_the_form_again(
    hass, supported
):
    entry, _, _, _target = registered(hass)
    manager, result = await open_icom(hass, entry)
    bad = await manager.async_configure(
        result["flow_id"],
        {"host": "", "gateway_password": GATEWAY_PASSWORD, "private_password": "x"},
    )
    assert bad["type"] is FlowResultType.FORM
    assert bad["errors"] == {"base": "icom_invalid_host"}
    assert bad["data_schema"].schema  # the form is shown again


async def test_without_the_local_runtime_the_entry_is_not_offered(hass, monkeypatch):
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusConfigFlow,
    )

    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 1)
    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    assert "icom_setup" not in result["menu_options"]


async def test_an_existing_icom_access_has_its_own_menu_entry(hass, supported):
    entry, management, _cloud, target = registered(hass)
    commit_icom_setup(management, proof(target))
    keys = list(management.current().connections)
    number = 1 + next(
        index
        for index, key in enumerate(keys)
        if "icom_access" in management.current().connections[key].to_data()
    )
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "connections"}
    )
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": f"connection_open_{number}"}
    )
    options = result["menu_options"]
    assert "icom_setup" in options
    # The K40 entries do not apply to an iCom access.
    assert not {"local_setup", "local_tokens"} & set(options)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "icom_setup"}
    )
    # The plant is known from the access; its address is offered again.
    assert result["step_id"] == "icom_access"
    marker = next(key for key in result["data_schema"].schema if key == "host")
    assert marker.default() == "192.168.1.20"
    # The passwords are never offered again.
    assert "abcdefghijklmnop" not in str(result["data_schema"].schema)


# ------------------------------------------------- identity by values (fallback)

FIRMWARE, HARDWARE = "/gateway/versionFirmware", "/gateway/versionHardware"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"
STARTS = "/heatSources/numberOfStarts"
CLOUD = {
    FIRMWARE: CloudReading("01.02.03", 30.0),
    HARDWARE: CloudReading("fixture-hw", 30.0),
    OUTDOOR: CloudReading(5.0, 30.0),
}


@pytest.mark.parametrize("unusable", ["-1", "", "  ", -1, None, "x", ["a"]])
async def test_an_unusable_own_id_falls_back_to_the_values_both_sides_report(
    gateway, session, unusable
):
    gateway.value("/gateway/uuid", unusable)
    proof = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof.method == "values" and proof.gateway_id == "000123"
    # The hardware text stays through firmware updates, so it is the one kept.
    assert proof.fingerprint == (HARDWARE, "fixture-hw")
    assert "fixture-hw" not in repr(proof)


@pytest.mark.parametrize("status", [404, 403])
async def test_a_missing_own_id_falls_back_too(gateway, session, status):
    gateway.path_status["/gateway/uuid"] = status
    proof = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof.method == "values"


async def test_a_usable_own_id_is_all_that_counts(gateway, session):
    proof = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof.method == "uuid" and proof.fingerprint is None
    assert gateway.requests == ["/gateway/uuid"]
    # A different usable ID is compared by values (X17): enough agreement
    # recognises the gateway by them.
    gateway.value("/gateway/uuid", "999999")
    assert (await verify(session, gateway, cloud_readings=CLOUD)).method == "values"


async def test_the_fallback_asks_only_for_paths_the_cloud_has_read(gateway, session):
    gateway.value("/gateway/uuid", "-1")
    await verify(session, gateway, cloud_readings=CLOUD)
    assert sorted(gateway.requests) == sorted(["/gateway/uuid", *CLOUD])
    assert len(gateway.requests) <= 25


@pytest.mark.parametrize(
    "cloud",
    [
        {},  # no cloud reading at all
        {
            FIRMWARE: CloudReading("01.02.03", 30.0),
            HARDWARE: CloudReading("fixture-hw", 30.0),
        },
        {OUTDOOR: CloudReading(5.0, 30.0)},
        {FIRMWARE: CloudReading("01.02.03", 30.0), OUTDOOR: CloudReading(5.0, 4000.0)},
        {FIRMWARE: CloudReading("01.02.03", 30.0), OUTDOOR: CloudReading(7.9, 30.0)},
    ],
)
async def test_too_little_agreement_sets_the_access_up_unverified(
    gateway, session, cloud
):
    gateway.value("/gateway/uuid", "-1")
    proof = await verify(session, gateway, cloud_readings=cloud)
    assert proof.method == "unverified"
    assert proof.check["values_verdict"] == "insufficient"


@pytest.mark.parametrize(
    "cloud",
    [
        {**CLOUD, FIRMWARE: CloudReading("09.09.09", 30.0)},
        {**CLOUD, OUTDOOR: CloudReading(-8.0, 30.0)},
        {**CLOUD, STARTS: CloudReading(99999, 30.0)},
    ],
)
async def test_a_clear_contradiction_is_another_gateway(gateway, session, cloud):
    gateway.value("/gateway/uuid", "-1")
    with pytest.raises(IcomSetupError, match="icom_other_gateway"):
        await verify(session, gateway, cloud_readings=cloud)


async def test_a_failing_read_of_the_fallback_is_a_cause_of_its_own(gateway, session):
    gateway.value("/gateway/uuid", "-1")
    gateway.path_status[OUTDOOR] = 500
    with pytest.raises(IcomSetupError, match="icom_cannot_connect"):
        await verify(session, gateway, cloud_readings=CLOUD)
    # A path the gateway does not offer is only left out: firmware and
    # hardware alone (no live value) are too little, so the access is unverified.
    gateway.path_status[OUTDOOR] = 404
    proof = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof.method == "unverified"
    assert proof.fingerprint == (HARDWARE, "fixture-hw")


def test_the_fallback_is_stored_with_its_fingerprint_and_read_back():
    before, _, target = starting()
    after = prepare_icom_configuration(
        before, proof(target, fingerprint=(HARDWARE, "fixture-hw"))
    )
    key = next(key for key in after.connections if key not in before.connections)
    access = load_icom_access(after.connections[key].to_data())
    assert access.identity_method == "values" and access.identity_path == HARDWARE
    assert access.confirms("Fixture-HW") and not access.confirms("other")
    plain = prepare_icom_configuration(before, proof(target))
    key = next(key for key in plain.connections if key not in before.connections)
    assert load_icom_access(plain.connections[key].to_data()).identity_method == "uuid"


def _manager_with_cloud(monkeypatch, data):
    from custom_components.bosch_buderus_heating import managed_entry

    class Owner:
        connections = SimpleNamespace(
            source_data=lambda key: SimpleNamespace(data=data)
        )

    monkeypatch.setattr(managed_entry, "ManagedEntry", Owner)
    return Owner()


def test_the_cloud_readings_are_taken_from_the_running_cloud_source(monkeypatch):
    before, _, target = starting()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def snapshot(value, minutes, available=True):
        return SimpleNamespace(
            resource=SimpleNamespace(value=value),
            available=available,
            last_success=now - timedelta(minutes=minutes),
        )

    owner = _manager_with_cloud(
        monkeypatch,
        {
            FIRMWARE: snapshot("01.02.03", 2),
            OUTDOOR: snapshot(5.0, 20),
            HARDWARE: snapshot("hw", 1, available=False),
            "/not/asked": snapshot(1, 1),
        },
    )
    found = cloud_readings(owner, before, target, now=now)
    assert found == {
        FIRMWARE: CloudReading("01.02.03", 120.0),
        OUTDOOR: CloudReading(5.0, 1200.0),
    }
    assert cloud_readings(object(), before, target, now=now) == {}
