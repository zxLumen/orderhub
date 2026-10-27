"""Generate VAPID keys for Web Push.

Requires cryptography (installed with pywebpush):
    pip install -r requirements-webpush.txt
    python scripts/make_vapid_keys.py
"""

from __future__ import annotations

import base64
import sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("Install pywebpush first: pip install -r requirements-webpush.txt", file=sys.stderr)
    raise SystemExit(1)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_value = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    print(f"ORDERHUB_VAPID_PUBLIC_KEY={b64url(public_value)}")
    print(f"ORDERHUB_VAPID_PRIVATE_KEY={b64url(private_value)}")
    print("ORDERHUB_VAPID_SUBJECT=mailto:you@example.com")


if __name__ == "__main__":
    main()
