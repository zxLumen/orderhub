"""Generate ORDERHUB_PASSWORD_HASH for .env.

Usage:
    python scripts/make_password.py
    python scripts/make_password.py --password 'your-password'
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth import hash_password  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--password", help="password (omit to prompt)")
    args = parser.parse_args()
    password = args.password or getpass.getpass("Password: ")
    if len(password) < 10:
        print("Password must be at least 10 characters.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
