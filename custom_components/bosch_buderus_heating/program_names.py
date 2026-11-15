"""TP-05: the names of the weekly programs A and B (pure, no HA).

``/heatingCircuits/hcN/switchPrograms/nameA`` (``nameB``) is a text the app
codes as its ``valueConfig`` says: ``codingType`` ``base64`` means the text
in ``charset`` (UTF-8; UTF-16 and UTF-32 big-endian; ASCII) as Base64
without line breaks, ``ascii`` the text itself. The app edits a name only
with this coding; without it a name would be stored only on the phone, so
the integration does not offer to change it.

Writing: one PUT ``{"value": <coded text>}``, no automatic retry, read-back
of the decoded name (``rrc_write.RrcStructWrite``). The name is read fresh
and compared with the one shown before; a name changed in the app meanwhile
sends nothing. The same name sends nothing. An empty name is not written
(its meaning, a reset to the app's default name, is not proven). iCom
gateways get no name control: their app does not write names.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from .pointt import PointTClient, Resource, WriteValidationError
from .pointt.exceptions import ProtocolError
from .rrc_write import RrcStructWrite
from .write_policies import RRC_READ_BACK_DELAYS, ContractRejected

NAME_PATTERN: Final = re.compile(
    r"^/heatingCircuits/hc[1-9][0-9]?/switchPrograms/name(?P<program>[AB])$"
)
# The charsets the app codes, by their normalized name, and the codec.
_CODECS: Final = {
    "UTF8": "utf-8",
    "UTF16": "utf-16-be",
    "UTF32": "utf-32-be",
    "ASCII": "ascii",
    "USASCII": "ascii",
}


@dataclass(frozen=True, slots=True)
class ValueConfig:
    """The coding of a program name."""

    charset: str
    coding: str  # "base64" or "ascii"
    max_length: int

    @property
    def codec(self) -> str:
        return _CODECS[self.charset]


def is_name_path(path: str) -> bool:
    return NAME_PATTERN.fullmatch(path) is not None


def value_config(resource: Resource | None) -> ValueConfig | None:
    """The complete, known coding of a name, or ``None``."""
    if resource is None:
        return None
    raw = resource.program_fields.get("valueConfig")
    if not isinstance(raw, dict):
        return None
    charset, coding, length = raw.get("charset"), raw.get("codingType"), raw.get("maxLength")
    if not isinstance(charset, str) or not isinstance(coding, str):
        return None
    normalized = charset.replace("-", "").replace("_", "").upper()
    coding = coding.lower()
    if (
        normalized not in _CODECS
        or coding not in ("base64", "ascii")
        or type(length) is not int
        or length < 0
    ):
        return None
    return ValueConfig(normalized, coding, length)


def config_issue(resource: Resource | None) -> str | None:
    """Why a name has no control, as a diagnostics token, or ``None``."""
    if resource is None or not is_name_path(resource.path):
        return "not_a_name"
    if (
        resource.metadata.resource_type != "stringValue"
        or not resource.has_value
        or not isinstance(resource.value, str)
    ):
        return "name_shape_unexpected"
    if "valueConfig" not in resource.program_fields:
        return "name_no_value_config"
    config = value_config(resource)
    if config is None:
        return "name_coding_unknown"
    if config.max_length == 0:
        return "name_max_length_zero"
    if decoded_name(resource) is None:
        return "name_not_decodable"
    return None


def decoded_name(resource: Resource | None) -> str | None:
    """The name as text: decoded where the coding says so, else as read.

    ``None`` for no reading or a value that cannot be decoded; an empty name
    stays empty.
    """
    if (
        resource is None
        or resource.metadata.resource_type != "stringValue"
        or not resource.has_value
        or not isinstance(resource.value, str)
    ):
        return None
    config = value_config(resource)
    if config is None:
        if "valueConfig" in resource.program_fields:
            # A coding that is not known: the raw text could be Base64.
            return None
        return resource.value
    if config.coding == "ascii":
        return resource.value
    try:
        data = base64.b64decode(resource.value, validate=True)
        return data.decode(config.codec)
    except binascii.Error, ValueError, UnicodeError:
        return None


def display_name(resource: Resource | None) -> str | None:
    """A printable name of at most 64 characters for dialogs and attributes."""
    name = decoded_name(resource)
    if name is None:
        return None
    name = name.strip()
    return name if name and name.isprintable() and len(name) <= 64 else None


def name_issue(text: object, config: ValueConfig) -> str | None:
    """Why a new name cannot be written, as a token, or ``None``."""
    if not isinstance(text, str) or not text:
        return "program_name_empty"
    if len(text) > config.max_length:
        # Characters, not bytes.
        return "program_name_too_long"
    if not text.isprintable():
        return "program_name_charset"
    try:
        text.encode(config.codec)
    except UnicodeEncodeError:
        return "program_name_charset"
    return None


def encode_name(text: str, config: ValueConfig) -> str:
    """The wire value of a name in its coding."""
    if (issue := name_issue(text, config)) is not None:
        raise ContractRejected(issue)
    if config.coding == "ascii":
        return text
    return base64.b64encode(text.encode(config.codec)).decode("ascii")


def name_shows(path: str, text: str) -> Callable[[Resource], bool]:
    """Whether a reading of ``path`` shows the name ``text`` (decoded)."""

    def shows(resource: Resource) -> bool:
        return resource.path == path and decoded_name(resource) == text

    return shows


class ProgramNameService:
    """Fresh preflight, one PUT, read-back of the decoded name."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delays: tuple[float, ...] = RRC_READ_BACK_DELAYS,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.delays = delays

    async def write(
        self,
        gateway_id: str,
        expected: Resource,
        text: str,
        *,
        on_sent: Callable[[], None] | None = None,
    ) -> tuple[Resource, ...]:
        """Write ``text`` over the name ``expected`` was read as."""
        path = expected.path
        if config_issue(expected) is not None or not expected.metadata.writable:
            raise ContractRejected("program_name_unsupported")
        config = value_config(expected)
        assert config is not None
        if expected.metadata.used is False or expected.metadata.available is False:
            raise ContractRejected("program_name_unsupported")
        wire = encode_name(text, config)
        current = await self.client.get_resource(gateway_id, path)
        # The decoded name, the coding and the permission as shown.
        if (
            current.path != path
            or decoded_name(current) != decoded_name(expected)
            or value_config(current) != config
            or not current.metadata.writable
            or current.metadata.used is False
            or current.metadata.available is False
        ):
            raise ContractRejected("program_name_changed")
        if decoded_name(current) == text:
            return (current,)
        shows = name_shows(path, text)

        async def preflight() -> None:
            return None

        async def send(_state: None) -> None:
            try:
                await self.client.put_resource_value(gateway_id, path, wire)
            except ProtocolError:
                # Accepted, but the answer could not be read: reading decides.
                return

        async def confirm(_state: None) -> tuple[Resource, ...] | None:
            reading = await self.client.get_resource(gateway_id, path)
            return (reading,) if shows(reading) else None

        writer = RrcStructWrite(sleep=self.sleep, delays=self.delays)
        return await writer.async_write(
            preflight=preflight, send=send, confirm=confirm, on_sent=on_sent
        )


__all__ = [
    "ProgramNameService",
    "ValueConfig",
    "WriteValidationError",
    "config_issue",
    "decoded_name",
    "display_name",
    "encode_name",
    "is_name_path",
    "name_issue",
    "value_config",
]
