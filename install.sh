#!/usr/bin/env bash
set -Eeuo pipefail

# Install script revision: 2026-05-27

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
PLANNED_AGENTS="${PLANNED_AGENTS:-}"
SKIP_SIZING="${SKIP_SIZING:-0}"

# ── Interactive input helper ────────────────────────────────────────────
# When running via `curl ... | bash`, stdin is the pipe — not a terminal.
# All `read` calls must read from /dev/tty instead, otherwise they consume
# pipe data or hit EOF (which triggers `set -e` → crash).

IS_INTERACTIVE=1
if [ ! -t 0 ] || [ ! -e /dev/tty ]; then
  IS_INTERACTIVE=0
fi

read_tty() {
  # Reads a single line from /dev/tty (the controlling terminal).
  # Falls back to reading from stdin only when /dev/tty is unavailable
  # AND stdin looks like a terminal (e.g. `bash install.sh` directly).
  # Returns the input in the variable named by $1.
  local _var="${1:?variable name required}"
  if [ -e /dev/tty ]; then
    IFS= read -r "$_var" </dev/tty || true
  elif [ -t 0 ]; then
    IFS= read -r "$_var" || true
  else
    # Fully non-interactive (no tty, no terminal stdin).
    # Return empty string — callers must handle this.
    eval "$_var="
  fi
}

# ── Phase 3: System-resource detection and sizing ──────────────────────

SYS_TOTAL_RAM_MB=0
SYS_AVAILABLE_RAM_MB=0
SYS_CPU_CORES=0
SYS_AVAILABLE_DISK_GB=0

SIZING_SEMAPHORE=0
SIZING_RAM_BACKEND=0
SIZING_RAM_POSTGRES=0
SIZING_RAM_TOTAL=0
SIZING_CPU=0
SIZING_DISK=0
SIZING_CONCURRENT=0

PG_SHARED_BUFFERS=""
PG_EFFECTIVE_CACHE=""
PG_MAX_CONNECTIONS=0

detect_system_resources() {
  local os_name
  os_name="$(uname -s)"

  if [ "$os_name" = "Darwin" ]; then
    SYS_TOTAL_RAM_MB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
    SYS_AVAILABLE_RAM_MB=$(( SYS_TOTAL_RAM_MB * 70 / 100 ))
    SYS_CPU_CORES="$(sysctl -n hw.ncpu)"
  else
    if command -v free >/dev/null 2>&1; then
      SYS_TOTAL_RAM_MB="$(free -m | awk '/^Mem:/ {print $2}')"
      SYS_AVAILABLE_RAM_MB="$(free -m | awk '/^Mem:/ {print $7}')"
    else
      SYS_TOTAL_RAM_MB=4096
      SYS_AVAILABLE_RAM_MB=2048
    fi
    SYS_CPU_CORES="$(nproc 2>/dev/null || printf '2')"
  fi

  local disk_path
  disk_path="$(dirname "$INSTALL_DIR")"
  if [ "$os_name" = "Darwin" ]; then
    SYS_AVAILABLE_DISK_GB="$(df -g "$disk_path" 2>/dev/null | awk 'NR==2{printf "%d", $4}' || printf '50')"
  else
    SYS_AVAILABLE_DISK_GB="$(df -BG "$disk_path" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}' || printf '50')"
  fi
}

