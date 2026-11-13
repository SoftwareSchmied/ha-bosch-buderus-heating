"""The gateway's token list and the revocation of an old token of ours.

Every test runs against a synthetic TLS gateway on the loopback interface;
no request ever reaches a real gateway.
"""

import json
from copy import deepcopy
from urllib.parse import parse_qs

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.bosch_buderus_heating import local_auth as auth
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.local_auth import (
    CLIENT_NAME,
    MAX_LISTED_TOKENS,
    AuthProblem,
    GatewayToken,
    RevokeState,
    list_gateway_tokens,
    parse_token_list,
    revoke_gateway_token,
    token_management_available,
)
from custom_components.bosch_buderus_heating.local_budget import (
    BUDGET_KEY,
    LocalReadBudget,
    local_budget_manager,
)
from custom_components.bosch_buderus_heating.local_credentials import (
    LocalCredentials,
    StoredLocalAccess,
    TokenOrigin,
    import_local_token,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    PortRole,
)
from tests.helpers.options_menu import outcome
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_probe import CONTEXT
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("socket_enabled")

TOKEN = "synthetic-access-token"
FOREIGN_NAME = "[other](https://example.invalid/x)"
OWN = {
    "token_type": "Bearer",
    "token_id": "tid-own-current",
    "client_name": CLIENT_NAME,
    "created_at": 1775134322,
}
OLD = {
    "token_type": "Bearer",
    "token_id": "tid-own-old",
    "client_name": CLIENT_NAME,
    "created_at": 1770000000,
}
FOREIGN = {
    "token_type": "Bearer",
    "token_id": "tid-foreign",
    "client_name": FOREIGN_NAME,
    "created_at": 1760000000,
    # Never part of the documented answer; must never be kept or shown.
    "access_token": "foreign-secret-value",
}


def _token(entry):
    return GatewayToken(entry["token_id"], entry["client_name"], entry["created_at"])


class Gateway:
    """A synthetic sign-in service with a token list and the revocation."""

    def __init__(self, tokens=(OWN, OLD, FOREIGN), *, current="tid-own-current"):
        self.tokens = [dict(token) for token in tokens]
        self.current = current
        self.revoked_current = False
        self.revoke_status = 200
        self.apply_revoke = True
        self.drop_revoke = False
        self.list_statuses = []
        self.list_body = None
        self.after_list = None
        self.posts = []

    async def __call__(self, head, body, writer):
        method, path, _ = head.split(b"\r\n")[0].decode().split(" ")
        headers = {
            key.lower(): value
            for key, value in (
                line.decode().split(": ", 1) for line in head.split(b"\r\n")[1:] if line
            )
        }
        if headers.get("authorization") != f"Bearer {TOKEN}" or self.revoked_current:
            writer.write(wire_reply(b"{}", status=401))
        elif method == "GET" and path == "/auth/token":
            if self.list_statuses:
                writer.write(wire_reply(b"{}", status=self.list_statuses.pop(0)))
            else:
                writer.write(
                    wire_reply(
                        self.list_body
                        if self.list_body is not None
                        else json.dumps(self.tokens).encode()
                    )
                )
            if self.after_list is not None:
                self.after_list(self)
        elif method == "POST" and path == "/auth/revoke":
            form = parse_qs(body.decode())
            self.posts.append((headers, form))
            if self.apply_revoke and self.revoke_status == 200:
                token_id = form["token_id"][0]
                self.tokens = [t for t in self.tokens if t["token_id"] != token_id]
                if token_id == self.current:
                    self.revoked_current = True
            if self.drop_revoke:
                return
            writer.write(wire_reply(b"{}", status=self.revoke_status))
        else:
            writer.write(wire_reply(b"{}", status=404))
        await writer.drain()


def _paths(requests):
    return [head.split(b"\r\n")[0].split(b" ")[:2] for head, _ in requests]


async def _start(auth_gateway, gateway, *, certificate=0):
    requests, _, binding = await auth_gateway(gateway, certificate=certificate)
    return requests, binding


# --- the list ---------------------------------------------------------------


