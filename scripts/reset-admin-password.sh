#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USERNAME="${HERMESHQ_ADMIN_USERNAME:-admin}"
PASSWORD="${HERMESHQ_ADMIN_PASSWORD:-}"
DISPLAY_NAME="${HERMESHQ_ADMIN_DISPLAY_NAME:-Hermes Operator}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/reset-admin-password.sh [--username admin] [--password 'NewPass123!'] [--display-name 'Hermes Operator']

Defaults:
  --username      admin
  --display-name  Hermes Operator

If --password is omitted, the script prompts securely on the host and then applies the change inside the backend container.
This script expects the Docker Compose-based HermesHQ installation.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --username)
      USERNAME="${2:?missing value for --username}"
      shift 2
      ;;
    --password)
      PASSWORD="${2:?missing value for --password}"
      shift 2
      ;;
    --display-name)
      DISPLAY_NAME="${2:?missing value for --display-name}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is required." >&2
  exit 1
fi

if ! docker compose ps backend >/dev/null 2>&1; then
  echo "Error: docker compose backend service not found from $ROOT_DIR." >&2
  exit 1
fi

if [[ -z "$PASSWORD" ]]; then
  read -r -s -p "New admin password: " PASSWORD
  echo
  read -r -s -p "Confirm password: " PASSWORD_CONFIRM
  echo
  if [[ "$PASSWORD" != "$PASSWORD_CONFIRM" ]]; then
    echo "Error: passwords do not match." >&2
    exit 1
  fi
fi

HERMESHQ_RESET_USERNAME="$USERNAME" \
HERMESHQ_RESET_PASSWORD="$PASSWORD" \
HERMESHQ_RESET_DISPLAY_NAME="$DISPLAY_NAME" \
docker compose exec -T \
  -e HERMESHQ_RESET_USERNAME \
  -e HERMESHQ_RESET_PASSWORD \
  -e HERMESHQ_RESET_DISPLAY_NAME \
  backend \
  python - <<'PY'
import asyncio
import os
import sys

from sqlalchemy import select

from hermeshq.core.security import hash_password
from hermeshq.database import AsyncSessionLocal
from hermeshq.models.user import User
from hermeshq.schemas.user_management import _validate_password_strength


async def main() -> int:
    username = os.environ["HERMESHQ_RESET_USERNAME"]
    password = os.environ["HERMESHQ_RESET_PASSWORD"]
    display_name = os.environ.get("HERMESHQ_RESET_DISPLAY_NAME", "Hermes Operator")

    _validate_password_strength(password)

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
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
PY
