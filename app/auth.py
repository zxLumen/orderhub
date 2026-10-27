"""Single-user password login and signed session cookies.

Designed for a public single-user deployment:
- password is stored as a scrypt hash in .env
- the browser gets an HttpOnly, SameSite=Lax session cookie
- API clients (collector agent, iOS Shortcuts) keep using the Bearer token
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

COOKIE_NAME = "orderhub_session"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if not password or not encoded:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def create_session(secret: str, ttl_seconds: int) -> str:
    payload = _b64(
        json.dumps(
            {"exp": int(time.time()) + max(60, ttl_seconds), "v": 1},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{payload}.{_sign(secret, payload)}"


def verify_session(secret: str, value: str) -> bool:
    if not secret or not value or "." not in value:
        return False
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return False
    try:
        data: dict[str, Any] = json.loads(_unb64(payload).decode("utf-8"))
        return int(data.get("exp", 0)) >= int(time.time())
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
