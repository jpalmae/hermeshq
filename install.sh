#!/usr/bin/env bash
set -Eeuo pipefail

# Install script revision: 2026-04-30

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
DOCKER_PREFIX=()
FRESH_INSTALL=0
STARTUP_ATTEMPTED=0
INSTALL_BACKUP_DIR=""
RESTORE_BACKUP_ON_FAILURE=0
FINAL_ADMIN_USERNAME=""
FINAL_ADMIN_PASSWORD=""
DOCKER_GROUP_UPDATED=0
USED_SUDO_DOCKER=0

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

cleanup_tmp_dir() {
  if [ -n "${TMP_DIR:-}" ] && [ -d "${TMP_DIR:-}" ]; then
    rm -rf "$TMP_DIR"
  fi
}

on_error() {
  local exit_code=$?
  local down_flags=("--remove-orphans" "--rmi" "local")

  trap - ERR EXIT

  printf '\nHermesHQ installation failed.\n' >&2

  if [ "$STARTUP_ATTEMPTED" = "1" ] && [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    printf 'Collecting recent Docker status...\n' >&2
    (
      cd "$INSTALL_DIR"
      compose ps || true
      compose logs --tail=80 || true
    ) >&2 || true
  fi

  if [ "$STARTUP_ATTEMPTED" = "1" ] && [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    if [ "$FRESH_INSTALL" = "1" ]; then
      down_flags+=("--volumes")
    fi
    (
      cd "$INSTALL_DIR"
      compose down "${down_flags[@]}"
    ) >/dev/null 2>&1 || true
  fi

  if [ "$RESTORE_BACKUP_ON_FAILURE" = "1" ] && [ -n "$INSTALL_BACKUP_DIR" ] && [ -d "$INSTALL_BACKUP_DIR" ]; then
    printf 'Restoring previous installation at %s...\n' "$INSTALL_DIR" >&2
    rm -rf "$INSTALL_DIR"
    mv "$INSTALL_BACKUP_DIR" "$INSTALL_DIR"
    if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
      (
        cd "$INSTALL_DIR"
        compose up -d
      ) >/dev/null 2>&1 || true
    fi
  elif [ "$FRESH_INSTALL" = "1" ] && [ -d "$INSTALL_DIR" ]; then
    printf 'Cleaning up failed fresh installation...\n' >&2
    rm -rf "$INSTALL_DIR"
  fi

  cleanup_tmp_dir
  exit "$exit_code"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "This step requires root privileges and sudo is not installed"
  fi
}

docker_cmd() {
  if [ "${#DOCKER_PREFIX[@]}" -gt 0 ]; then
    "${DOCKER_PREFIX[@]}" docker "$@"
  else
    docker "$@"
  fi
}

compose() {
  if docker_cmd compose version >/dev/null 2>&1; then
    docker_cmd compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    if [ "${#DOCKER_PREFIX[@]}" -gt 0 ]; then
      "${DOCKER_PREFIX[@]}" docker-compose "$@"
    else
      docker-compose "$@"
    fi
  else
    fail "Docker Compose is required"
  fi
}

adopt_existing_docker_context() {
  if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
    return
  fi

  local current_ids sudo_ids
  current_ids="$( (cd "$INSTALL_DIR" && compose ps -q 2>/dev/null) || true )"
  if [ -n "$current_ids" ]; then
    return
  fi

  if [ "$(id -u)" -eq 0 ] || ! command -v sudo >/dev/null 2>&1; then
    return
  fi

  sudo_ids="$(sudo -n docker compose -f "$INSTALL_DIR/docker-compose.yml" ps -q 2>/dev/null || true)"
  if [ -n "$sudo_ids" ]; then
    printf 'Detected an existing HermesHQ stack managed by sudo Docker. Reusing sudo for this update.\n'
    DOCKER_PREFIX=(sudo)
    USED_SUDO_DOCKER=1
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

read_env_value() {
  local key="$1" file="$2"
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

detect_target_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER:-}" != "root" ]; then
    printf '%s\n' "$SUDO_USER"
  else
    id -un
  fi
}

detect_host() {
  if [ -n "$HERMESHQ_HOST" ]; then
    printf '%s\n' "$HERMESHQ_HOST"
    return
  fi

  if command -v hostname >/dev/null 2>&1; then
    local ip
    ip="$( (hostname -I 2>/dev/null || true) | awk '{for (i = 1; i <= NF; i++) if ($i !~ /^127\./) {print $i; exit}}' )"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return
    fi
  fi

  hostname -f 2>/dev/null || hostname || printf 'localhost\n'
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

ensure_docker_installed() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi

  case "$(uname -s)" in
    Linux) ;;
    *)
      fail "Docker is not installed. Automatic Docker install is supported only on Linux."
      ;;
  esac

  printf 'Docker not found. Installing Docker Engine and Compose plugin...\n'
  run_root sh -c 'curl -fsSL https://get.docker.com | sh'

  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl enable --now docker
  elif command -v service >/dev/null 2>&1; then
    run_root service docker start
  fi
}

