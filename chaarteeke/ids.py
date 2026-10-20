"""Time-sortable ids: <prefix>_<13 base32 chars of ms time><8 random base32 chars>."""

from __future__ import annotations

import secrets

from . import clock

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    ms = int(clock.now().timestamp() * 1000)
    return f"{prefix}_{_b32(ms, 10)}{_b32(secrets.randbits(40), 8)}"
