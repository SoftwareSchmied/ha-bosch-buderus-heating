"""The local setup reads tolerantly what only describes the gateway.

Real options flows of the local setup (A1): a gateway still collecting its
system information is waited for, a brand that cannot be read, another
firmware format, another form of the system information envelope, a pasted
token with ``Bearer``, a copied web address and a pass typed in groups do
not stop the setup. The gateway ID, the certificate and the token stay
strict.
"""

from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs

import pytest

from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating import local_setup as setup
from custom_components.bosch_buderus_heating.local_probe import ProbePath
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateObservation,
    LocalProbeExchange,
)
from tests.test_local_auth import LABEL
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import imported, start, submit
from tests.test_local_flow import supported as supported
from tests.test_local_probe import K40_ROWS, response
from tests.test_local_setup import TOKEN, registered
from tests.test_local_transport import certificates as certificates

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

GATEWAY = "000123"


def answers(context, *, system=None, brand=None, firmware=None):
    """One probe exchange; each argument replaces the answer of one path."""
    rows = []
    for path in ProbePath:
        item = response(path, context=context, gatewayId=GATEWAY)
        if path is ProbePath.SYSTEM and system is not None:
            body = {**json.loads(item.body), **system}
            item = replace(item, body=json.dumps(body).encode())
        elif path is ProbePath.BRAND and brand is not None:
            item = brand(item)
        elif path is ProbePath.FIRMWARE and firmware is not None:
            item = response(path, context=context, value=firmware)
        rows.append(item)
    return LocalProbeExchange(tuple(rows))


def reading(monkeypatch, plan):
    """Answer each read check with the next entry of ``plan`` (keywords of
    ``answers``), the last one repeated; returns the recorded reads."""
    reads = []

    async def read(context, token, binding, *, is_current, budget):
        assert is_current() and token == TOKEN
        reads.append(context)
        return answers(context, **plan[min(len(reads), len(plan)) - 1])

    monkeypatch.setattr(setup, "read_local_probe", read)
    return reads


IN_PROGRESS = {"system": {"status": "in_progress"}}


async def test_a_gateway_still_collecting_its_system_information_is_waited_for(
    hass, supported, journal, monkeypatch
):
    # Real #111: the gateway needed several minutes; the read check now asks
    # again by itself instead of failing.
    monkeypatch.setattr(flow, "INCOMPLETE_SYSTEM_WAIT_SECONDS", 0.0)
    reads = reading(monkeypatch, [IN_PROGRESS, IN_PROGRESS, {}])
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result", result.get("errors")
    assert len(reads) == 3
    result = await submit(hass, manager, result, {})
    assert result["reason"] == "local_setup_complete"


async def test_the_wait_for_the_system_information_ends_with_its_cause(
    hass, supported, journal, monkeypatch
):
    monkeypatch.setattr(flow, "INCOMPLETE_SYSTEM_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(flow, "INCOMPLETE_SYSTEM_CHECKS", 4)
    reads = reading(monkeypatch, [IN_PROGRESS])
    entry, management, _, target = registered(hass)
    before = management.current()
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert len(reads) == 4
    assert result["step_id"] == "local_pending"
    assert result["errors"] == {"base": "local_incomplete_system"}
    assert management.current() == before


def test_the_wait_for_the_system_information_is_bounded():
    # About five minutes in all, then the dialog names the cause.
    waited = (flow.INCOMPLETE_SYSTEM_CHECKS - 1) * flow.INCOMPLETE_SYSTEM_WAIT_SECONDS
    assert 120 <= waited <= 330


def _not_found(item):
    return replace(item, status=404, body=b"")


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param({"brand": _not_found}, id="brand-404"),
        pytest.param(
            {"brand": lambda item: replace(item, body=b"<html>no brand</html>")},
            id="brand-html",
        ),
        pytest.param({"firmware": "16.00.00-rc1"}, id="firmware-suffix"),
        pytest.param({"firmware": "unknown"}, id="firmware-unknown"),
        pytest.param(
            {
                "system": {
                    "timeStamp": "1684201667123",
                    "writeable": True,
                    "status": "Completed",
                    "values": [
                        {"Tok": 8, "Id": 8, "Ver": None, "ProductName": 4711},
                        {"Id": "5", "ModuleSerialNumber": 1.5},
                        {"Tok": "72", "Id": "17", "ModuleSerialNumber": None},
                    ],
                }
            },
            id="system-envelope",
        ),
    ],
)
async def test_what_only_describes_the_gateway_never_stops_the_setup(
    hass, supported, journal, monkeypatch, plan
):
    reads = reading(monkeypatch, [plan])
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result", result.get("errors")
    result = await submit(hass, manager, result, {})
    assert result["reason"] == "local_setup_complete"
    assert len(reads) == 2
    stored = [
        connection.to_data()
        for connection in management.current().connections.values()
        if connection.kind == "k40_local"
    ]
    assert stored[0]["local_access"]["read_evidence"]["gateway_id"] == GATEWAY


