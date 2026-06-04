"""Generate a bcrypt hash for the admin password.

Usage:
    python gen_password_hash.py 'my-secret-password'
    python gen_password_hash.py            # prompts interactively (hidden input)

Inside Docker:
    docker compose run --rm app python gen_password_hash.py 'my-secret-password'

Copy the printed hash into the ADMIN_PASSWORD_HASH variable in your .env file.
IMPORTANT: wrap it in SINGLE QUOTES, like:

    ADMIN_PASSWORD_HASH='$2b$12$....'

bcrypt hashes contain '$' characters; without single quotes Docker Compose
interpolates them as variables and silently corrupts the hash (making login
impossible). Single quotes make Compose treat the value literally.
"""
from __future__ import annotations

import getpass
import sys

import bcrypt


def main() -> int:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1

    if not password:
        print("Password must not be empty.", file=sys.stderr)
        return 1

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
    print(hashed.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
