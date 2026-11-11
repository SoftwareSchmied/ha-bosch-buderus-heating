"""D14: the cloud transport keeps the shared session's middleware chain."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.bosch_buderus_heating.pointt.exceptions import TransportError
from custom_components.bosch_buderus_heating.pointt.transport import (
    PointTTransport,
    _session_middlewares,
)


async def test_home_assistant_session_still_exposes_its_middleware_chain(
    hass: HomeAssistant,
) -> None:
    """Fails early when an aiohttp or HA update renames the private chain."""
    session = async_get_clientsession(hass)
    chain = _session_middlewares(session)
    assert isinstance(chain, tuple)
    ssrf = getattr(aiohttp_client, "_ssrf_redirect_middleware", None)
    if ssrf is not None:
        # HA's URL checks are part of the chain the transport carries over.
        assert ssrf in chain


async def test_a_missing_chain_stops_the_request_with_a_clear_error() -> None:
    session = Mock(spec=["request"])
    transport = PointTTransport(session, base_url="https://cloud.invalid/api")
    with pytest.raises(TransportError, match="middleware chain"):
        await transport.request_json("GET", "gateways/", "token")
    session.request.assert_not_called()
    assert transport.metrics.snapshot()["outcomes"] == {"internal_error": 1}


def test_any_sequence_chain_is_carried_over_unchanged() -> None:
    def first(request, handler):  # pragma: no cover - never called
        return handler(request)

    assert _session_middlewares(SimpleNamespace(_middlewares=[first])) == (first,)
    assert _session_middlewares(SimpleNamespace(_middlewares=())) == ()