async def test_list_reads_once_with_the_access_token_and_keeps_no_values(
    auth_gateway,
):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    result = await list_gateway_tokens(CONTEXT, TOKEN, binding, is_current=lambda: True)
    assert result.problem is None
    assert result.tokens == (_token(OWN), _token(OLD), _token(FOREIGN))
    assert [token.own for token in result.tokens] == [True, True, False]
    assert _paths(requests) == [[b"GET", b"/auth/token"]]
    head, body = requests[0]
    assert f"Authorization: Bearer {TOKEN}".encode() in head and body == b""
    for value in (result, *result.tokens):
        text = repr(value)
        assert TOKEN not in text and "tid-" not in text
        assert "foreign-secret-value" not in text and FOREIGN_NAME not in text


async def test_an_empty_list_is_a_list(auth_gateway):
    gateway = Gateway(())
    _, binding = await _start(auth_gateway, gateway)
    result = await list_gateway_tokens(CONTEXT, TOKEN, binding, is_current=lambda: True)
    assert result.tokens == () and result.problem is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"token_id": "1"}',
        json.dumps([{**OWN}, {**OWN}]).encode(),
        json.dumps([{**OWN, "created_at": None}]).encode(),
        json.dumps([{**OWN, "created_at": -1}]).encode(),
        json.dumps([{**OWN, "client_name": 7}]).encode(),
        json.dumps([{**OWN, "token_id": ""}]).encode(),
        json.dumps([{**OWN, "token_id": True}]).encode(),
        json.dumps(
            [{**OWN, "token_id": str(n)} for n in range(MAX_LISTED_TOKENS + 1)]
        ).encode(),
        b"[" + b" " * (auth.MAX_TOKEN_LIST_BODY + 1) + b"]",
    ],
    ids=[
        "not_json",
        "not_a_list",
        "repeated_id",
        "no_time",
        "negative_time",
        "name_not_text",
        "empty_id",
        "boolean_id",
        "too_many",
        "too_large",
    ],
)
async def test_an_unusable_answer_is_no_list(auth_gateway, body):
    gateway = Gateway()
    gateway.list_body = body
    _, binding = await _start(auth_gateway, gateway)
    result = await list_gateway_tokens(CONTEXT, TOKEN, binding, is_current=lambda: True)
    assert result.tokens is None
    assert result.problem == AuthProblem.INVALID_RESPONSE


def test_a_numeric_token_id_is_kept_as_text():
    assert parse_token_list([{**OWN, "token_id": 1}]) == (
        GatewayToken("1", CLIENT_NAME, OWN["created_at"]),
    )


@pytest.mark.parametrize(
    "status,problem",
    [
        (401, AuthProblem.UNAUTHORIZED),
        (403, AuthProblem.FORBIDDEN),
        (404, AuthProblem.NOT_FOUND),
        (429, AuthProblem.RATE_LIMITED),
        (503, AuthProblem.SERVICE_UNAVAILABLE),
        (507, AuthProblem.INVALID_RESPONSE),
    ],
)
async def test_a_refused_list_names_the_reason_and_is_not_repeated(
    auth_gateway, status, problem
):
    gateway = Gateway()
    gateway.list_statuses = [status]
    requests, binding = await _start(auth_gateway, gateway)
    result = await list_gateway_tokens(CONTEXT, TOKEN, binding, is_current=lambda: True)
    assert result.tokens is None and result.problem == problem
    assert len(requests) == 1


async def test_a_changed_certificate_sends_nothing(auth_gateway, certificates):
    gateway = Gateway()
    requests, _ = await _start(auth_gateway, gateway)
    other = CertificateBinding(CONTEXT, PortRole.AUTHENTICATION, certificates[1][1])
    result = await list_gateway_tokens(CONTEXT, TOKEN, other, is_current=lambda: True)
    assert result.problem == AuthProblem.CERTIFICATE_CHANGED
    revoked = await revoke_gateway_token(
        CONTEXT, TOKEN, other, _token(OLD), is_current=lambda: True
    )
    assert revoked.state == RevokeState.NOT_SENT
    assert revoked.problem == AuthProblem.CERTIFICATE_CHANGED
    assert requests == [] and gateway.posts == []


