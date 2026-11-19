"""The encryption of the local iCom API, checked against self-made vectors."""

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.bosch_buderus_heating import icom_crypto as crypto
from custom_components.bosch_buderus_heating.icom_crypto import (
    IcomCryptoError,
    decrypt,
    derive_key,
    encrypt,
    normalize_gateway_password,
    validate_private_password,
)

GATEWAY = "abcd-efgh-ijkl-mnop"
PRIVATE = "pass word 1"


def test_key_is_two_digests_of_the_passwords_with_the_constant():
    constant = bytes.fromhex(
        "867845e97c4e29dce522b9a7d3a3e07b152bffadddbed7f5ffd842e9895ad1e4"
    )
    expected = (
        hashlib.md5(b"abcdefghijklmnop" + constant).digest()
        + hashlib.md5(constant + b"pass word 1").digest()
    )
    assert derive_key(GATEWAY, PRIVATE) == expected
    assert len(expected) == 32


def test_key_and_cipher_vectors_stay_fixed():
    # Made-up passwords with values produced once by this implementation; they
    # guard against accidental changes (the order of the two parts, the dashes,
    # the fill) that a round trip alone would not notice.
    assert (
        derive_key(GATEWAY, PRIVATE).hex()
        == "40596c742c0fce83f4f07cc444e0db7d18d6bcaf425fa7aacae45b5f44d7e770"
    )
    assert (
        encrypt(derive_key(GATEWAY, PRIVATE), b'{"id":"/gateway/uuid","value":"1"}')
        == b"GLKf3p087nWE1n2lp2sJ7LMXk8Vamr3VmPsCRuOAa2zTH2IAa9+Ucd8iVvX3JFXT"
    )
    assert derive_key("abcd-efgh-ijkl-mnop", "x") != derive_key(
        "abcd-efgh-ijkl-mnoq", "x"
    )
    assert derive_key("a-b-c", "x") == derive_key("abc", "x")


def test_round_trip_for_all_lengths_around_the_block_size():
    key = derive_key(GATEWAY, PRIVATE)
    for size in (1, 15, 16, 17, 31, 32, 33, 200):
        text = (b'{"v":"' + b"y" * size)[:size] + b"}"
        assert decrypt(key, encrypt(key, text)) == text


def test_encryption_matches_a_direct_aes_ecb_with_zero_fill():
    key = derive_key(GATEWAY, PRIVATE)
    text = b'{"id":"/gateway/uuid","value":"1"}'
    padded = text + b"\x00" * (-len(text) % 16)
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    direct = base64.b64encode(cipher.update(padded) + cipher.finalize())
    assert encrypt(key, text) == direct


def test_decrypt_accepts_pkcs7_fill_and_surrounding_whitespace():
    key = derive_key(GATEWAY, PRIVATE)
    text = b'{"value":1}'
    padded = text + bytes([16 - len(text) % 16]) * (16 - len(text) % 16)
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    wire = base64.b64encode(cipher.update(padded) + cipher.finalize())
    assert decrypt(key, b"\n" + wire + b"\r\n") == text


def test_a_wrong_key_gives_noise_not_the_text():
    right = derive_key(GATEWAY, PRIVATE)
    wrong = derive_key(GATEWAY, "another")
    wire = encrypt(right, b'{"value":"secret"}')
    assert b"secret" not in decrypt(wrong, wire)


@pytest.mark.parametrize("body", [b"", b"not base64!", b"QUJD", b"====", "text"])
def test_unusable_answers_are_invalid_responses(body):
    with pytest.raises(IcomCryptoError, match="invalid_response"):
        decrypt(derive_key(GATEWAY, PRIVATE), body)


@pytest.mark.parametrize("value", [None, 5, "", "a b", "ä" * 4, "x" * 200, "-"])
def test_gateway_password_must_be_plain_text(value):
    with pytest.raises(IcomCryptoError, match="invalid_gateway_password"):
        normalize_gateway_password(value)


@pytest.mark.parametrize("value", [None, 5, "", "x" * 200, "tab\there"])
def test_private_password_must_be_printable_text(value):
    with pytest.raises(IcomCryptoError, match="invalid_private_password"):
        validate_private_password(value)


def test_private_password_may_hold_any_printable_character():
    assert validate_private_password("Ünï cödé §$% 1") == "Ünï cödé §$% 1"
    key = derive_key("abcd", "Ünï cödé")
    assert decrypt(key, encrypt(key, b"{}")) == b"{}"


def test_bad_keys_and_payloads_are_rejected():
    with pytest.raises(IcomCryptoError, match="invalid_key"):
        encrypt(b"short", b"{}")
    with pytest.raises(IcomCryptoError, match="invalid_payload"):
        encrypt(derive_key(GATEWAY, PRIVATE), "text")  # type: ignore[arg-type]
    assert crypto.BLOCK_SIZE == 16