@pytest.mark.parametrize(
    ("rows", "firmware", "error"),
    [
        (K40_ROWS, "14.00.01", "local_old_firmware"),
        ([], "01.00.00", None),
        ([{"Tok": "72", "Id": "17", "ModuleHwIdentStr": "K21"}], "01.00.00", None),
    ],
)
async def test_the_minimum_firmware_is_checked_only_for_k30_and_k40(
    hass, supported, journal, monkeypatch, rows, firmware, error
):
    reading(monkeypatch, [{"firmware": firmware, "system": {"values": rows}}])
    entry, management, _, target = registered(hass)
    before = management.current()
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    if error is None:
        assert result["step_id"] == "local_result", result.get("errors")
    else:
        assert result["errors"] == {"base": error}
        assert management.current() == before


@pytest.mark.parametrize(
    "change",
    [
        {"gatewayId": "other gateway"},
        {"gatewayId": None},
        {"values": None},
    ],
)
async def test_the_gateway_id_stays_strict(
    hass, supported, journal, monkeypatch, change
):
    reading(monkeypatch, [{"system": change}])
    entry, management, _, target = registered(hass)
    before = management.current()
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] != "local_result"
    assert result["errors"] == {"base": "local_invalid_response"}
    assert management.current() == before


@pytest.mark.parametrize(
    "typed", [f"Bearer {TOKEN}", f"  {TOKEN}\n", f"bearer   {TOKEN} \r\n"]
)
async def test_a_pasted_token_is_taken_without_its_decoration(
    hass, supported, journal, typed
):
    # The fixture's read check accepts only the token itself.
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": typed})
    assert result["step_id"] == "local_certificate_data", result.get("errors")
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"


@pytest.mark.parametrize("typed", [f"Bearer{TOKEN}", f"{TOKEN} more", "Bearer x y"])
async def test_a_token_with_other_text_inside_is_still_refused(
    hass, supported, journal, typed
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": typed})
    if typed == f"Bearer{TOKEN}":
        # One word: a token of its own, which the gateway then refuses.
        assert result["step_id"] == "local_certificate_data"
    else:
        assert result["errors"] == {"token": "local_invalid_token"}


@pytest.mark.parametrize(
    "typed",
    [
        "https://Gateway.Example.Invalid",
        "gateway.example.invalid:9443",
        "https://gateway.example.invalid:9443/",
        " http://gateway.example.invalid/ ",
    ],
)
async def test_a_copied_web_address_is_taken_as_the_address(
    hass, supported, journal, typed
):
    entry, _, _, target = registered(hass)
    _, reads = supported
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": typed})
    assert result["step_id"] == "local_method", result.get("errors")
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": TOKEN})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    assert reads[-1].endpoint.host == "gateway.example.invalid"


@pytest.mark.parametrize(
    "typed", ["gateway.example.invalid:8080", "gateway.example.invalid/path"]
)
async def test_another_port_or_a_path_is_still_refused(hass, supported, journal, typed):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": typed})
    assert result["errors"] == {"address": "local_invalid_address"}


async def test_a_pass_typed_in_groups_is_sent_without_spaces(
    hass, supported, journal, auth_gateway, monkeypatch
):
    calls, _, pin = await auth_gateway()

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_auth"
    grouped = " ".join(LABEL.password[i : i + 4] for i in range(0, 25, 4)) + "\t"
    result = await submit(
        hass,
        manager,
        result,
        {"login": f" {LABEL.login} ", "password": grouped, "unlocked": True},
    )
    assert len(calls) == 1, result.get("errors")
    form = parse_qs(calls[0][1].decode())
    assert form["username"] == [LABEL.login]
    assert form["password"] == [LABEL.password.replace("-", "")]
    assert result["step_id"] == "local_certificate_data"


async def test_a_login_with_a_space_inside_is_still_refused(
    hass, supported, journal, auth_gateway, monkeypatch
):
    calls, _, pin = await auth_gateway()

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    result = await submit(
        hass,
        manager,
        result,
        {"login": "synthetic login", "password": LABEL.password, "unlocked": True},
    )
    assert result["errors"] == {"base": "local_invalid_label_format"}
    assert not calls
