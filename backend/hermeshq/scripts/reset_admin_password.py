from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from hermeshq.core.security import hash_password
from hermeshq.database import AsyncSessionLocal
from hermeshq.models.user import User
from hermeshq.schemas.user_management import _validate_password_strength


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset or create a HermesHQ admin user's password.",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="Admin username to reset. Defaults to 'admin'.",
    )
    parser.add_argument(
        "--password",
        help="New password. If omitted, the script prompts securely.",
    )
    parser.add_argument(
        "--display-name",
        default="Hermes Operator",
        help="Display name to use if the admin user must be created.",
    )
    return parser.parse_args()


def resolve_password(raw_password: str | None) -> str:
    password = raw_password
    if not password:
        password = getpass.getpass("New admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise ValueError("Passwords do not match")
    _validate_password_strength(password)
    return password


async def reset_admin_password(username: str, password: str, display_name: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
            )
            session.add(user)
            action = "created"
        else:
            user.password_hash = hash_password(password)
            user.role = "admin"
            user.is_active = True
            if not user.display_name:
                user.display_name = display_name
            action = "updated"

        await session.commit()
        print(f"Admin password {action} successfully for '{username}'.")


def main() -> int:
    try:
        args = parse_args()
        password = resolve_password(args.password)
        asyncio.run(reset_admin_password(args.username, password, args.display_name))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
