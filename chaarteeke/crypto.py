"""The phone vault: numbers are stored encrypted (Fernet) with only the last four digits in clear.

The key setting may hold several keys separated by commas: the first encrypts, every one of them can
decrypt, so a key is replaced without losing a number (put the new key first, run `rotate_ciphers`, then
drop the old key)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class PhoneVault:
    def __init__(self, key: str | bytes) -> None:
        text = key.decode() if isinstance(key, bytes) else key
        keys = [k.strip() for k in text.split(",") if k.strip()]
        if not keys:
            raise ValueError("phone key is empty")
        self._f = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt(self, number: str) -> str:
        return self._f.encrypt(number.encode()).decode()

    def rotate(self, cipher: str) -> str:
        """The same number encrypted with the first key (for a cipher made with an older one)."""
        try:
            return self._f.rotate(cipher.encode()).decode()
        except InvalidToken as e:
            raise ValueError("phone cipher does not open with these keys") from e

    def decrypt(self, cipher: str) -> str:
        try:
            return self._f.decrypt(cipher.encode()).decode()
        except InvalidToken as e:  # a wrong key must fail loudly
            raise ValueError("phone cipher does not open with this key") from e

    @staticmethod
    def last4(number: str) -> str:
        digits = [c for c in number if c.isdigit()]
        return "".join(digits[-4:]) if len(digits) >= 4 else ""


def demo_key(var_dir: Path) -> str:
    """Demo mode only: a key kept in var/ so a restart can still open the demo vault.

    Written complete and private (0600) under a temporary name, then linked into place, so two servers
    starting together end up with the same key and nobody ever reads a half-written one."""
    p = var_dir / "demo_phone.key"
    if not p.exists():
        var_dir.mkdir(parents=True, exist_ok=True)
        tmp = var_dir / f".demo_phone.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(Fernet.generate_key().decode())
            try:
                os.link(tmp, p)  # fails if another server got there first: then theirs is the key
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    return p.read_text().strip()


def demo_secret(var_dir: Path) -> str:
    """Demo mode only: the session signing key, kept in var/ like the phone key, so a restart does not
    sign the presenter and the visitors out.

    The file is written complete and private (0600) under a temporary name and then linked into place, so
    two servers starting together end up with the same key and nobody ever reads a half-written one. An
    empty file is refused: signing with an empty key would let anyone forge a session, and quietly making a
    new key would sign everybody out."""
    p = var_dir / "demo_secret.key"
    if not p.exists():
        var_dir.mkdir(parents=True, exist_ok=True)
        tmp = var_dir / f".demo_secret.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
            try:
                os.link(tmp, p)  # fails if another server got there first: then theirs is the key
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    key = p.read_text().strip()
    if not key:
        raise RuntimeError(f"{p} is empty: delete it to make a new session key (everyone signs in again)")
    return key