calculate_sizing() {
  local total_agents="${1:?total_agents required}"
  local concurrent semaphore ram_backend ram_postgres ram_total cpu_needed disk_needed
  local max_by_ram

  concurrent=$(( total_agents / 2 ))
  if [ "$concurrent" -lt 1 ]; then concurrent=1; fi
  semaphore=$concurrent
  ram_backend=$(( semaphore * 50 + 500 ))
  ram_postgres=$(( semaphore * 10 + 200 ))
  ram_total=$(( ram_backend + ram_postgres + 256 ))
  cpu_needed=$(( semaphore / 6 + 1 ))
  disk_needed=$(( total_agents * 1500 / 1000 + 5 ))

  # Clamp semaphore to available RAM
  # ram_total = semaphore*60 + 956 (500 min backend + 200 min postgres + 256 frontend)
  if [ "$SYS_AVAILABLE_RAM_MB" -gt 956 ]; then
    max_by_ram=$(( (SYS_AVAILABLE_RAM_MB - 956) / 60 ))
    if [ "$max_by_ram" -lt "$semaphore" ]; then
      semaphore=$max_by_ram
      concurrent=$semaphore
      ram_backend=$(( semaphore * 50 + 500 ))
      ram_postgres=$(( semaphore * 10 + 200 ))
      ram_total=$(( ram_backend + ram_postgres + 256 ))
      cpu_needed=$(( semaphore / 6 + 1 ))
      disk_needed=$(( semaphore * 2 * 1500 / 1000 + 5 ))
    fi
  fi

  SIZING_CONCURRENT=$concurrent
  SIZING_SEMAPHORE=$semaphore
  SIZING_RAM_BACKEND=$ram_backend
  SIZING_RAM_POSTGRES=$ram_postgres
  SIZING_RAM_TOTAL=$ram_total
  SIZING_CPU=$cpu_needed
  SIZING_DISK=$disk_needed
}

validate_resources() {
  local ok=1
  if [ "$SIZING_RAM_TOTAL" -gt "$SYS_AVAILABLE_RAM_MB" ]; then
    ok=0
  fi
  if [ "$SIZING_CPU" -gt "$SYS_CPU_CORES" ]; then
    ok=0
  fi
  if [ "$SIZING_DISK" -gt "$SYS_AVAILABLE_DISK_GB" ]; then
    ok=0
  fi
  return $(( 1 - ok ))
}

print_resource_table() {
  local needed_ram="$1" available_ram="$2"
  local needed_cpu="$3" available_cpu="$4"
  local needed_disk="$5" available_disk="$6"
  local semaphore="$7"

  local ram_icon cpu_icon disk_icon
  if [ "$needed_ram" -le "$available_ram" ]; then ram_icon="✅"; else ram_icon="❌"; fi
  if [ "$needed_cpu" -le "$available_cpu" ]; then cpu_icon="✅"; else cpu_icon="❌"; fi
  if [ "$needed_disk" -le "$available_disk" ]; then disk_icon="✅"; else disk_icon="❌"; fi

  local needed_ram_gb available_ram_gb needed_disk_gb available_disk_gb
  needed_ram_gb="$(( needed_ram / 1024 )) GB"
  available_ram_gb="$(( available_ram / 1024 )) GB"
  needed_disk_gb="${needed_disk} GB"
  available_disk_gb="${available_disk} GB"

  printf '\n'
  printf '    ┌──────────────┬─────────────┬──────────────┐\n'
  printf '    │ %-12s │ %-11s │ %-12s │\n' "Resource" "Needed" "Available"
  printf '    ├──────────────┼─────────────┼──────────────┤\n'
  printf '    │ %-12s │ %-11s │ %-8s %s │\n' "RAM" "$needed_ram_gb" "$available_ram_gb" "$ram_icon"
  printf '    │ %-12s │ %-11s │ %-8s %s │\n' "CPU" "${needed_cpu} cores" "${available_cpu} cores" "$cpu_icon"
  printf '    │ %-12s │ %-11s │ %-8s %s │\n' "Disk" "$needed_disk_gb" "$available_disk_gb" "$disk_icon"
  printf '    │ %-12s │ %-11s │ %-12s │\n' "Semaphore" "$semaphore" ""
  printf '    └──────────────┴─────────────┴──────────────┘\n'
  printf '\n'
}

calculate_max_agents() {
  local max_agents=200
  local max_semaphore max_by_cpu max_by_disk agents_from_ram agents_from_cpu agents_from_disk

  if [ "$SYS_AVAILABLE_RAM_MB" -gt 956 ]; then
    max_semaphore=$(( (SYS_AVAILABLE_RAM_MB - 956) / 60 ))
    agents_from_ram=$(( max_semaphore * 2 ))
  else
    agents_from_ram=0
  fi

  max_by_cpu=$(( SYS_CPU_CORES * 6 ))
  agents_from_cpu=$(( max_by_cpu * 2 ))

  agents_from_disk=$(( (SYS_AVAILABLE_DISK_GB - 5) * 1000 / 1500 ))

  # Take minimum of all constraints
  max_agents=$agents_from_ram
  if [ "$agents_from_cpu" -lt "$max_agents" ]; then
    max_agents=$agents_from_cpu
  fi
  if [ "$agents_from_disk" -lt "$max_agents" ]; then
    max_agents=$agents_from_disk
  fi

  if [ "$max_agents" -lt 1 ]; then
    max_agents=1
  fi
  if [ "$max_agents" -gt 200 ]; then
    max_agents=200
  fi

  printf '%d' "$max_agents"
}