async def test_the_list_waits_for_the_shared_admission(auth_gateway):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    budget = LocalReadBudget(CONTEXT)
    result = await list_gateway_tokens(
        CONTEXT, TOKEN, binding, is_current=lambda: True, budget=budget
    )
    assert result.tokens is not None and len(requests) == 1
    # A paused access sends nothing, neither the list nor a revocation.
    budget.defer(30)
    result = await list_gateway_tokens(
        CONTEXT, TOKEN, binding, is_current=lambda: True, budget=budget
    )
    assert result.problem == AuthProblem.BUDGET_BUSY
    revoked = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True, budget=budget
    )
    assert revoked.state == RevokeState.NOT_SENT
    assert revoked.problem == AuthProblem.BUDGET_BUSY
    assert len(requests) == 1 and gateway.posts == []


async def test_a_closed_dialog_sends_nothing(auth_gateway):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    result = await list_gateway_tokens(
        CONTEXT, TOKEN, binding, is_current=lambda: False
    )
    assert result.problem == AuthProblem.CONTEXT_CHANGED and requests == []


# --- the revocation ---------------------------------------------------------


async def test_an_old_token_of_ours_is_revoked_once_and_read_back(auth_gateway):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.REVOKED
    assert _paths(requests) == [
        [b"GET", b"/auth/token"],
        [b"POST", b"/auth/revoke"],
        [b"GET", b"/auth/token"],
    ]
    ((headers, form),) = gateway.posts
    assert form == {"token_id": ["tid-own-old"], "usage_type": ["private"]}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert [t["token_id"] for t in gateway.tokens] == ["tid-own-current", "tid-foreign"]


async def test_a_foreign_token_is_never_revoked(auth_gateway):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    with pytest.raises(ValueError, match="foreign_token"):
        await revoke_gateway_token(
            CONTEXT, TOKEN, binding, _token(FOREIGN), is_current=lambda: True
        )
    # Our name on a foreign entry's ID does not make it ours.
    disguised = GatewayToken(FOREIGN["token_id"], CLIENT_NAME, FOREIGN["created_at"])
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, disguised, is_current=lambda: True
    )
    assert result.state == RevokeState.LIST_CHANGED
    assert _paths(requests) == [[b"GET", b"/auth/token"]] and gateway.posts == []


@pytest.mark.parametrize("change", ["gone", "recreated", "renamed"])
async def test_a_list_changed_since_it_was_shown_sends_nothing(auth_gateway, change):
    gateway = Gateway()
    old = gateway.tokens[1]
    if change == "gone":
        gateway.tokens.remove(old)
    elif change == "recreated":
        old["created_at"] += 1
    else:
        old["client_name"] = "another client"
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.LIST_CHANGED
    assert _paths(requests) == [[b"GET", b"/auth/token"]] and gateway.posts == []


@pytest.mark.parametrize("applied", [True, False])
async def test_a_lost_answer_is_never_repeated(auth_gateway, applied):
    gateway = Gateway()
    gateway.drop_revoke = True
    gateway.apply_revoke = applied
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    # The read-back decides; the revocation itself went out exactly once.
    assert result.state == (RevokeState.REVOKED if applied else RevokeState.UNCERTAIN)
    assert len(gateway.posts) == 1
    assert [path for path in _paths(requests) if path[0] == b"POST"] == [
        [b"POST", b"/auth/revoke"]
    ]


async def test_a_failed_read_back_leaves_the_outcome_open(auth_gateway):
    gateway = Gateway()

    def fail_next(gateway):
        gateway.after_list = None
        gateway.list_statuses = [500]

    gateway.after_list = fail_next
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.UNCERTAIN
    assert len(gateway.posts) == 1 and len(requests) == 3


async def test_a_refused_revocation_stays_listed(auth_gateway):
    gateway = Gateway()
    gateway.revoke_status = 400
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.REJECTED
    assert len(gateway.posts) == 1 and len(requests) == 3


