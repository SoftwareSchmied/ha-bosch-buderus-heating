"""The sign-in with the device label: what the dialog accepts and says."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating.local_auth import (
    AuthProblem,
    AuthState,
    rejection_status,
)
from custom_components.bosch_buderus_heating.local_setup_history import (
    local_setup_history,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateObservation,
    PortRole,
)
from tests.test_local_auth import LABEL
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import start, submit
from tests.test_local_flow import supported as supported
from tests.test_local_setup import TOKEN, registered
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


async def _to_auth(hass, monkeypatch, auth_gateway, statuses=None):
    entry, _, _, target = registered(hass)
    forms = []

    async def reply(head, body, writer):
        forms.append({k: v[0] for k, v in parse_qs(body.decode()).items()})
        payload = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "open_api.read",
        }
        status = statuses.pop(0) if statuses else 200
        writer.write(wire_reply(json.dumps(payload).encode(), status=status))
        await writer.drain()

    _, _, pin = await auth_gateway(reply)

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_auth"
    return entry, manager, result, forms


def _selector_type(result, field):
    for key, selector in result["data_schema"].schema.items():
        if str(key) == field:
            return selector.config["type"]
    raise AssertionError(field)


async def test_the_login_is_visible_and_the_pass_masked(
    hass, supported, journal, auth_gateway, monkeypatch
):
    _, _, result, _ = await _to_auth(hass, monkeypatch, auth_gateway)
    assert _selector_type(result, "login") == "text"
    assert _selector_type(result, "password") == "password"


async def test_spaces_and_line_breaks_at_the_edges_are_removed(
    hass, supported, journal, auth_gateway, monkeypatch
):
    _, manager, result, forms = await _to_auth(hass, monkeypatch, auth_gateway)
    result = await submit(
        hass,
        manager,
        result,
        {
            "login": f" {LABEL.login}\n",
            "password": f"\t{LABEL.password} \r\n",
            "unlocked": True,
        },
    )
    assert result["step_id"] == "local_certificate"
    assert len(forms) == 1
    assert forms[0]["username"] == LABEL.login
    assert forms[0]["password"] == LABEL.password.replace("-", "")


async def test_a_space_inside_a_value_is_an_input_error_and_sends_nothing(
    hass, supported, journal, auth_gateway, monkeypatch
):
    entry, manager, result, forms = await _to_auth(hass, monkeypatch, auth_gateway)
    broken = LABEL.password[:3] + " " + LABEL.password[3:]
    result = await submit(
        hass,
        manager,
        result,
        {"login": LABEL.login, "password": broken, "unlocked": True},
    )
    assert result["step_id"] == "local_auth"
    assert result["errors"] == {"base": "local_invalid_label_format"}
    assert forms == []
    await hass.async_block_till_done()
    (attempt,) = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert attempt["error"] == "local_invalid_label_format"
    assert attempt["detail"] == "not_sent"


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "local_invalid_credentials"), (412, "local_unlock_required")],
)
async def test_a_rejection_names_its_reason_and_status(
    hass, supported, journal, auth_gateway, monkeypatch, status, code
):
    entry, manager, result, forms = await _to_auth(
        hass, monkeypatch, auth_gateway, [status]
    )
    values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
    result = await submit(hass, manager, result, values)
    assert result["errors"] == {"base": code}
    assert len(forms) == 1
    await hass.async_block_till_done()
    (attempt,) = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert (attempt["error"], attempt["detail"]) == (code, f"http_{status}")


def test_the_status_of_each_rejection() -> None:
    assert rejection_status(AuthProblem.INVALID_CREDENTIALS) == 400
    assert rejection_status(AuthProblem.PHYSICAL_CONFIRMATION_REQUIRED) == 412
    assert rejection_status(AuthProblem.RATE_LIMITED) == 429
    assert rejection_status(AuthProblem.TOKEN_DATABASE_FULL) == 507
    assert rejection_status(AuthProblem.TIMEOUT) is None
    assert rejection_status(None) is None


async def test_a_changed_certificate_leads_back_to_its_check(
    hass, supported, journal, monkeypatch
):
    """Instead of the same error after every sign-in attempt (a loop)."""
    entry, _, _, target = registered(hass)
    fingerprints = [b"a" * 32, b"b" * 32]
    inspections = []

    async def inspect(context, role, *, is_current, budget):
        inspections.append(role)
        return CertificateObservation(
            context, role, fingerprints.pop(0), "192.0.2.1", "TLSv1.3"
        )

    async def create(*args, **kwargs):
        return SimpleNamespace(
            state=AuthState.REJECTED,
            problem=AuthProblem.CERTIFICATE_CHANGED,
            resolved=True,
        )

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    monkeypatch.setattr(type(journal), "create", create)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
    result = await submit(hass, manager, result, values)
    assert result["step_id"] == "local_certificate"
    assert result["errors"] == {"base": "local_certificate_changed"}
    assert result["description_placeholders"]["fingerprint"] == (b"b" * 32).hex()
    assert inspections == [PortRole.AUTHENTICATION, PortRole.AUTHENTICATION]
    # Confirming the new certificate returns to the sign-in.
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_auth" and not result["errors"]


async def test_a_failed_check_after_a_changed_certificate_is_retried_as_a_check(
    hass, supported, journal, monkeypatch
):
    """The new check of the sign-in port fails once: retrying checks it again
    instead of a sign-in without a confirmed certificate (a loop)."""
    from custom_components.bosch_buderus_heating.local_transport import (
        LocalTransportError,
        TransportFailure,
    )

    entry, _, _, target = registered(hass)
    answers = [b"a" * 32, LocalTransportError(TransportFailure.TIMEOUT), b"b" * 32]
    inspections, creations = [], []

    async def inspect(context, role, *, is_current, budget):
        inspections.append(role)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return CertificateObservation(context, role, answer, "192.0.2.1", "TLSv1.3")

    async def create(*args, **kwargs):
        creations.append(args)
        return SimpleNamespace(
            state=AuthState.REJECTED,
            problem=AuthProblem.CERTIFICATE_CHANGED,
            resolved=True,
        )

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    monkeypatch.setattr(type(journal), "create", create)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
    result = await submit(hass, manager, result, values)
    assert result["step_id"] == "local_retry"
    assert result["errors"] == {"base": "local_timeout"}
    result = await submit(hass, manager, result, {})
    assert result["step_id"] == "local_certificate"
    assert result["description_placeholders"]["fingerprint"] == (b"b" * 32).hex()
    assert inspections == [PortRole.AUTHENTICATION] * 3
    # Nothing was sent with the label after the first attempt.
    assert len(creations) == 1
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_auth" and not result["errors"]
    attempts = await local_setup_history(hass, entry.entry_id).async_attempts()
    assert all(not str(a["error"]).startswith("unexpected") for a in attempts)


def test_the_texts_give_the_documented_button_time() -> None:
    from pathlib import Path

    component = Path(flow.__file__).parent
    for name, needle in (
        ("translations/en.json", "for 1 second"),
        ("translations/de.json", "1 Sekunde"),
    ):
        texts = json.loads((component / name).read_text(encoding="utf-8"))
        for section in ("config", "options"):
            description = texts[section]["step"]["local_auth"]["description"]
            assert needle in description
            assert needle in texts[section]["error"]["local_unlock_required"]
            assert "two seconds" not in description
            assert "zwei Sekunden" not in description