calculate_postgres_tuning() {
  local pg_ram_mb="${1:?pg_ram_mb required}"

  PG_SHARED_BUFFERS="$(( pg_ram_mb / 4 ))MB"
  PG_EFFECTIVE_CACHE="$(( pg_ram_mb * 3 / 4 ))MB"
  PG_MAX_CONNECTIONS=$(( SIZING_SEMAPHORE * 2 ))
  if [ "$PG_MAX_CONNECTIONS" -lt 50 ]; then
    PG_MAX_CONNECTIONS=50
  fi
}

generate_docker_override() {
  local semaphore="${1:?semaphore required}"
  local ram_backend ram_postgres backend_mem_limit cpu_limit

  ram_backend=$(( semaphore * 50 + 500 ))
  ram_postgres=$(( semaphore * 10 + 200 ))
  backend_mem_limit="${ram_backend}M"
  cpu_limit="$(( semaphore / 6 + 1 ))"

  cat >"$INSTALL_DIR/docker-compose.override.yml" <<OVERRIDE
services:
  postgres:
    command: >
      postgres
      -c shared_buffers=${PG_SHARED_BUFFERS}
      -c effective_cache_size=${PG_EFFECTIVE_CACHE}
      -c max_connections=${PG_MAX_CONNECTIONS}
    deploy:
      resources:
        limits:
          memory: ${ram_postgres}M
          cpus: "1"
  backend:
    deploy:
      resources:
        limits:
          memory: ${backend_mem_limit}
          cpus: "${cpu_limit}"
  frontend:
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"
OVERRIDE

  printf 'Generated docker-compose.override.yml with resource limits\n'
}

update_env_semaphore() {
  local semaphore="${1:?semaphore required}"
  local env_file="$INSTALL_DIR/.env"

  if [ ! -f "$env_file" ]; then
    printf 'CONCURRENCY_SEMAPHORE=%s\n' "$semaphore" >> "$env_file"
    return
  fi

  if grep -q '^CONCURRENCY_SEMAPHORE=' "$env_file" 2>/dev/null; then
    sed -i.bak "s/^CONCURRENCY_SEMAPHORE=.*/CONCURRENCY_SEMAPHORE=${semaphore}/" "$env_file"
    rm -f "$env_file.bak"
  else
    printf 'CONCURRENCY_SEMAPHORE=%s\n' "$semaphore" >> "$env_file"
  fi
}

prompt_agent_count() {
  local count=""
  # When non-interactive and PLANNED_AGENTS is set, use it without prompting
  if [ "$IS_INTERACTIVE" = "0" ] && [ -n "${PLANNED_AGENTS:-}" ]; then
    printf '%d' "$PLANNED_AGENTS"
    return
  fi
  if [ "$IS_INTERACTIVE" = "0" ]; then
    # No tty and no env var — default to 4 agents
    printf '4'
    return
  fi
  while true; do
    printf 'How many agents do you plan to deploy? (1-200): ' >&2
    read_tty count
    if printf '%s' "$count" | grep -qE '^[0-9]+$' && [ "$count" -ge 1 ] && [ "$count" -le 200 ]; then
      printf '%d' "$count"
      return
    fi
    printf '  ⚠️  Please enter a number between 1 and 200.\n' >&2
  done
}

