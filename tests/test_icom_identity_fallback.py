"""X17: a gateway whose own ID is unusable or not the cloud's never breaks the setup.

/gateway/uuid that matches the cloud's gateway ID recognises the gateway as
before. Anything else (no ID, "-1", empty, a number, another ID) leads to the
comparison of values: only a clear contradiction stops the setup; too little
agreement sets the access up unverified. What the gateway reported is kept as
kinds and counts (a text masked) for the debug log and the diagnostics.
"""

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating import icom_setup
from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionRuntime,
)
from custom_components.bosch_buderus_heating.icom_access import (
    IcomAccessError,
    describe_reported_id,
    icom_connection_data,
    load_icom_access,
    safe_setup_check,
)
from custom_components.bosch_buderus_heating.icom_identity import CloudReading
from custom_components.bosch_buderus_heating.icom_plan import UUID_PATH
from custom_components.bosch_buderus_heating.icom_setup import (
    IcomSetupError,
    prepare_icom_configuration,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from tests.helpers.options_menu import outcome
from tests.test_connection_runtime import Harness, entry_for
from tests.test_icom_setup import (
    CLOUD,
    FIRMWARE,
    GATEWAY_PASSWORD,
    HARDWARE,
    PRIVATE_PASSWORD,
    open_icom,
    proof,
    verify,
)
from tests.test_icom_setup import gateway as gateway
from tests.test_icom_setup import session as session
from tests.test_icom_source import fast as fast
from tests.test_icom_source import source, state, until
from tests.test_local_flow import supported as supported
from tests.test_local_setup import registered, starting

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
CONTRADICTION = {**CLOUD, FIRMWARE: CloudReading("09.09.09", 30.0)}
STATIC_ONLY = {FIRMWARE: CloudReading("01.02.03", 30.0)}


# ---------------------------------------------------------- what is noted


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-1", {"kind": "minus_one"}),
        (" -1 ", {"kind": "minus_one"}),
        (-1, {"kind": "minus_one"}),
        (0, {"kind": "placeholder"}),
        ("", {"kind": "empty", "length": 0}),
        ("   ", {"kind": "empty", "length": 3}),
        (123456, {"kind": "number", "length": 6, "chars": "digits"}),
        (None, {"kind": "none"}),
        (True, {"kind": "other", "type": "bool"}),
        (["a"], {"kind": "other", "type": "list"}),
        ("7736901234", {"kind": "text", "length": 10, "chars": "digits"}),
        ("AB12CD34EF", {"kind": "text", "length": 10, "chars": "hex"}),
        ("ab-12-cd", {"kind": "text", "length": 8, "chars": "other"}),
        ("-" * 40, {"kind": "placeholder", "length": 40, "chars": "other"}),
    ],
)
def test_what_the_gateway_reported_is_noted_without_the_id(value, expected):
    noted = describe_reported_id(value)
    assert noted == expected
    # Never a character of the ID itself.
    if isinstance(value, str) and len(value.strip()) > 2:
        assert not any(
            isinstance(item, str) and item and item in value for item in noted.values()
        )


def test_a_path_the_gateway_does_not_offer_is_noted_as_such():
    assert describe_reported_id(None, read=False) == {"kind": "not_offered"}


def test_stored_notes_keep_only_known_short_fields():
    noted = safe_setup_check(
        {
            "uuid": {
                "kind": "text",
                "length": 10,
                "chars": "hex",
                "masked": "AB…EF",
                "value": "-1",
                "serial": "1234567890",
            },
            "uuid_matches_cloud": False,
            "values_verdict": "insufficient",
            "values_matching": 1,
            "values_read": 3,
            "method": "unverified",
            "extra": "x",
        }
    )
    assert noted == {
        "uuid": {"kind": "text", "length": 10, "chars": "hex"},
        "uuid_matches_cloud": False,
        "values_verdict": "insufficient",
        "values_matching": 1,
        "values_read": 3,
        "method": "unverified",
    }
    assert safe_setup_check({"uuid": {"kind": "x" * 40}}) == {"uuid": {}}
    assert safe_setup_check("broken") == {}


# ------------------------------------------------------------------ setup


async def test_the_cloud_s_id_is_the_proof_as_before(gateway, session, caplog):
    caplog.set_level(logging.DEBUG)
    proof_ = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof_.method == "uuid"
    assert proof_.check == {
        "uuid": {"kind": "text", "length": 6, "chars": "digits"},
        "uuid_matches_cloud": True,
        "method": "uuid",
    }
    assert gateway.requests == [UUID_PATH]
    assert "method uuid" in caplog.text and "000123" not in caplog.text


