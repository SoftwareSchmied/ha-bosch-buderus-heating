"""Encryption of the local iCom gateway API.

The iCom gateways (KM200 family, IP inside, IP modules, MB LAN 2) answer on
port 80 with text that is AES-256 encrypted and Base64 coded. The key follows
from two passwords: the gateway password of the device sticker and the
private password set in the vendor's device app. This module only derives the
key and converts between the JSON text of a request or answer and its wire
form. It opens no connection and knows no path.

The key is the MD5 digest of the gateway password followed by a fixed
protocol constant, directly followed by the MD5 digest of that constant
followed by the private password (16 + 16 bytes). The cipher is AES in ECB
mode; the plain text is filled with zero bytes up to the block size. MD5 is
only part of this protocol, never a security measure of the integration.

Pure logic: no Home Assistant import, no clock, no state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE: Final = 16
KEY_SIZE: Final = 32
# The fixed constant of the protocol's key derivation.
_PROTOCOL_CONSTANT: Final = bytes.fromhex(
    "867845e97c4e29dce522b9a7d3a3e07b152bffadddbed7f5ffd842e9895ad1e4"
)
MAX_PASSWORD_LENGTH: Final = 128


class IcomCryptoError(ValueError):
    """A fixed code, never a password, key or text of the gateway."""


def normalize_gateway_password(value: object) -> str:
    """The sticker password without the dashes of its groups (xxxx-xxxx-...)."""
    if not isinstance(value, str):
        raise IcomCryptoError("invalid_gateway_password")
    text = value.strip().replace("-", "")
    if (
        not 0 < len(text) <= MAX_PASSWORD_LENGTH
        or not text.isascii()
        or not text.isprintable()
        or any(character.isspace() for character in text)
    ):
        raise IcomCryptoError("invalid_gateway_password")
    return text


def validate_private_password(value: object) -> str:
    """The private password exactly as typed (it may contain any characters)."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_PASSWORD_LENGTH
        or not value.isprintable()
    ):
        raise IcomCryptoError("invalid_private_password")
    return value


def derive_key(gateway_password: str, private_password: str) -> bytes:
    """The 32-byte AES key of a gateway from its two passwords."""
    gateway = normalize_gateway_password(gateway_password).encode()
    private = validate_private_password(private_password).encode()
    first = hashlib.md5(gateway + _PROTOCOL_CONSTANT, usedforsecurity=False)
    second = hashlib.md5(_PROTOCOL_CONSTANT + private, usedforsecurity=False)
    return first.digest() + second.digest()


def _cipher(key: bytes) -> Cipher[modes.ECB]:
    if not isinstance(key, bytes) or len(key) != KEY_SIZE:
        raise IcomCryptoError("invalid_key")
    return Cipher(algorithms.AES(key), modes.ECB())


def encrypt(key: bytes, text: bytes) -> bytes:
    """Zero-fill, encrypt and Base64-code a request body or a test answer."""
    if not isinstance(text, bytes):
        raise IcomCryptoError("invalid_payload")
    padded = text + b"\x00" * (-len(text) % BLOCK_SIZE)
    encryptor = _cipher(key).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize())


def _unpadded(plain: bytes) -> bytes:
    """Remove the zero fill, or a PKCS#7 fill some firmware versions use.

    A JSON text ends with a closing bracket, never with a byte below 17, so
    neither fill can be confused with content.
    """
    stripped = plain.rstrip(b"\x00")
    if stripped:
        count = stripped[-1]
        if 1 <= count <= BLOCK_SIZE and stripped.endswith(bytes([count]) * count):
            return stripped[:-count]
    return stripped


def decrypt(key: bytes, body: bytes) -> bytes:
    """Decode and decrypt an answer into its text.

    The answer is not trusted: a text that is no Base64 or has no whole
    number of blocks is "invalid_response" (no gateway answer at all); what
    a wrong key produces is noise, which the caller recognizes when it is no
    JSON.
    """
    if not isinstance(body, bytes):
        raise IcomCryptoError("invalid_response")
    try:
        raw = base64.b64decode(b"".join(body.split()), validate=True)
    except binascii.Error, ValueError:
        raise IcomCryptoError("invalid_response") from None
    if not raw or len(raw) % BLOCK_SIZE:
        raise IcomCryptoError("invalid_response")
    decryptor = _cipher(key).decryptor()
    return _unpadded(decryptor.update(raw) + decryptor.finalize())