do_sizing_flow() {
  detect_system_resources

  printf '\n  Detected system resources:\n'
  printf '    RAM:   %d MB total, %d MB available\n' "$SYS_TOTAL_RAM_MB" "$SYS_AVAILABLE_RAM_MB"
  printf '    CPU:   %d cores\n' "$SYS_CPU_CORES"
  printf '    Disk:  %d GB available\n\n' "$SYS_AVAILABLE_DISK_GB"

  if [ -z "$PLANNED_AGENTS" ]; then
    PLANNED_AGENTS="$(prompt_agent_count)"
  fi

  calculate_sizing "$PLANNED_AGENTS"
  print_resource_table "$SIZING_RAM_TOTAL" "$SYS_AVAILABLE_RAM_MB" \
    "$SIZING_CPU" "$SYS_CPU_CORES" "$SIZING_DISK" "$SYS_AVAILABLE_DISK_GB" \
    "$SIZING_SEMAPHORE"

  if ! validate_resources; then
    local max_agents
    max_agents="$(calculate_max_agents)"
    printf '  ⚠️  Insufficient resources for %d agents.\n' "$PLANNED_AGENTS"
    printf '  Maximum agents supported by this system: %d\n\n' "$max_agents"
    printf '  Options:\n'
    printf '    1) Reduce agent count to %d (recommended)\n' "$max_agents"
    printf '    2) Continue with %d agents anyway (may cause instability)\n' "$PLANNED_AGENTS"
    printf '    3) Cancel installation\n'
    printf '\n  Choose [1/2/3]: '

    local choice=""
    if [ "$IS_INTERACTIVE" = "0" ]; then
      # Non-interactive: auto-pick option 1 (reduce to max supported)
      choice="1"
      printf '1 (auto-selected)\n'
    else
      read_tty choice
    fi
    case "$choice" in
      1)
        PLANNED_AGENTS=$max_agents
        calculate_sizing "$PLANNED_AGENTS"
        printf '  ✅ Resized to %d agents (semaphore=%d)\n' "$PLANNED_AGENTS" "$SIZING_SEMAPHORE"
        ;;
      2)
        printf '  ⚠️  Continuing with %d agents — monitor for resource issues.\n' "$PLANNED_AGENTS"
        ;;
      *)
        printf '  Installation cancelled.\n'
        exit 0
        ;;
    esac
  else
    printf '  ✅ System resources sufficient for %d agents.\n' "$PLANNED_AGENTS"
  fi

  calculate_postgres_tuning "$SIZING_RAM_POSTGRES"
  update_env_semaphore "$SIZING_SEMAPHORE"
  generate_docker_override "$SIZING_SEMAPHORE"

  printf '  ✅ Sizing complete: semaphore=%d, ram_total=%dMB, cpu=%d, disk=%dGB\n' \
    "$SIZING_SEMAPHORE" "$SIZING_RAM_TOTAL" "$SIZING_CPU" "$SIZING_DISK"
}

