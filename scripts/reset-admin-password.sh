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

If --password is omitted, the script prompts securely inside the backend container.
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

cmd=(
  docker compose exec
  backend
  python
  /app/hermeshq/scripts/reset_admin_password.py
  --username "$USERNAME"
  --display-name "$DISPLAY_NAME"
)

if [[ -n "$PASSWORD" ]]; then
  cmd+=(--password "$PASSWORD")
fi

"${cmd[@]}"