async def test_a_pause_request_ends_without_a_read_back(auth_gateway):
    gateway = Gateway()
    gateway.revoke_status = 429
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.REJECTED
    assert result.problem == AuthProblem.RATE_LIMITED
    assert len(gateway.posts) == 1 and len(requests) == 2


async def test_revoking_the_access_token_itself_ends_the_access(auth_gateway):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OWN), is_current=lambda: True
    )
    assert result.state == RevokeState.ACCESS_ENDED
    assert len(gateway.posts) == 1 and len(requests) == 3


async def test_nothing_is_sent_when_the_time_left_is_too_short(
    auth_gateway, monkeypatch
):
    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    monkeypatch.setattr(auth, "TOKEN_REVOKE_DEADLINE", 2 * auth.PROBE_TIMEOUT - 1)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.NOT_SENT
    assert result.problem == AuthProblem.TIMEOUT
    assert len(requests) == 1 and gateway.posts == []


# --- the options dialog -----------------------------------------------------


def _entry(hass, certificate, *, imported=False, resource=None, auth_pin=True):
    from tests.test_connection_runtime import entry_for

    entry, _, _ = entry_for(hass)
    data = deepcopy(dict(entry.data))
    fingerprint = certificate.hex()
    access = StoredLocalAccess(
        import_local_token(TOKEN)
        if imported
        else LocalCredentials(TOKEN, TokenOrigin.CREATED, ("open_api.read",)),
        LocalEndpoint("gateway.example.invalid"),
        resource or fingerprint,
        fingerprint if auth_pin else None,
        "000123",
        1_700_000_000,
    )
    data["connections"]["local"] = {**data["connections"]["local"], **access.to_data()}
    hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return entry


@pytest.fixture
def dialog(hass, tmp_path, enable_custom_integrations, auth_gateway, certificates):
    local_budget_manager(hass)._path = tmp_path / BUDGET_KEY

    async def start(gateway, **kwargs):
        requests, _ = await _start(auth_gateway, gateway)
        entry = _entry(hass, certificates[0][1], **kwargs)
        return entry, requests

    return start


async def _open_menu(hass, entry):
    from tests.test_managed_connections import choose_connection

    result = await choose_connection(hass, entry, "local")
    assert result["type"] is FlowResultType.MENU
    return result


async def _open_list(hass, entry):
    result = await _open_menu(hass, entry)
    assert "local_tokens" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "local_tokens"}
    )


def _choices(result):
    schema = result["data_schema"].schema
    for key, selector in schema.items():
        if str(key) == "token":
            return {
                option["label"]: option["value"]
                for option in selector.config["options"]
            }
    return {}


async def test_the_list_shows_only_our_tokens_as_choices(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(gateway)
    before = deepcopy((dict(entry.data), dict(entry.options)))
    result = await _open_list(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local_tokens"
    placeholders = result["description_placeholders"]
    assert placeholders["count"] == "3"
    # Ours by creation time; the foreign one only as escaped text.
    assert placeholders["own"].count("\n- ") == 1
    assert "[other](" not in placeholders["others"]
    assert "\\[other\\]" in placeholders["others"]
    choices = _choices(result)
    assert len(choices) == 2 and set(choices.values()) == {"token_1", "token_2"}
    shown = json.dumps(
        {k: v for k, v in result.items() if k != "data_schema"}, default=str
    )
    assert TOKEN not in shown and "tid-" not in shown
    assert "foreign-secret-value" not in shown
    assert _paths(requests) == [[b"GET", b"/auth/token"]]
    # Nothing of the list is kept: neither in the entry nor in diagnostics.
    assert (dict(entry.data), dict(entry.options)) == before
    diagnostics = json.dumps(
        await async_get_config_entry_diagnostics(hass, entry), default=str
    )
    for value in ("tid-", FOREIGN_NAME, CLIENT_NAME, TOKEN):
        assert value not in diagnostics
    # Without a choice nothing changes.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "connection"
    assert len(requests) == 1 and gateway.posts == []


async def test_an_old_token_is_revoked_only_after_the_confirmation(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(gateway)
    result = await _open_list(hass, entry)
    oldest = next(iter(_choices(result).values()))
    flow = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow, {"token": oldest})
    assert result["step_id"] == "local_token_revoke"
    # Not confirmed: nothing is sent.
    result = await hass.config_entries.options.async_configure(flow, {"confirm": False})
    assert result["step_id"] == "connection" and gateway.posts == []
    result = await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "local_tokens"}
    )
    result = await hass.config_entries.options.async_configure(flow, {"token": oldest})
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert result["step_id"] == "connection"
    assert await outcome(hass, result) == "local_token_revoked"
    ((_, form),) = gateway.posts
    assert form["token_id"] == ["tid-own-old"]
    assert [path[0] for path in _paths(requests)] == [
        b"GET",
        b"GET",
        b"GET",
        b"POST",
        b"GET",
    ]
    assert entry.options["connections"]["local"]["enabled"] is True


