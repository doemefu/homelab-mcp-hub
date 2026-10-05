#!/usr/bin/env bash
# Image check: non-root, read-only root FS, 401 challenge, token rules, Host check, clean logs.
set -euo pipefail
IMAGE="${1:?usage: smoke_image.sh <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mcp-hub-wp5a-smoke.XXXXXX")"
NET="mcp-hub-smoke-$$"
# No forced or recursive deletes: containers are stopped then removed; the temp dir stays in $TMPDIR
# (worktree-local .local/tmp when run by an agent, removed with the worktree; ephemeral on CI runners).
cleanup() {
  docker stop "hub-$$" "jwks-$$" >/dev/null 2>&1 || true
  docker rm "hub-$$" "jwks-$$" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  echo "smoke work dir left for inspection: $WORK"
}
trap cleanup EXIT
fail() {
  echo "FAIL: $*" >&2
  echo "clocks: host $(date -u +%FT%TZ) container $(docker exec "hub-$$" date -u +%FT%TZ 2>/dev/null || echo n/a)" >&2
  docker logs "hub-$$" >&2 || true
  exit 1
}

(cd "$ROOT" && uv run python scripts/dev_token.py init --dir "$WORK")
docker network create --label org.furchert.homelab.workpackage=mcp-hub-wp5a "$NET" >/dev/null
docker run -d --label org.furchert.homelab.workpackage=mcp-hub-wp5a --name "jwks-$$" --network "$NET" \
  --read-only --user 10001:10001 \
  -v "$WORK/jwks:/srv:ro" "$IMAGE" python -m http.server 8099 --directory /srv >/dev/null
docker run -d --label org.furchert.homelab.workpackage=mcp-hub-wp5a --name "hub-$$" --network "$NET" \
  --read-only --tmpfs /tmp:size=16m --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:18083:8083 -p 127.0.0.1:18084:8084 \
  -v "$WORK/secrets:/etc/mcp-hub/secrets:ro" \
  -e AUTH_JWKS_URL="http://jwks-$$:8099/jwks.json" "$IMAGE" >/dev/null

# --retry-all-errors: while the process starts, the published port accepts and then closes (curl 52/56).
curl -fsS --retry 30 --retry-all-errors --retry-delay 1 http://127.0.0.1:18084/readyz >/dev/null || fail "readyz"
[ "$(docker exec "hub-$$" id -u)" = "10001" ] || fail "uid"

mcp() { # $1 token or "", $2 extra header or ""
  local args=(-s -o "$WORK/body" -D "$WORK/headers" -w '%{http_code}' -X POST http://127.0.0.1:18083/mcp
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream'
    -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/list')
  [ -n "$1" ] && args+=(-H "Authorization: Bearer $1")
  if [ -n "${2:-}" ]; then args+=(-H "$2"); else args+=(-H 'Host: mcp.furchert.ch'); fi
  curl "${args[@]}" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"smoke","version":"0"}}}}'
}
token() { (cd "$ROOT" && uv run python scripts/dev_token.py mint --dir "$WORK" --case "$1"); }

[ "$(mcp "")" = "401" ] || fail "no token"
grep -qi 'resource_metadata="https://mcp.furchert.ch/.well-known/oauth-protected-resource/mcp", scope="mail:read calendar:read"' "$WORK/headers" || fail "challenge"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: mcp.furchert.ch' http://127.0.0.1:18083/.well-known/oauth-protected-resource/mcp)" = "200" ] || fail "metadata"
VALID="$(token valid)"
[ "$(mcp "$VALID")" = "200" ] || fail "valid token"
grep -q '"list_accounts"' "$WORK/body" || fail "tool list"
[ "$(mcp "$(token typ-jwt)")" = "401" ] || fail "typ JWT"
[ "$(mcp "$(token wrong-aud)")" = "401" ] || fail "audience"
[ "$(mcp "$(token expired)")" = "401" ] || fail "expired"
[ "$(mcp "$(token mail-only)")" = "403" ] || fail "insufficient scope"
grep -qi 'scope="mail:read calendar:read"' "$WORK/headers" || fail "403 scope param"
[ "$(mcp "$VALID" 'Host: evil.example.org')" = "421" ] || fail "host"
# Large garbage bearer below the HTTP server's header limit -> real verifier -> 401 with the challenge.
[ "$(mcp "$(printf 'a%.0s' $(seq 1 8192))")" = "401" ] || fail "8 KiB bearer"
grep -qi 'scope="mail:read calendar:read"' "$WORK/headers" || fail "8 KiB bearer challenge"
# Above ~16 KiB of headers the outcome depends on TCP segmentation (spec 080 §10.3, D53): the HTTP server answers
# 400 when the header block arrives in several reads, otherwise the app answers 401 with the challenge.
# Either is fail-closed; anything else (2xx, 5xx, connection reset) fails the check.
BIG="$(mcp "$(printf 'a%.0s' $(seq 1 32768))" || true)"
case "$BIG" in
  400) ;;
  401) grep -qi 'scope="mail:read calendar:read"' "$WORK/headers" || fail "32 KiB bearer 401 without challenge" ;;
  *) fail "32 KiB bearer got '$BIG' (expected 400 or 401 with challenge)" ;;
esac
LOGS="$(docker logs "hub-$$" 2>&1)"
! grep -q "$VALID" <<<"$LOGS" || fail "token in logs"
! grep -q 'HTTP Request' <<<"$LOGS" || fail "httpx2 request line in logs"
! grep -q 'evil.example.org' <<<"$LOGS" || fail "header value in logs"
# Owner-command wrapper (spec 080 §7.3): present, runs as uid 10001 on a read-only root FS, prints usage and exits 2.
rc=0
USAGE_OUT="$(docker run --rm --label org.furchert.homelab.workpackage=mcp-hub-wp5a --read-only --user 10001:10001 \
  --network none "$IMAGE" mcp-hub login 2>&1)" || rc=$?  # no arguments would start the server
[ "$rc" = "2" ] || fail "mcp-hub wrapper exit code $rc (expected 2)"
grep -q 'usage: mcp-hub login <account-id>' <<<"$USAGE_OUT" || fail "mcp-hub wrapper usage text"
# HTML converter on the image's own interpreter: linear growth on malformed input, unterminated comments hidden.
docker run --rm -i --label org.furchert.homelab.workpackage=mcp-hub-wp5a --read-only --user 10001:10001 \
  --network none "$IMAGE" python - <"$ROOT/scripts/html_scaling_check.py" || fail "html scaling check"
echo "smoke OK"