@pytest.mark.parametrize("reported", ["-1", "", "  ", -1, 123, None])
async def test_an_unusable_id_with_agreeing_values_is_recognised_by_them(
    gateway, session, reported
):
    gateway.value(UUID_PATH, reported)
    proof_ = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof_.method == "values"
    assert proof_.check["uuid_matches_cloud"] is None
    assert proof_.check["values_verdict"] == "confirmed"
    assert proof_.check["values_matching"] >= 2


@pytest.mark.parametrize("reported", ["-1", "", 123])
async def test_an_unusable_id_without_cloud_values_is_set_up_unverified(
    gateway, session, reported, caplog
):
    caplog.set_level(logging.DEBUG)
    gateway.value(UUID_PATH, reported)
    proof_ = await verify(session, gateway)
    assert proof_.method == "unverified" and proof_.fingerprint is None
    assert proof_.check["values_verdict"] == "insufficient"
    assert proof_.check["values_matching"] == 0
    assert "method unverified" in caplog.text


async def test_another_id_with_agreeing_values_is_recognised_by_them(
    gateway, session, caplog
):
    caplog.set_level(logging.DEBUG)
    gateway.value(UUID_PATH, "AB98765432")
    proof_ = await verify(session, gateway, cloud_readings=CLOUD)
    assert proof_.method == "values"
    assert proof_.local_id == "000123"
    assert proof_.check["uuid"] == {"kind": "text", "length": 10, "chars": "hex"}
    assert proof_.check["uuid_matches_cloud"] is False
    # Neither the whole own ID nor the cloud's ID reaches the log.
    assert "AB98765432" not in caplog.text and "000123" not in caplog.text


@pytest.mark.parametrize("reported", ["AB98765432", "-1"])
async def test_a_clear_contradiction_still_stops_the_setup(
    gateway, session, reported, caplog
):
    caplog.set_level(logging.DEBUG)
    gateway.value(UUID_PATH, reported)
    with pytest.raises(IcomSetupError, match="icom_other_gateway"):
        await verify(session, gateway, cloud_readings=CONTRADICTION)
    assert "values: contradiction" in caplog.text


async def test_too_little_agreement_keeps_the_fingerprint_it_read(gateway, session):
    gateway.value(UUID_PATH, "AB98765432")
    proof_ = await verify(session, gateway, cloud_readings=STATIC_ONLY)
    assert proof_.method == "unverified"
    assert proof_.fingerprint == (FIRMWARE, "01.02.03")
    assert proof_.check["values_matching"] == 1


async def test_a_gateway_without_the_id_path_is_set_up_too(gateway, session):
    gateway.path_status[UUID_PATH] = 404
    proof_ = await verify(session, gateway)
    assert proof_.method == "unverified"
    assert proof_.check["uuid"] == {"kind": "not_offered"}


@pytest.mark.parametrize("reported", ["000123\n", " 000123 ", "000 123", "00-01\t23"])
async def test_the_cloud_s_id_with_spaces_is_set_up_and_stored_in_a_usable_form(
    hass, supported, gateway, reported
):
    # The gateway's own ID with a line break, spaces or separators is still
    # the cloud's: the dialog saves the access (by its ID) and it loads.
    gateway.value(UUID_PATH, reported)
    entry, management, _cloud, _target = registered(hass)
    manager, result = await open_icom(hass, entry)
    original = icom_setup.verify_icom_access

    async def on_test_port(*args, **kwargs):
        return await original(*args, **{**kwargs, "port": gateway.port})

    import custom_components.bosch_buderus_heating.icom_flow as dialog

    dialog_verify = dialog.verify_icom_access
    dialog.verify_icom_access = on_test_port
    try:
        done = await manager.async_configure(
            result["flow_id"],
            {
                "host": "127.0.0.1",
                "gateway_password": GATEWAY_PASSWORD,
                "private_password": PRIVATE_PASSWORD,
            },
        )
    finally:
        dialog.verify_icom_access = dialog_verify
    assert await outcome(hass, done) == "icom_connected"
    data = next(
        c.to_data()
        for c in management.current().connections.values()
        if "icom_access" in c.to_data()
    )
    access = load_icom_access(data)
    assert access.identity_method == "uuid"
    assert not any(character.isspace() for character in access.local_id)
    assert access.confirms(reported)


# ----------------------------------------------------------- stored access


def _stored(**changes):
    before, _, target = starting()
    after = prepare_icom_configuration(before, proof(target, **changes))
    key = next(key for key in after.connections if key not in before.connections)
    return after.connections[key].to_data()