do_update_sizing() {
  detect_system_resources

  local current_semaphore
  current_semaphore="$(grep '^CONCURRENCY_SEMAPHORE=' "$INSTALL_DIR/.env" 2>/dev/null | sed 's/^CONCURRENCY_SEMAPHORE=//' || printf '8')"

  printf '\n  Current configuration:\n'
  printf '    Semaphore: %s\n' "$current_semaphore"
  printf '    Estimated agents: %d\n' "$(( current_semaphore * 2 ))"
  printf '\n  Detected system resources:\n'
  printf '    RAM:   %d MB available\n' "$SYS_AVAILABLE_RAM_MB"
  printf '    CPU:   %d cores\n' "$SYS_CPU_CORES"
  printf '    Disk:  %d GB available\n\n' "$SYS_AVAILABLE_DISK_GB"

  local max_agents
  max_agents="$(calculate_max_agents)"

  printf '  Maximum agents this system supports: %d\n\n' "$max_agents"
  printf '  Options:\n'
  printf '    1) Keep current configuration (semaphore=%s)\n' "$current_semaphore"
  printf '    2) Resize to recommended (%d agents)\n' "$max_agents"
  printf '    3) Resize to specific agent count\n'
  printf '    4) Skip sizing\n'
  printf '\n  Choose [1/2/3/4]: '

  local choice=""
  if [ "$IS_INTERACTIVE" = "0" ]; then choice="1"; printf "1 (auto-selected)\n"; else read_tty choice; fi
  case "$choice" in
    2)
      PLANNED_AGENTS=$max_agents
      calculate_sizing "$PLANNED_AGENTS"
      calculate_postgres_tuning "$SIZING_RAM_POSTGRES"
      update_env_semaphore "$SIZING_SEMAPHORE"
      generate_docker_override "$SIZING_SEMAPHORE"
      printf '  ✅ Resized: semaphore=%d for %d agents\n' "$SIZING_SEMAPHORE" "$PLANNED_AGENTS"
      ;;
    3)
      PLANNED_AGENTS="$(prompt_agent_count)"
      calculate_sizing "$PLANNED_AGENTS"
      print_resource_table "$SIZING_RAM_TOTAL" "$SYS_AVAILABLE_RAM_MB" \
        "$SIZING_CPU" "$SYS_CPU_CORES" "$SIZING_DISK" "$SYS_AVAILABLE_DISK_GB" \
        "$SIZING_SEMAPHORE"
      calculate_postgres_tuning "$SIZING_RAM_POSTGRES"
      update_env_semaphore "$SIZING_SEMAPHORE"
      generate_docker_override "$SIZING_SEMAPHORE"
      printf '  ✅ Resized: semaphore=%d for %d agents\n' "$SIZING_SEMAPHORE" "$PLANNED_AGENTS"
      ;;
    *)
      # Ensure CONCURRENCY_SEMAPHORE exists in .env even if keeping current config
      if ! grep -q '^CONCURRENCY_SEMAPHORE=' "$INSTALL_DIR/.env" 2>/dev/null; then
        printf 'CONCURRENCY_SEMAPHORE=8\n' >> "$INSTALL_DIR/.env"
        printf '  Added CONCURRENCY_SEMAPHORE=8 to .env\n'
        current_semaphore=8
      fi
      # Always generate override so docker-compose.override.yml exists for compose up
      calculate_sizing "$(( current_semaphore * 2 ))"
      calculate_postgres_tuning "$SIZING_RAM_POSTGRES"
      generate_docker_override "$current_semaphore"
      printf '  Keeping current configuration.\n'
      ;;
  esac
}

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
    # Re-seed CWD in case it was pointing inside the moved directory
    cd "$INSTALL_DIR" 2>/dev/null || cd /tmp || true
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
# Avoid characters that break URI parsing (@, :, /, #, ?) and shell metacharacters
safe_special = "!%-_.~"
special = secrets.choice(safe_special)
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
  # URL-encode the password to handle special characters safely
  local encoded_password
  encoded_password="$(printf '%s' "$db_password" | python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))")"
  database_url="postgresql+asyncpg://${POSTGRES_USER}:${encoded_password}@postgres:5432/${POSTGRES_DB}"
  local fernet_key
  fernet_key="${FERNET_KEY:-$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")}"

  cat >"$INSTALL_DIR/.env" <<EOF
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${db_password}
POSTGRES_PORT=${POSTGRES_PORT}
DATABASE_URL=${database_url}
JWT_SECRET=${jwt_secret}
FERNET_KEY=${fernet_key}
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

  # Preserve CWD in case it is inside INSTALL_DIR (e.g. user ran ./install.sh from ~/hermeshq)
  local _saved_cwd
  _saved_cwd="$(pwd 2>/dev/null || printf '/tmp')" || true

  if [ -d "$INSTALL_DIR" ]; then
    INSTALL_BACKUP_DIR="$TMP_DIR/install-backup"
    mv "$INSTALL_DIR" "$INSTALL_BACKUP_DIR"
    RESTORE_BACKUP_ON_FAILURE=1
    # Restore CWD if it was invalidated by the move
    cd "$_saved_cwd" 2>/dev/null || cd /tmp || true
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

  # ── Phase 3: Sizing ─────────────────────────────────────────────────
  if [ "$SKIP_SIZING" != "1" ]; then
    if [ "$FRESH_INSTALL" = "1" ]; then
      do_sizing_flow
    else
      do_update_sizing
    fi
  elif [ "$FRESH_INSTALL" = "1" ]; then
    # Ensure a default semaphore exists for non-interactive installs
    if ! grep -q '^CONCURRENCY_SEMAPHORE=' "$INSTALL_DIR/.env" 2>/dev/null; then
      printf 'CONCURRENCY_SEMAPHORE=8\n' >> "$INSTALL_DIR/.env"
    fi
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