configure_docker_access() {
  local target_user
  target_user="$(detect_target_user)"

  if docker info >/dev/null 2>&1; then
    DOCKER_PREFIX=()
    return
  fi

  if getent group docker >/dev/null 2>&1; then
    if ! id -nG "$target_user" | tr ' ' '\n' | grep -qx docker; then
      printf 'Adding %s to the docker group...\n' "$target_user"
      run_root usermod -aG docker "$target_user"
      DOCKER_GROUP_UPDATED=1
    fi
  fi

  if docker info >/dev/null 2>&1; then
    DOCKER_PREFIX=()
    return
  fi

  if [ "$(id -u)" -eq 0 ]; then
    DOCKER_PREFIX=()
    return
  fi

  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER_PREFIX=(sudo)
    USED_SUDO_DOCKER=1
    return
  fi

  if command -v sudo >/dev/null 2>&1; then
    printf 'Docker is installed but requires elevated access for this run.\n'
    if sudo docker info >/dev/null 2>&1; then
      DOCKER_PREFIX=(sudo)
      USED_SUDO_DOCKER=1
      return
    fi
  fi

  fail "Docker daemon is not reachable"
}

write_env_file() {
  local install_host="$1"
  local jwt_secret db_password admin_password api_base cors_json database_url

  jwt_secret="$(random_hex)"
  db_password="${POSTGRES_PASSWORD:-$(random_password)}"
  admin_password="${ADMIN_PASSWORD:-$(random_password)}"
  api_base="${VITE_API_BASE_URL:-/api}"
  cors_json=$(printf '["http://%s:%s","http://localhost:%s","http://frontend"]' "$install_host" "$FRONTEND_PORT" "$FRONTEND_PORT")
  database_url="postgresql+asyncpg://${POSTGRES_USER}:${db_password}@postgres:5432/${POSTGRES_DB}"

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
}

main() {
  need_cmd curl
  need_cmd tar
  need_cmd python3

  trap on_error ERR
  trap cleanup_tmp_dir EXIT

  ensure_docker_installed
  configure_docker_access
  adopt_existing_docker_context
  compose version >/dev/null 2>&1

  local install_host archive_url src_root existing_env preserve_cloudflared_env
  install_host="$(detect_host)"
  archive_url="$(archive_url_from_repo)"
  TMP_DIR="$(mktemp -d)"
  existing_env=""
  preserve_cloudflared_env=""

  if [ -d "$INSTALL_DIR" ]; then
    INSTALL_BACKUP_DIR="$TMP_DIR/install-backup"
    mv "$INSTALL_DIR" "$INSTALL_BACKUP_DIR"
    RESTORE_BACKUP_ON_FAILURE=1
  fi

  if [ -f "$INSTALL_BACKUP_DIR/.env" ]; then
    existing_env="$TMP_DIR/existing.env"
    cp "$INSTALL_BACKUP_DIR/.env" "$existing_env"
  fi
  if [ -f "$INSTALL_BACKUP_DIR/.cloudflared.env" ]; then
    preserve_cloudflared_env="$TMP_DIR/cloudflared.env"
    cp "$INSTALL_BACKUP_DIR/.cloudflared.env" "$preserve_cloudflared_env"
  fi
  if [ -z "$existing_env" ]; then
    FRESH_INSTALL=1
  fi

  printf 'Downloading HermesHQ from %s\n' "$archive_url"
  curl -fsSL "$archive_url" -o "$TMP_DIR/hermeshq.tar.gz"
  mkdir -p "$TMP_DIR/src"
  if tar --version 2>/dev/null | grep -q 'GNU tar'; then
    tar --warning=no-timestamp -xzf "$TMP_DIR/hermeshq.tar.gz" -C "$TMP_DIR/src"
  else
    tar -xzf "$TMP_DIR/hermeshq.tar.gz" -C "$TMP_DIR/src"
  fi
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
  if [ -n "$preserve_cloudflared_env" ]; then
    cp "$preserve_cloudflared_env" "$INSTALL_DIR/.cloudflared.env"
  fi

  FINAL_ADMIN_USERNAME="$(read_env_value ADMIN_USERNAME "$INSTALL_DIR/.env")"
  FINAL_ADMIN_PASSWORD="$(read_env_value ADMIN_PASSWORD "$INSTALL_DIR/.env")"

  cd "$INSTALL_DIR"
  if [ "$SKIP_START" = "1" ]; then
    printf '\nHermesHQ extracted to %s\n' "$INSTALL_DIR"
    printf 'Skipped docker compose startup because SKIP_START=1\n'
    exit 0
  fi

  printf '\nStarting HermesHQ in %s\n' "$INSTALL_DIR"
  STARTUP_ATTEMPTED=1
  compose up --build -d

  RESTORE_BACKUP_ON_FAILURE=0

  printf '\nHermesHQ is starting\n'
  printf '  frontend: http://%s:%s\n' "$install_host" "$FRONTEND_PORT"
  printf '  backend:  http://%s:%s\n' "$install_host" "$BACKEND_PORT"
  if [ -n "$FINAL_ADMIN_USERNAME" ] && [ -n "$FINAL_ADMIN_PASSWORD" ]; then
    printf '\nHermesHQ admin credentials\n'
    printf '  username: %s\n' "$FINAL_ADMIN_USERNAME"
    printf '  password: %s\n' "$FINAL_ADMIN_PASSWORD"
  fi
  if [ "$USED_SUDO_DOCKER" = "1" ] && [ "$DOCKER_GROUP_UPDATED" = "1" ]; then
    printf '\nDocker was installed and %s was added to the docker group.\n' "$(detect_target_user)"
    printf 'Open a new shell or log out and back in before using docker without sudo.\n'
  elif [ "$DOCKER_GROUP_UPDATED" = "1" ]; then
    printf '\n%s was added to the docker group.\n' "$(detect_target_user)"
    printf 'Open a new shell or log out and back in before using docker without sudo.\n'
  fi
}

main "$@"
