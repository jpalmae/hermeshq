#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT_DIR="${OUT_DIR:-$ROOT_DIR/backups}"
TIMESTAMP="${TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BUNDLE_NAME="${BUNDLE_NAME:-hermeshq-backup-${TIMESTAMP}.tar.gz}"
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

need_cmd docker
need_cmd tar
need_cmd date

mkdir -p "$OUT_DIR"

printf 'Ensuring HermesHQ services are available for backup\n'
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

printf 'Dumping PostgreSQL database\n'
docker exec "$POSTGRES_CONTAINER_ID" sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$TMP_DIR/postgres.dump"

printf 'Archiving workspaces volume %s\n' "$WORKSPACES_VOLUME"
docker run --rm \
  -v "${WORKSPACES_VOLUME}:/source:ro" \
  -v "${TMP_DIR}:/backup" \
  alpine sh -lc 'cd /source && tar czf /backup/workspaces.tgz .'

if [ -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env" "$TMP_DIR/.env"
fi

if [ -f "$ROOT_DIR/.cloudflared.env" ]; then
  cp "$ROOT_DIR/.cloudflared.env" "$TMP_DIR/.cloudflared.env"
fi

cat >"$TMP_DIR/metadata.txt" <<EOF
created_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
root_dir=$ROOT_DIR
workspaces_volume=$WORKSPACES_VOLUME
postgres_container=$POSTGRES_CONTAINER_ID
backend_container=$BACKEND_CONTAINER_ID
EOF

printf 'Packing backup bundle\n'
tar czf "$OUT_DIR/$BUNDLE_NAME" -C "$TMP_DIR" .

printf '\nBackup complete\n'
printf '  bundle: %s\n' "$OUT_DIR/$BUNDLE_NAME"
printf '  volume: %s\n' "$WORKSPACES_VOLUME"

