"""Base58 validation for Solana public keys and signatures (no network, no keys)."""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def _decoded_length(text: str) -> int | None:
    number = 0
    for char in text:
        digit = _INDEX.get(char)
        if digit is None:
            return None
        number = number * 58 + digit
    leading_zeros = len(text) - len(text.lstrip("1"))
    body = (number.bit_length() + 7) // 8
    return leading_zeros + body


def is_valid_pubkey(text: str) -> bool:
    return 32 <= len(text) <= 44 and _decoded_length(text) == 32


def is_valid_signature(text: str) -> bool:
    return 64 <= len(text) <= 88 and _decoded_length(text) == 64


def check_pubkey(text: str) -> str:
    if not isinstance(text, str) or not is_valid_pubkey(text):
        raise ValueError(f"invalid Solana address: {text!r}")
    return text


def check_signature(text: str) -> str:
    if not isinstance(text, str) or not is_valid_signature(text):
        raise ValueError("invalid Solana signature")
    return text