async def test_a_value_that_was_not_offered_is_refused(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"token": "token_3"}
        )
    assert gateway.posts == []


async def test_a_list_changed_before_the_confirmation_revokes_nothing(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    gateway.tokens = [t for t in gateway.tokens if t["token_id"] != "tid-own-old"]
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert await outcome(hass, result) == "local_tokens_changed"
    assert gateway.posts == []


async def test_revoking_the_used_token_switches_the_access_off(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    newest = list(_choices(result).values())[-1]
    result = await hass.config_entries.options.async_configure(flow, {"token": newest})
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert await outcome(hass, result) == "local_token_access_ended"
    assert result["step_id"] == "connection"
    assert entry.options["connections"]["local"]["enabled"] is False
    assert "local" in entry.data["connections"] and len(gateway.posts) == 1


async def test_removing_the_access_without_the_choice_sends_nothing(hass, dialog):
    from custom_components.bosch_buderus_heating import async_remove_entry

    gateway = Gateway()
    entry, requests = await dialog(gateway)
    result = await _open_menu(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection_remove"}
    )
    assert result["step_id"] == "connection_remove"
    assert {str(key) for key in result["data_schema"].schema} == {"confirm", "revoke"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert await outcome(hass, result) == "connection_removed"
    assert "local" not in entry.data["connections"]
    await async_remove_entry(hass, entry)
    assert requests == [] and gateway.posts == []


async def test_removing_with_the_choice_revokes_first(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_menu(hass, entry)
    flow = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "connection_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        flow, {"confirm": True, "revoke": True}
    )
    assert result["step_id"] == "local_tokens_remove"
    result = await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    assert result["step_id"] == "local_token_revoke"
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert await outcome(hass, result) == "local_token_revoked_removed"
    assert "local" not in entry.data["connections"]
    assert len(gateway.posts) == 1


async def test_an_unconfirmed_revocation_keeps_the_access(hass, dialog):
    gateway = Gateway()
    gateway.drop_revoke = True
    gateway.apply_revoke = False
    entry, _ = await dialog(gateway)
    result = await _open_menu(hass, entry)
    flow = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "connection_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        flow, {"confirm": True, "revoke": True}
    )
    result = await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert await outcome(hass, result) == "local_token_not_removed"
    assert "local" in entry.data["connections"] and len(gateway.posts) == 1


async def test_removing_with_the_choice_but_no_token_selected_only_removes(
    hass, dialog
):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_menu(hass, entry)
    flow = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow, {"next_step_id": "connection_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        flow, {"confirm": True, "revoke": True}
    )
    result = await hass.config_entries.options.async_configure(flow, {})
    assert await outcome(hass, result) == "connection_removed"
    assert "local" not in entry.data["connections"] and gateway.posts == []


async def test_an_entered_token_uses_the_data_port_certificate(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(gateway, imported=True, auth_pin=False)
    result = await _open_list(hass, entry)
    assert result["step_id"] == "local_tokens"
    assert len(requests) == 1


async def test_another_sign_in_certificate_needs_a_confirmation(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(
        gateway, imported=True, auth_pin=False, resource="61" * 32
    )
    result = await _open_list(hass, entry)
    assert result["step_id"] == "local_tokens_certificate"
    assert requests == []
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["step_id"] == "local_tokens" and len(requests) == 1
    # Trusted for that dialog only; nothing is saved.
    assert (
        entry.data["connections"]["local"]["local_access"]["certificate_binding"][
            "authentication"
        ]
        is None
    )


async def test_a_refused_list_is_shown_as_an_error(hass, dialog):
    gateway = Gateway()
    gateway.list_statuses = [401]
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    assert result["errors"] == {"base": "local_tokens_rejected"}
    assert _choices(result) == {}


def test_only_a_usable_stored_access_offers_the_list():
    assert not token_management_available({})
    assert not token_management_available(
        {"credentials": {"token": "x", "scope": ["open_api.read"]}}
    )


# --- edge cases -------------------------------------------------------------


def test_texts_for_unknown_values():
    from custom_components.bosch_buderus_heating import local_token_flow as flow

    assert flow._problem_error(None) == "local_unexpected"
    assert flow._problem_error(AuthProblem.MEDIA_TYPE) == "local_unexpected"
    assert flow._created(1e20) == "?"
    assert flow._lines([]) == "—"
    shown = flow._lines(str(n) for n in range(flow.MAX_SHOWN_TOKENS + 3))
    assert shown.count("\n") == flow.MAX_SHOWN_TOKENS and shown.endswith("(+3)")


async def test_only_a_sign_in_binding_is_accepted(auth_gateway):
    requests, binding = await _start(auth_gateway, Gateway())
    wrong = CertificateBinding(CONTEXT, PortRole.RESOURCES, binding.sha256)
    with pytest.raises(ValueError, match="authentication_binding_required"):
        await list_gateway_tokens(CONTEXT, TOKEN, wrong, is_current=lambda: True)
    assert requests == []


async def test_a_pause_request_pauses_the_shared_budget(auth_gateway):
    gateway = Gateway()
    gateway.list_statuses = [429]
    _, binding = await _start(auth_gateway, gateway)
    budget = LocalReadBudget(CONTEXT)
    result = await list_gateway_tokens(
        CONTEXT, TOKEN, binding, is_current=lambda: True, budget=budget
    )
    assert result.problem == AuthProblem.RATE_LIMITED
    assert budget.pause_seconds > 0


@pytest.mark.parametrize("failure", ["stale", "storage", "wait"])
async def test_an_admission_failure_sends_nothing(auth_gateway, monkeypatch, failure):
    from custom_components.bosch_buderus_heating import local_budget
    from custom_components.bosch_buderus_heating.local_budget import (
        LocalBudgetError,
    )

    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    budget = LocalReadBudget(CONTEXT)
    active = True
    if failure == "stale":
        active = False
    elif failure == "storage":

        def closed(*args, **kwargs):
            raise LocalBudgetError("local_budget_closed")

        monkeypatch.setattr(budget, "claim", closed)
    else:
        held = budget.claim(exclusive=True)
        assert held is not None
        monkeypatch.setattr(
            local_budget, "SETUP_WAIT_LIMIT", 3 * auth.PROBE_TIMEOUT + 0.2
        )
    expected = {
        "stale": AuthProblem.CONTEXT_CHANGED,
        "storage": AuthProblem.BUDGET_BUSY,
        "wait": AuthProblem.TIMEOUT,
    }[failure]
    revoked = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: active, budget=budget
    )
    assert revoked.state == RevokeState.NOT_SENT and revoked.problem == expected
    if failure == "wait":
        monkeypatch.setattr(local_budget, "SETUP_WAIT_LIMIT", auth.PROBE_TIMEOUT + 0.2)
    listed = await list_gateway_tokens(
        CONTEXT, TOKEN, binding, is_current=lambda: active, budget=budget
    )
    assert listed.problem == expected
    assert requests == [] and gateway.posts == []


async def test_a_revocation_that_could_not_be_sent_is_reported(
    auth_gateway, monkeypatch
):
    from custom_components.bosch_buderus_heating.local_probe import TransportFailure
    from custom_components.bosch_buderus_heating.local_transport import (
        LocalTransportError,
    )

    gateway = Gateway()
    requests, binding = await _start(auth_gateway, gateway)
    inspect = auth._inspect
    calls = []

    async def second_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise LocalTransportError(TransportFailure.CONNECT)
        return await inspect(*args, **kwargs)

    monkeypatch.setattr(auth, "_inspect", second_fails)
    result = await revoke_gateway_token(
        CONTEXT, TOKEN, binding, _token(OLD), is_current=lambda: True
    )
    assert result.state == RevokeState.NOT_SENT
    assert result.problem == AuthProblem.CONNECTION
    assert len(requests) == 1 and gateway.posts == []


async def test_a_silent_gateway_times_out(auth_gateway, monkeypatch):
    import asyncio

    async def silent(head, body, writer):
        await asyncio.sleep(5)

    requests, binding = await _start(auth_gateway, silent)
    monkeypatch.setattr(auth, "REQUEST_TIMEOUT", 0.3)
    result = await list_gateway_tokens(CONTEXT, TOKEN, binding, is_current=lambda: True)
    assert result.problem == AuthProblem.TIMEOUT and len(requests) == 1


async def test_tokens_with_the_same_time_get_distinct_labels(hass, dialog):
    twin = {**OLD, "token_id": "tid-own-twin"}
    gateway = Gateway((OWN, OLD, twin))
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    (selector,) = result["data_schema"].schema.values()
    labels = [option["label"] for option in selector.config["options"]]
    assert len(set(labels)) == 3 and labels[1] == labels[0] + " (2)"


@pytest.mark.parametrize(
    "status,drop,note",
    [
        (400, False, "local_token_revoke_rejected"),
        (200, True, "local_token_revoke_unclear"),
    ],
)
async def test_an_unconfirmed_revocation_is_reported(hass, dialog, status, drop, note):
    gateway = Gateway()
    gateway.revoke_status = status
    gateway.drop_revoke = drop
    gateway.apply_revoke = False
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert await outcome(hass, result) == note
    assert len(gateway.posts) == 1


async def test_a_gateway_out_of_reach_keeps_the_confirmation(hass, dialog, monkeypatch):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    monkeypatch.setattr(LocalEndpoint, "auth_port", 1)
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert result["step_id"] == "local_token_revoke"
    assert result["errors"] == {"base": "local_connection_failed"}
    assert gateway.posts == []


async def test_a_changed_access_ends_the_dialog(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow, {"token": next(iter(_choices(result).values()))}
    )
    data = deepcopy(dict(entry.data))
    data["connections"]["local"]["credentials"]["token"] = "another-token"
    hass.config_entries.async_update_entry(entry, data=data)
    result = await hass.config_entries.options.async_configure(flow, {"confirm": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"
    assert len(requests) == 1 and gateway.posts == []


async def test_a_closed_storage_shows_an_error(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(gateway)
    await local_budget_manager(hass).initialize()
    await local_budget_manager(hass).close()
    result = await _open_list(hass, entry)
    assert result["errors"] == {"base": "local_storage_failed"}
    assert requests == []


async def test_closing_the_dialog_forgets_the_list(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = hass.config_entries.options._progress[result["flow_id"]]
    assert flow._token_access is not None
    hass.config_entries.options.async_abort(result["flow_id"])
    assert flow._token_access is None and flow._token_choices is None
    assert not flow._token_current()


async def test_declining_the_sign_in_certificate_sends_nothing(hass, dialog):
    gateway = Gateway()
    entry, requests = await dialog(
        gateway, imported=True, auth_pin=False, resource="61" * 32
    )
    result = await _open_list(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    assert result["step_id"] == "connection" and requests == []


async def test_an_unreachable_sign_in_port_is_shown(hass, dialog, monkeypatch):
    gateway = Gateway()
    entry, _ = await dialog(gateway, imported=True, auth_pin=False)
    monkeypatch.setattr(LocalEndpoint, "auth_port", 1)
    result = await _open_list(hass, entry)
    assert result["step_id"] == "local_tokens"
    assert result["errors"] == {"base": "local_connection_failed"}
