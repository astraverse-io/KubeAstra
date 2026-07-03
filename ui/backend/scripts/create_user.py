"""Create a local auth user in SQLite.

Usage:
    python -m scripts.create_user --username alice --role admin
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import auth  # noqa: E402
import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local KubeAstra Assistant user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--role", default="user", choices=["user", "admin"])
    parser.add_argument("--password", default=None, help="Prefer prompt input; this may be stored in shell history.")
    args = parser.parse_args()

    username = auth.normalize_username(args.username)
    if not username:
        print("username is required", file=sys.stderr)
        return 2

    password = args.password or getpass.getpass("Password: ")
    if not args.password:
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 2

    settings = auth.get_auth_settings()
    if len(password) < settings.password_min_length:
        print(f"password must be at least {settings.password_min_length} characters", file=sys.stderr)
        return 2

    db.init_db()
    try:
        user = db.create_user(
            username=username,
            password_hash=auth.hash_password(password),
            display_name=args.display_name,
            role=args.role,
        )
    except sqlite3.IntegrityError:
        print(f"user already exists: {username}", file=sys.stderr)
        return 1

    print(f"created {user['role']} user: {user['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