@pytest.mark.parametrize("fingerprint", [None, (HARDWARE, "fixture-hw")])
def test_an_unverified_access_is_stored_and_read_back(fingerprint):
    data = _stored(
        identity_method="unverified",
        fingerprint=fingerprint,
        check={"uuid": {"kind": "minus_one"}, "method": "unverified"},
    )
    assert data["icom_access"]["identity"] == "unverified"
    access = load_icom_access(data)
    assert access.identity_method == "unverified"
    assert access.identity_path == (HARDWARE if fingerprint else UUID_PATH)
    # Nothing to compare with: no reading ever stops it.
    for reading in ("-1", "", None, "999999", "other-hw", 5):
        assert access.usable(reading) and access.confirms(reading)
    assert access.setup_check == {
        "uuid": {"kind": "minus_one"},
        "method": "unverified",
    }


def test_older_accesses_load_as_before():
    plain = _stored()
    assert "identity" not in plain["icom_access"]
    assert "setup_check" not in plain["icom_access"]
    access = load_icom_access(plain)
    assert access.identity_method == "uuid" and access.setup_check == {}
    values = _stored(fingerprint=(HARDWARE, "fixture-hw"))
    assert load_icom_access(values).identity_method == "values"
    broken = _stored(fingerprint=(HARDWARE, "fixture-hw"))
    del broken["icom_access"]["fingerprint"]
    with pytest.raises(IcomAccessError, match="invalid_icom_identity"):
        load_icom_access(broken)
    broken["icom_access"]["identity"] = "guess"
    with pytest.raises(IcomAccessError, match="invalid_icom_identity"):
        load_icom_access(broken)
    with pytest.raises(IcomAccessError, match="invalid_icom_identity"):
        icom_connection_data(
            host="192.168.1.20",
            gateway_password=GATEWAY_PASSWORD,
            private_password="pin",
            gateway_id="000123",
            verified_at=1.0,
            identity_method="values",
        )


# ----------------------------------------------------------------- running


def build_unverified(hass, gateway, *, fingerprint=None):
    entry, cloud, target = entry_for(hass)
    data = dict(entry.data)
    data["connections"] = dict(data["connections"])
    data["connections"]["local"] = {
        "kind": "k40_local",
        "auth_epoch": 0,
        "config_revision": 1,
        **icom_connection_data(
            host="127.0.0.1",
            port=gateway.port,
            gateway_password=GATEWAY_PASSWORD,
            private_password=PRIVATE_PASSWORD,
            gateway_id="000123",
            verified_at=1.0,
            fingerprint=fingerprint,
            identity_method="unverified",
            setup_check={"uuid": {"kind": "minus_one"}},
        ),
    }
    hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    harness = Harness()
    factory = module.LocalDriverFactory(hass, entry, lambda: runtime)
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": factory},
        sleep=harness.sleep,
    )
    return SimpleNamespace(entry=entry, runtime=runtime, cloud=cloud, target=target)


@pytest.mark.parametrize("change", ["placeholder", "other_id", "not_offered"])
async def test_an_unverified_access_reads_whatever_the_id_says(
    hass, gateway, fast, change
):
    if change == "placeholder":
        gateway.value(UUID_PATH, "-1")
    elif change == "other_id":
        gateway.value(UUID_PATH, "999999")
    else:
        gateway.path_status[UUID_PATH] = 404
    p = build_unverified(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        assert state(p).condition is LocalCondition.READY
        diagnostics = source(p).diagnostics()
        assert diagnostics["identity_method"] == "unverified"
        assert diagnostics["setup_check"] == {"uuid": {"kind": "minus_one"}}
        assert (await source(p).test_connection())[0] == "ok"
    finally:
        await p.runtime.close()


async def test_a_changed_fingerprint_does_not_stop_an_unverified_access(
    hass, gateway, fast
):
    p = build_unverified(hass, gateway, fingerprint=(HARDWARE, "another-hw"))
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        assert state(p).condition is LocalCondition.READY
        assert gateway.requests[0] == HARDWARE
    finally:
        await p.runtime.close()


async def test_the_diagnostics_name_the_stored_form_of_the_private_password(
    hass, gateway, fast
):
    p = build_unverified(hass, gateway)
    p.runtime.start()
    try:
        await until(hass, lambda: state(p) and state(p).current_observations)
        running = source(p)
        running._access = replace(running._access, private_password_variant="trimmed")
        assert running.diagnostics()["private_password"]["variant"] == "trimmed"
    finally:
        await p.runtime.close()
