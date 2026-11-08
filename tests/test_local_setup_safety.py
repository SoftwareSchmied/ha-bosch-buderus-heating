"""P39: local setup and token change stay understandable and safe."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating.config_flow import (
    BoschBuderusOptionsFlow,
)
from custom_components.bosch_buderus_heating.configuration import LOCAL_KIND
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_state import LocalCondition

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"


@pytest.mark.parametrize(
    ("problem", "key"),
    [
        ("invalid_credentials", "local_invalid_credentials"),
        ("unauthorized", "local_invalid_credentials"),  # 401 from the sign-in port
        ("forbidden", "local_auth_forbidden"),  # 403 from the sign-in port
        ("token_rejected", "local_token_rejected"),  # 401 from the data port
        ("resource_forbidden", "local_resource_forbidden"),  # 403 from the data port
        ("physical_confirmation_required", "local_unlock_required"),  # 412
        ("token_database_full", "local_token_database_full"),  # 507
        ("token_creation_uncertain", "local_creation_uncertain"),
        ("enrollment_completion_uncertain", "local_completion_uncertain"),
        ("interface", "local_interface_required"),
        ("redirect_rejected", "local_invalid_response"),
        ("http_error", "local_invalid_response"),
        ("identity_invalid", "local_invalid_response"),
    ],
)
def test_each_failure_has_its_own_message(problem, key) -> None:
    assert flow.local_error(problem) == key


def test_every_message_is_translated() -> None:
    keys = {
        "local_auth_forbidden",
        "local_creation_uncertain",
        "local_invalid_credentials",
        "local_unlock_required",
        "local_token_database_full",
        "local_token_rejected",
        "local_resource_forbidden",
    }
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))
        for section in ("config", "options"):
            assert keys <= set(data[section]["error"]), (name, section)
        assert "keep" in data["selector"]["local_method"]["options"], name
        assert data["issues"]["local_certificate_changed"]["description"], name


# --------------------------------------------- keep the current token


def _handler(hass, *, stored: bool):
    handler = BoschBuderusOptionsFlow(SimpleNamespace(entry_id="entry"))
    handler.hass = hass
    handler._local_guard = lambda step: None
    handler._local_edit = SimpleNamespace(
        context=SimpleNamespace(
            identity=SimpleNamespace(connection_id="lan"), endpoint="gateway-a"
        )
    )
    connection = SimpleNamespace(kind=LOCAL_KIND, to_data=lambda: {"local": True})
    snapshot = SimpleNamespace(connections={"lan": connection} if stored else {})
    handler._local_management = lambda: SimpleNamespace(current=lambda: snapshot)
    handler._local_active = lambda: True
    handler._local_start = AsyncMock(return_value={"type": "form", "step_id": "next"})
    return handler


async def test_a_certificate_change_can_keep_the_current_token(hass) -> None:
    handler = _handler(hass, stored=True)
    journal = SimpleNamespace(import_token=AsyncMock(return_value="record"))
    access = SimpleNamespace(
        credentials=SimpleNamespace(token="stored-token"), endpoint="gateway-a"
    )
    offered = []
    original = flow.translated_selection

    def capture(values, key):
        offered.append(tuple(values))
        return original(values, key)

    with (
        patch.object(flow, "load_local_access", return_value=access),
        patch.object(flow, "enrollment_journal", return_value=journal),
        patch.object(flow, "translated_selection", side_effect=capture),
    ):
        await handler.async_step_local_method()
        assert offered[-1] == ("keep", "label", "token")
        result = await handler.async_step_local_method({"method": "keep"})
    assert result["step_id"] == "next"
    assert journal.import_token.await_args.args[1] == "stored-token"
    handler._local_start.assert_awaited_once()


async def test_without_a_stored_token_only_the_usual_methods_are_offered(
    hass,
) -> None:
    handler = _handler(hass, stored=False)
    offered = []
    original = flow.translated_selection

    def capture(values, key):
        offered.append(tuple(values))
        return original(values, key)

    journal = SimpleNamespace(import_token=AsyncMock(return_value="record"))
    with (
        patch.object(flow, "translated_selection", side_effect=capture),
        patch.object(flow, "enrollment_journal", return_value=journal),
    ):
        await handler.async_step_local_method()
        result = await handler.async_step_local_method({"method": "keep"})
    assert offered[-1] == ("label", "token")
    assert result["type"] == "abort"
    journal.import_token.assert_not_awaited()  # nothing to keep: nothing sent


async def test_a_new_address_never_receives_the_stored_token(hass) -> None:
    """Keep is offered only for the same gateway address (review P39)."""
    handler = _handler(hass, stored=True)
    access = SimpleNamespace(
        credentials=SimpleNamespace(token="stored-token"), endpoint="gateway-b"
    )
    with patch.object(flow, "load_local_access", return_value=access):
        assert handler._stored_local_token() is None


async def test_a_failing_keep_is_shown_on_the_form(hass) -> None:
    handler = _handler(hass, stored=True)
    access = SimpleNamespace(
        credentials=SimpleNamespace(token="stored-token"), endpoint="gateway-a"
    )
    journal = SimpleNamespace(
        import_token=AsyncMock(side_effect=ValueError("enrollment_retry_later"))
    )
    with (
        patch.object(flow, "load_local_access", return_value=access),
        patch.object(flow, "enrollment_journal", return_value=journal),
    ):
        result = await handler.async_step_local_method({"method": "keep"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "local_rate_limited"}


# ----------------------------------------------- runtime notices


def test_a_refused_access_is_shown_as_denied() -> None:
    from custom_components.bosch_buderus_heating import connection_status as status

    texts = status._TEXTS
    assert texts["de"]["access_denied"] and texts["en"]["access_denied"]


async def test_a_changed_certificate_creates_a_repair_note(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.managed_local import LocalSource
    from tests.ha_test_helpers import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    source = SimpleNamespace(
        connection=SimpleNamespace(
            factory=SimpleNamespace(hass=hass, entry=entry), identifier="lan"
        ),
        binding_id="local",
    )
    source._certificate_issue_id = lambda: LocalSource._certificate_issue_id(source)
    issue_id = source._certificate_issue_id()
    assert issue_id.startswith("local_certificate_changed_" + entry.entry_id)
    registry = ir.async_get(hass)
    LocalSource._certificate_issue(source, LocalCondition.CERTIFICATE_CHANGED)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and not issue.is_fixable
    assert issue.translation_key == "local_certificate_changed"
    # Scoped like every managed repair, so the orphan cleanup finds it.
    assert issue.data == {
        "entry_id": entry.entry_id,
        "connection_id": "lan",
        "binding_id": "local",
    }
    LocalSource._certificate_issue(source, LocalCondition.UNAVAILABLE)
    assert registry.async_get_issue(DOMAIN, issue_id)
    LocalSource._certificate_issue(source, LocalCondition.READY)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
