#!/bin/sh
set -eu

allow_file=/tmp/allowed_domains
: > "$allow_file"

printf '%s\n' "${RUNTIME_EGRESS_ALLOWLIST:-}" | tr ',' '\n' | while IFS= read -r domain; do
    normalized=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
    [ -n "$normalized" ] || continue
    printf '%s\n' "$normalized" | grep -Eq '^(\.?[a-z0-9]|\.?[a-z0-9][a-z0-9.-]*[a-z0-9])$' || {
        printf 'Invalid egress allowlist domain\n' >&2
        exit 1
    }
    printf '%s\n' "$normalized" >> "$allow_file"
done

[ -s "$allow_file" ] || {
    printf 'RUNTIME_EGRESS_ALLOWLIST must not be empty\n' >&2
    exit 1
}

exec squid -N -f /etc/squid/squid.conf
