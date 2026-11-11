"""D45: a gateway ID is one URL path segment, never a path step."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from yarl import URL

from custom_components.bosch_buderus_heating.pointt.client import (
    PointTClient,
    _encode_gateway_id,
)


@pytest.mark.parametrize(
    "gateway_id",
    [".", "..", " .. ", "1\x002", "1\n2", "1\t2", "\u200b", "1\u20282"],
)
def test_dot_segments_and_control_characters_are_rejected(gateway_id):
    with pytest.raises(ValueError, match="not a valid path segment"):
        _encode_gateway_id(gateway_id)


@pytest.mark.parametrize(
    ("gateway_id", "encoded"),
    [
        ("101012902", "101012902"),
        (" 101012902 ", "101012902"),
        ("a/b", "a%2Fb"),
        ("gw one", "gw%20one"),
        ("...", "..."),
        ("1.2", "1.2"),
    ],
)
def test_ordinary_ids_stay_one_segment(gateway_id, encoded):
    assert _encode_gateway_id(gateway_id) == encoded
    url = URL(f"https://cloud.invalid/api/v1/gateways/{encoded}/resource/x")
    assert url.path.startswith("/api/v1/gateways/")
    assert url.path.endswith("/resource/x")


async def test_no_request_is_sent_for_a_dot_segment_gateway():
    session = Mock(spec=["request", "_middlewares"])
    client = PointTClient(session, "token", base_url="https://cloud.invalid/api")
    with pytest.raises(ValueError):
        await client.get_resource("..", "/system/info")
    session.request.assert_not_called()
