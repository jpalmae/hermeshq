#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jpalmae/hermeshq.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/hermeshq}"
HERMESHQ_HOST="${HERMESHQ_HOST:-}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
ADMIN_DISPLAY_NAME="${ADMIN_DISPLAY_NAME:-Hermes Operator}"
POSTGRES_DB="${POSTGRES_DB:-hermeshq}"
POSTGRES_USER="${POSTGRES_USER:-hermeshq}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3420}"
SKIP_START="${SKIP_START:-0}"
TMP_DIR=""

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

cleanup_tmp_dir() {
  if [ -n "${TMP_DIR:-}" ] && [ -d "${TMP_DIR:-}" ]; then
    rm -rf "$TMP_DIR"
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    fail "Docker Compose is required"
  fi
}

random_hex() {
  python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
}

random_password() {
  python3 - <<'PY'
import secrets
import string

upper = secrets.choice(string.ascii_uppercase)
lower = secrets.choice(string.ascii_lowercase)
digit = secrets.choice(string.digits)
special = secrets.choice("!@#$%^&*()-_=+")
rest = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
password = list(upper + lower + digit + special + rest)
secrets.SystemRandom().shuffle(password)
print(''.join(password))
PY
}

detect_host() {
  if [ -n "$HERMESHQ_HOST" ]; then
    printf '%s\n' "$HERMESHQ_HOST"
    return
  fi

  if command -v hostname >/dev/null 2>&1; then
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i !~ /^127\./) {print $i; exit}}')"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return
    fi
  fi

  hostname -f 2>/dev/null || hostname
}

archive_url_from_repo() {
  local repo_no_git
  repo_no_git="${REPO_URL%.git}"
  case "$repo_no_git" in
    https://github.com/*/*)
      printf '%s/archive/refs/heads/%s.tar.gz\n' "$repo_no_git" "$BRANCH"
      ;;
    *)
      fail "Unsupported REPO_URL for auto-download: $REPO_URL"
      ;;
  esac
}

write_env_file() {
  local install_host="$1"
  local jwt_secret db_password admin_password api_base cors_json database_url

  jwt_secret="$(random_hex)"
  db_password="${POSTGRES_PASSWORD:-$(random_password)}"
  admin_password="${ADMIN_PASSWORD:-$(random_password)}"
  api_base="${VITE_API_BASE_URL:-http://${install_host}:${BACKEND_PORT}/api}"
  cors_json=$(printf '["http://%s:%s","http://localhost:%s","http://frontend"]' "$install_host" "$FRONTEND_PORT" "$FRONTEND_PORT")
  database_url="postgresql+asyncpg://${POSTGRES_USER}:${db_password}@localhost:${POSTGRES_PORT}/${POSTGRES_DB}"

  cat >"$INSTALL_DIR/.env" <<EOF
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${db_password}
POSTGRES_PORT=${POSTGRES_PORT}
DATABASE_URL=${database_url}
JWT_SECRET=${jwt_secret}
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${admin_password}
ADMIN_DISPLAY_NAME=${ADMIN_DISPLAY_NAME}
BACKEND_PORT=${BACKEND_PORT}
FRONTEND_PORT=${FRONTEND_PORT}
CORS_ORIGINS_JSON=${cors_json}
WORKSPACES_ROOT=./workspaces
BRANDING_ROOT=./workspaces/_branding
PTY_SHELL=/bin/sh
VITE_API_BASE_URL=${api_base}
EOF

  printf '\nHermesHQ admin bootstrap credentials\n'
  printf '  username: %s\n' "$ADMIN_USERNAME"
  printf '  password: %s\n' "$admin_password"
}

main() {
  need_cmd curl
  need_cmd tar
  need_cmd python3
  need_cmd docker

  docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable"
  compose version >/dev/null 2>&1 || true

  local install_host archive_url src_root src_dir existing_env
  install_host="$(detect_host)"
  archive_url="$(archive_url_from_repo)"
  TMP_DIR="$(mktemp -d)"
  existing_env=""

  trap cleanup_tmp_dir EXIT

  if [ -f "$INSTALL_DIR/.env" ]; then
    existing_env="$TMP_DIR/existing.env"
    cp "$INSTALL_DIR/.env" "$existing_env"
  fi

  printf 'Downloading HermesHQ from %s\n' "$archive_url"
  curl -fsSL "$archive_url" -o "$TMP_DIR/hermeshq.tar.gz"
  mkdir -p "$TMP_DIR/src"
  tar -xzf "$TMP_DIR/hermeshq.tar.gz" -C "$TMP_DIR/src"
  src_root="$(find "$TMP_DIR/src" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$src_root" ] || fail "Failed to extract repository archive"

  rm -rf "$INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  mv "$src_root" "$INSTALL_DIR"

  if [ -n "$existing_env" ]; then
    cp "$existing_env" "$INSTALL_DIR/.env"
    printf 'Reused existing %s/.env\n' "$INSTALL_DIR"
  else
    write_env_file "$install_host"
  fi

  cd "$INSTALL_DIR"
  if [ "$SKIP_START" = "1" ]; then
    printf '\nHermesHQ extracted to %s\n' "$INSTALL_DIR"
    printf 'Skipped docker compose startup because SKIP_START=1\n'
    exit 0
  fi

  printf '\nStarting HermesHQ in %s\n' "$INSTALL_DIR"
  compose up --build -d

  printf '\nHermesHQ is starting\n'
  printf '  frontend: http://%s:%s\n' "$install_host" "$FRONTEND_PORT"
  printf '  backend:  http://%s:%s\n' "$install_host" "$BACKEND_PORT"
}

main "$@"
