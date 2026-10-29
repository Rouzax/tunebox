"""Secrets for the admin login: password hashes, TOTP codes, recovery codes,
session tokens and signed cookie values. Pure functions; nothing here
stores anything."""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets
import struct
import urllib.parse

MIN_PASSWORD = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# These parameters need about 33.5 MB; hashlib's default limit is 32 MiB.
SCRYPT_MAXMEM = 64 * 1024 * 1024
TOTP_STEP_S = 30
TOTP_DIGITS = 6
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    params = f"{SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${SCRYPT_MAXMEM}"
    return f"scrypt${params}${_b64(salt)}${_b64(digest)}"


def verify_password(stored: str, password: str) -> bool:
    """The parameters come from the stored string, so they can rise later
    without invalidating older hashes."""
    try:
        kind, n, r, p, maxmem, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=int(maxmem),
            dklen=len(expected),
        )
    except ValueError:  # bad base64 (binascii.Error) is a ValueError too
        return False
    return hmac.compare_digest(actual, expected)


@functools.cache
def dummy_hash() -> str:
    """Checked when there is no account, so a login takes as long either way."""
    return hash_password(secrets.token_urlsafe(16))


def new_totp_secret() -> bytes:
    return secrets.token_bytes(20)


def secret_text(secret: bytes) -> str:
    return base64.b32encode(secret).decode().rstrip("=")


def otpauth_uri(secret: bytes) -> str:
    query = urllib.parse.urlencode(
        {
            "secret": secret_text(secret),
            "issuer": "tunebox",
            "algorithm": "SHA1",
            "digits": TOTP_DIGITS,
            "period": TOTP_STEP_S,
        }
    )
    return f"otpauth://totp/{urllib.parse.quote('tunebox:admin')}?{query}"


def totp(secret: bytes, step: int, digits: int = TOTP_DIGITS) -> str:
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    (value,) = struct.unpack(">I", mac[offset : offset + 4])
    return str((value & 0x7FFFFFFF) % 10**digits).zfill(digits)


def matching_step(secret: bytes, code: str, now: float, after: int) -> int | None:
    """The step the code belongs to, now or one step either side, provided
    it is later than after (the last step used)."""
    if len(code) != TOTP_DIGITS or not (code.isascii() and code.isdigit()):
        return None
    current = int(now // TOTP_STEP_S)
    for step in (current - 1, current, current + 1):
        if step > after and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def new_recovery_codes(count: int = 10) -> list[str]:
    def part() -> str:
        return "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(4))

    return [f"{part()}-{part()}-{part()}" for _ in range(count)]


def _normalise(code: str) -> str:
    return code.strip().lower()


def is_recovery_code(text: str) -> bool:
    parts = _normalise(text).split("-")
    return len(parts) == 3 and all(
        len(part) == 4 and all(c in _RECOVERY_ALPHABET for c in part) for part in parts
    )


def code_hash(code: str) -> str:
    # About 59 random bits each: a salt adds nothing a guesser could feel.
    return hashlib.sha256(_normalise(code).encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mac(key: bytes, payload: str, purpose: str) -> bytes:
    # The purpose is part of what is signed but not of the value: a value
    # signed for one use never verifies for another.
    return hmac.new(key, f"{purpose}\n{payload}".encode(), hashlib.sha256).digest()


def sign(key: bytes, payload: str, purpose: str) -> str:
    return f"{payload}.{_b64(_mac(key, payload, purpose))}"


def unsign(key: bytes, value: str, purpose: str) -> str | None:
    payload, dot, mac = value.rpartition(".")
    if not dot or not payload:
        return None
    try:
        given = _unb64(mac)
    except ValueError:  # bad base64 (binascii.Error) is a ValueError too
        return None
    try:
        expected = _mac(key, payload, purpose)
    except UnicodeEncodeError:  # a lone surrogate: never signed, since sign() encodes too
        return None
    return payload if hmac.compare_digest(given, expected) else None
