#!/usr/bin/env bash
# Test containers for the provider integration tests (spec 080 §10.2, rev. 4.4 §9.2), used locally and in CI.
# Test data only: neutral example.test users with throwaway passwords. No forced or recursive deletes.
# Passwords are generated per run (no user:password pair is committed) and handed to the tests through
# $STATE/credentials.json; the state directory lives in $TMPDIR (worktree-local for agents, ephemeral in CI).
set -euo pipefail
LABEL="org.furchert.homelab.workpackage=mcp-hub-provider-tests"
GREENMAIL_IMAGE="greenmail/standalone:2.1.14@sha256:1ef95a966418cd09b7ea91d504d8c0826bbe7a2f6e679a75c601a831587c1626"
RADICALE_IMAGE="tomsquest/docker-radicale:3.8.1.1@sha256:e6d8c17bc4d75f3fd40e52b20421ba244c17ca08fb01cd7dc90d5d78ba51b58d"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${HUB_PROVIDER_STATE_DIR:-${TMPDIR:-/tmp}/mcp-hub-provider-tests}"
LOGINS=(hub-list hub-get hub-big hub-logs)
CALDAV_LOGIN=hub-cal
NAMES=(mcp-hub-greenmail mcp-hub-radicale)

case "${1:-}" in
  up)
    mkdir -p "$STATE"
    users="" json="{"
    for login in "${LOGINS[@]}"; do
      password="$(openssl rand -hex 12)"
      users="${users:+$users,}${login}:${password}@example.test"
      json="${json}\"${login}\": \"${password}\","
    done
    caldav_password="$(openssl rand -hex 12)"
    json="${json}\"${CALDAV_LOGIN}\": \"${caldav_password}\","
    printf '%s}\n' "${json%,}" > "$STATE/credentials.json"
    # Radicale reads a per-run copy of the test config plus a users file with the generated password.
    mkdir -p "$STATE/radicale"
    cp "$ROOT/tests/fixtures/radicale/config" "$STATE/radicale/config"
    printf '%s:%s\n' "$CALDAV_LOGIN" "$caldav_password" > "$STATE/radicale/users"
    docker run -d --label "$LABEL" --name mcp-hub-greenmail \
      -p 127.0.0.1:3025:3025 -p 127.0.0.1:3993:3993 \
      -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=${users}" \
      "$GREENMAIL_IMAGE" >/dev/null
    docker run -d --label "$LABEL" --name mcp-hub-radicale -p 127.0.0.1:5232:5232 \
      -v "$STATE/radicale:/config:ro" "$RADICALE_IMAGE" >/dev/null
    echo "started: ${NAMES[*]}; credentials in $STATE (readiness is awaited by the tests)"
    ;;
  down)
    for name in "${NAMES[@]}"; do
      docker stop "$name" >/dev/null 2>&1 || true
      docker rm "$name" >/dev/null 2>&1 || true
    done
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
