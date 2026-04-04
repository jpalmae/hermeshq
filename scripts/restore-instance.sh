#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BUNDLE_PATH="${1:-}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
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

restart_cloudflared() {
  if [ ! -f "$ROOT_DIR/.cloudflared.env" ]; then
    return
  fi

  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.cloudflared.env"
  set +a

  if [ -z "${TUNNEL_TOKEN:-}" ]; then
    printf 'Skipping cloudflared restart: TUNNEL_TOKEN missing\n'
    return
  fi

  local backend_container network
  backend_container="$(compose ps -q backend)"
  [ -n "$backend_container" ] || return
  network="$(
    docker inspect "$backend_container" \
      --format '{{range $name, $_ := .NetworkSettings.Networks}}{{printf "%s\n" $name}}{{end}}' | head -n 1
  )"
  [ -n "$network" ] || return

  docker rm -f hermeshq-cloudflared >/dev/null 2>&1 || true
  docker run -d \
    --name hermeshq-cloudflared \
    --restart unless-stopped \
    --network "$network" \
    cloudflare/cloudflared:latest \
    tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" >/dev/null
}

need_cmd docker
need_cmd tar

[ -n "$BUNDLE_PATH" ] || fail "Usage: scripts/restore-instance.sh /path/to/hermeshq-backup.tar.gz"
[ -f "$BUNDLE_PATH" ] || fail "Backup bundle not found: $BUNDLE_PATH"

printf 'Extracting backup bundle\n'
tar xzf "$BUNDLE_PATH" -C "$TMP_DIR"

if [ -f "$TMP_DIR/.env" ]; then
  cp "$TMP_DIR/.env" "$ROOT_DIR/.env"
fi

if [ -f "$TMP_DIR/.cloudflared.env" ]; then
  cp "$TMP_DIR/.cloudflared.env" "$ROOT_DIR/.cloudflared.env"
fi

printf 'Starting HermesHQ base services\n'
compose up -d postgres backend >/dev/null

POSTGRES_CONTAINER_ID="$(compose ps -q postgres)"
BACKEND_CONTAINER_ID="$(compose ps -q backend)"
[ -n "$POSTGRES_CONTAINER_ID" ] || fail "Postgres container is not running"
[ -n "$BACKEND_CONTAINER_ID" ] || fail "Backend container is not running"

printf 'Waiting for PostgreSQL to become healthy\n'
for _ in $(seq 1 60); do
  STATUS="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$POSTGRES_CONTAINER_ID" 2>/dev/null || true)"
  if [ "$STATUS" = "healthy" ]; then
    break
  fi
  sleep 2
done
[ "$STATUS" = "healthy" ] || fail "PostgreSQL did not become healthy"

WORKSPACES_VOLUME="$(
  docker inspect "$BACKEND_CONTAINER_ID" \
    --format '{{range .Mounts}}{{if eq .Destination "/app/workspaces"}}{{.Name}}{{end}}{{end}}'
)"
[ -n "$WORKSPACES_VOLUME" ] || fail "Could not determine workspaces Docker volume"

printf 'Restoring workspaces volume %s\n' "$WORKSPACES_VOLUME"
docker run --rm \
  -v "${WORKSPACES_VOLUME}:/target" \
  -v "${TMP_DIR}:/backup" \
  alpine sh -lc 'rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null || true; tar xzf /backup/workspaces.tgz -C /target'

printf 'Restoring PostgreSQL database\n'
cat "$TMP_DIR/postgres.dump" | docker exec -i "$POSTGRES_CONTAINER_ID" sh -lc 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists'

printf 'Restarting HermesHQ services with restored state\n'
compose up --build -d >/dev/null

restart_cloudflared

printf '\nRestore complete\n'
printf '  bundle: %s\n' "$BUNDLE_PATH"
printf '  volume: %s\n' "$WORKSPACES_VOLUME"

