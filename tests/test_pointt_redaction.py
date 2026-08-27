from __future__ import annotations

from custom_components.bosch_buderus_heating.pointt import (
    anonymize_identifier,
    redact_mapping,
    redact_text,
)
from custom_components.bosch_buderus_heating.pointt.redaction import (
    resource_path_template,
)


def test_mapping_redacts_credentials_and_private_data_recursively() -> None:
    source = {
        "access_token": "secret-access",
        "gatewayId": "gateway-123",
        "email": "owner@example.test",
        "nested": {"refresh_token": "secret-refresh", "temperature": 21.5},
        "items": [{"serial": "serial-123"}],
        "unsupported": object(),
    }
    redacted = redact_mapping(source, salt=b"installation salt")
    rendered = repr(redacted)
    assert "secret" not in rendered
    assert "owner@example.test" not in rendered
    assert "gateway-123" not in rendered
    assert redacted["nested"] == {"refresh_token": "<redacted>", "temperature": 21.5}
    assert anonymize_identifier("gateway-123", salt=b"installation salt").startswith(
        "id:"
    )


def test_text_redacts_common_sensitive_patterns() -> None:
    text = (
        "Bearer abc.def token eyJheader.payload.signature owner@example.test "
        "aa:bb:cc:dd:ee:ff 192.168.1.2 https://callback?code=secret&state=secret2"
    )
    redacted = redact_text(text)
    for secret in (
        "abc.def",
        "eyJheader.payload.signature",
        "owner@example.test",
        "aa:bb:cc:dd:ee:ff",
        "192.168.1.2",
        "code=secret",
        "state=secret2",
    ):
        assert secret not in redacted


def test_resource_path_template_removes_installation_identifiers() -> None:
    assert resource_path_template("/heatingCircuits/private-id/status") == (
        "/heatingCircuits/{hc}/status"
    )
    assert resource_path_template("/devices/private-device/errors") == (
        "/devices/{device}/errors"
    )
