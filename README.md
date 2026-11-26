# homelab-mcp-hub

A self-hosted, **read-only** remote [MCP](https://modelcontextprotocol.io) server (Streamable HTTP) that exposes mail and calendars across multiple accounts to AI clients such as Claude.

The hub is a pure OAuth 2.1 resource server: your authorization server issues RS256 access tokens, the hub validates them offline against its JWKS endpoint, and serves strictly read-only mail and calendar tools with prompt-injection containment and zero-PII logging.

> **Homelab origin:** This repository is actively deployed in the `doemefu/homelab` cluster via Flux, so `k8s/deployment.yaml` and the built-in config defaults target that homelab. Every deployment setting (domain, OAuth issuer/JWKS, client ID, timezone, accounts, database) can be overridden through environment variables and mounted files — no code changes required for standard self-hosting.

## Security & design highlights

- **Strictly read-only:** Every tool declares `readOnlyHint: true`, `destructiveHint: false`. IMAP traffic passes through an allowlist guard ([`src/mcp_hub/providers/imap_guard.py`](src/mcp_hub/providers/imap_guard.py)) that only permits `CAPABILITY`, `LOGIN`, `LIST`, `EXAMINE` (never `SELECT`), `UID SEARCH`, `UID FETCH` (`BODY.PEEK`), `NOOP`, and `LOGOUT`. Microsoft Graph only sends `GET` requests and rejects write scopes in the registry. CalDAV only sends `PROPFIND` and `REPORT`.
- **Prompt-injection containment:** Every third-party string (subjects, sender/recipient names, addresses, folder names, bodies, calendar fields) is sanitised, length-capped, and placed inside `untrusted` objects. Hub-generated metadata (`ref`, `id`, timestamps, enums) stays outside.
- **Offline OAuth 2.1 + kill switch:** Validates RS256 JWTs against a throttled JWKS cache (`typ: at+jwt` / `application/at+jwt`, `iss`, `aud`, `client_id`, `exp`/`iat`/`nbf`, required scopes `mail:read` + `calendar:read`) and checks `sub` against a mounted `allowed-subjects` file re-read every 60 s (empty or missing = reject all tokens).
- **No content storage & zero-PII logs:** Mail and calendar content is never stored on disk or in a database. PostgreSQL is only used if you enable Microsoft Graph accounts, storing AES-256-GCM encrypted refresh tokens. Structured JSON logs never contain tokens, headers, addresses, provider URLs, queries, or message content.
- **Hardened runtime:** Non-root container (`uid/gid 10001`), read-only root filesystem, all Linux capabilities dropped, strict per-call deadlines, response byte caps, and bounded RFC 5545 recurrence expansion.

## Supported providers & tools

| Capability | Protocol | Tested / supported providers | Notes |
|------------|----------|------------------------------|-------|
| Mail | `imap` | iCloud Mail, Gmail, standard IMAP-over-TLS servers | Uses `EXAMINE` + `BODY.PEEK`; detects Gmail `X-GM-EXT-1` automatically |
| Mail | `graph` | Outlook.com (`consumers`), Microsoft 365 (`organizations` / tenant ID) | Requires PostgreSQL token store + one-time `mcp-hub login <account-id>` |
| Calendar | `caldav` | iCloud Calendar, Radicale, standard CalDAV servers | Bounded RFC 5545 recurrence expansion (`RRULE`, `RDATE`, `EXDATE`, overrides) |

*(The `accounts.json` schema also reserves `ics` feeds and `graph` calendars for future work; only `imap`, `graph` mail, and `caldav` adapters are implemented today.)*

### MCP tools (all read-only)

| Tool | Purpose |
|------|---------|
| `list_accounts` | Lists configured accounts and per-capability health status (no upstream provider call) |
| `list_unread` | Unread inbox messages across all mail accounts or one account, with signed cursor paging |
| `list_messages` | Messages in `inbox`, `sent`, `archive`, `trash`, or `all`, with unread/direction filters and cursor paging |
| `get_message` | Full sanitised message body, headers, and attachment list by `id` or stable `ref` |
| `get_message_status` | Batch re-check (1–50 `ref`/`id` entries) for folder location, flags, reply state, and newer thread activity |
| `get_thread` | Full conversation by `ref`, `id`, or `thread_key`, oldest-first with quoted history removed |
| `search_messages` | Word, sender, recipient, and subject search (up to 90 days) across accounts/folders with cursor paging |
| `get_events` | Calendar events in a time window with time-zone conversion and bounded recurrence expansion |

Full tool schemas, error codes, limits, and token validation rules are documented in [INTERFACES.md](INTERFACES.md).

## Using this repo for your own setup (with an AI agent)

This repository is structured so you and your coding agent (Claude Code, Codex, Cursor, etc.) can adapt and deploy it to your own infrastructure without guessing internal contracts.

### Recommended reading order for your agent

1. `README.md` (this file) — architecture, prerequisites, and deployment shape
2. `INTERFACES.md` — exact OAuth/JWT contract (§1), environment variables & `accounts.json` schema (§5), logging (§6), token store & CLI commands (§7)
3. `tests/fixtures/accounts.example.json` — valid example account registry
4. `src/mcp_hub/config.py` and `src/mcp_hub/registry.py` — authoritative env defaults and Pydantic validation rules
5. `DEPLOYMENT.md` — Kubernetes manifests, `check-registry`, Graph login, metrics, and troubleshooting

### What you configure vs. what needs a code edit

- **No code changes needed:** Your public hostname (`HUB_PUBLIC_HOST`, `HUB_RESOURCE`), OAuth issuer and JWKS URL (`AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_EXPECTED_CLIENT_ID`), timezone, PostgreSQL settings, and mail/calendar accounts all live in environment variables and `HUB_SECRETS_DIR`.
- **When you would fork & edit code:**
  - **Browser `Origin` allowlist:** `ALLOWED_ORIGINS` in [`src/mcp_hub/app.py`](src/mcp_hub/app.py) allows `https://claude.ai` and `https://claude.com` (requests without an `Origin` header, such as non-browser MCP clients, are unaffected). Add your client's origin if you connect from a different browser app.
  - **Custom OAuth scopes:** `REQUIRED_SCOPES` in [`src/mcp_hub/app.py`](src/mcp_hub/app.py) requires both `mail:read` and `calendar:read` on every request.
  - **Additional provider labels:** `Provider` in [`src/mcp_hub/registry.py`](src/mcp_hub/registry.py) validates the `provider` metadata enum (`icloud`, `google`, `microsoft`, `microsoft-org`, `ics`). Generic IMAP/CalDAV servers work using their configured `host`/`url` under an existing enum value, or by adding a label to `Provider`.

### Keep human-only steps out of chat & git

Never paste real credentials into chat or commit them to a fork:
- Generating app-specific passwords (iCloud / Gmail) and writing secret files in `HUB_SECRETS_DIR`
- Registering your own Microsoft Entra ID application (for `graph` mail) and running `mcp-hub login <account-id>` in an interactive terminal
- Creating the OAuth client in your IdP and writing your JWT `sub` into `allowed-subjects`

### Starter prompt for your AI agent

Clone this repository and give your agent a prompt like this:

> Help me self-host `homelab-mcp-hub` on my own infrastructure (`<Docker Compose / Kubernetes / Fly / etc.>`).
> First read `README.md`, `INTERFACES.md` (§1, §5, §7), `tests/fixtures/accounts.example.json`, `src/mcp_hub/config.py`, and `src/mcp_hub/registry.py`.
> Ask me which deployment target, domain, OAuth 2.1 provider (must issue RS256 `at+jwt` tokens with JWKS), and mail/calendar accounts I want to connect.
> Then generate my deployment config, environment variables, and a placeholder `accounts.json` + secrets directory checklist (never ask me to paste real passwords or tokens), tell me the exact manual steps for my OAuth provider / app passwords / `mcp-hub login`, and give me verification commands (`mcp-hub check-registry`, `/readyz`, 401 challenge check, and `list_accounts`).

## Self-hosting quick start

### 1. Prerequisites

1. **HTTPS ingress / tunnel** routing your public hostname (e.g. `https://mcp.example.com`) to **container port `8083` only**. Never expose port `8084` (`/healthz`, `/readyz`, `/metrics`) to the internet.
2. **An OAuth 2.1 authorization server** (self-hosted or managed IdP) that:
   - Exposes an RSA JWKS endpoint reachable by the hub (`AUTH_JWKS_URL`)
   - Issues **RS256** access tokens with header `typ: at+jwt` (or `application/at+jwt`) and claims:
     - `iss` = `AUTH_ISSUER`
     - `aud` containing `HUB_RESOURCE` (exact match, string or array)
     - `client_id` = `AUTH_EXPECTED_CLIENT_ID`
     - `sub` = a stable user ID that you put in `allowed-subjects`
     - `scope` containing **both** `mail:read` and `calendar:read`
     - `exp` and `iat` (and optional `nbf`)
   - Works with RFC 9728 protected-resource metadata served by the hub at `/.well-known/oauth-protected-resource/mcp`
3. **PostgreSQL** *(only if you configure a Microsoft Graph account)* with a database and non-superuser owner role; the hub runs its own schema migrations on first use.

### 2. Prepare the secrets directory (`HUB_SECRETS_DIR`)

Create a directory to mount read-only at `/etc/mcp-hub/secrets` (readable by uid/gid `10001`):

```text
secrets/
  accounts.json                # Account registry (required)
  allowed-subjects             # One allowed JWT `sub` per line; empty = reject all (required)
  icloud-username              # Referenced by *_ref fields in accounts.json
  icloud-app-password
  icloud-own-addresses         # Optional: 1–20 of your own addresses, one per line
  # Only needed when a Microsoft Graph account is enabled:
  outlook-ms-client-id         # Entra ID application (client) ID
  db-username                  # PostgreSQL role
  db-password                  # PostgreSQL password
  token-encryption-key         # 32 random bytes, standard base64: `openssl rand -base64 32`
```

Minimal `accounts.json` example (see [`tests/fixtures/accounts.example.json`](tests/fixtures/accounts.example.json) for IMAP, Gmail, Outlook Graph, and disabled accounts):

```json
{
  "version": 1,
  "accounts": [
    {
      "id": "icloud",
      "label": "iCloud",
      "provider": "icloud",
      "enabled": true,
      "capabilities": { "mail": true, "calendar": true },
      "mail": {
        "protocol": "imap",
        "host": "imap.mail.me.com",
        "port": 993,
        "username_ref": "icloud-username",
        "password_ref": "icloud-app-password",
        "inbox": "INBOX"
      },
      "calendar": {
        "protocol": "caldav",
        "url": "https://caldav.icloud.com/",
        "username_ref": "icloud-username",
        "password_ref": "icloud-app-password",
        "include_calendars": "all"
      }
    }
  ]
}
```

### 3. Configure environment variables

Override the built-in defaults (see [INTERFACES.md §5](INTERFACES.md#5-configuration) for all variables and ranges):

| Variable | Example for your setup | Notes |
|----------|------------------------|-------|
| `HUB_PUBLIC_HOST` | `mcp.example.com` | Must equal the host of `HUB_RESOURCE`; enforced on `Host` header |
| `HUB_RESOURCE` | `https://mcp.example.com/mcp` | HTTPS URL with path; enforced as JWT `aud` |
| `AUTH_ISSUER` | `https://auth.example.com` | Must match JWT `iss` and is advertised in protected-resource metadata |
| `AUTH_JWKS_URL` | `https://auth.example.com/.well-known/jwks.json` | Internal or HTTPS URL where the hub fetches RSA signing keys |
| `AUTH_EXPECTED_CLIENT_ID` | `my-mcp-client` | Must match JWT `client_id` |
| `HUB_SECRETS_DIR` | `/etc/mcp-hub/secrets` | Directory containing `accounts.json`, `allowed-subjects`, and credential files |
| `HUB_DEFAULT_TIMEZONE` | `UTC` | Any IANA time-zone name (used for calendar queries/formatting) |
| `HUB_STATUS_CHECK_ENABLED` | `true` | Set `true` in production to populate `list_accounts` health in the background |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | `postgres` / `5432` / `mcp_hub` | Only used when a Microsoft Graph account is enabled |

### 4. Run the container

Published multi-arch images (`linux/amd64` + `linux/arm64`) are available at `ghcr.io/doemefu/homelab-mcp-hub` tagged as `main-<YYYYMMDDTHHmmss>` and short commit SHA (there is no `latest` tag; pin a specific tag or digest, or build locally with `docker build -t mcp-hub:local .`):

```bash
docker run -d --name mcp-hub \
  --read-only --tmpfs /tmp:size=16m \
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --memory=320m --cpus=1 \
  -p 127.0.0.1:8083:8083 \
  -p 127.0.0.1:8084:8084 \
  -v /path/to/secrets:/etc/mcp-hub/secrets:ro \
  -e HUB_PUBLIC_HOST=mcp.example.com \
  -e HUB_RESOURCE=https://mcp.example.com/mcp \
  -e AUTH_ISSUER=https://auth.example.com \
  -e AUTH_JWKS_URL=https://auth.example.com/.well-known/jwks.json \
  -e AUTH_EXPECTED_CLIENT_ID=my-mcp-client \
  -e HUB_DEFAULT_TIMEZONE=UTC \
  -e HUB_STATUS_CHECK_ENABLED=true \
  mcp-hub:local
```

For Kubernetes, see [`k8s/`](k8s/) and [DEPLOYMENT.md](DEPLOYMENT.md) — treat `k8s/deployment.yaml` as a reference manifest and override the env vars, Secret, and image tag for your cluster.

### 5. Validate, sign in (Graph only), and verify

```bash
# 1. Validate accounts.json, credential files, and (for Graph) encryption key & token store:
docker exec mcp-hub mcp-hub check-registry

# 2. For Microsoft Graph accounts only, run the interactive device-code login in a TTY:
docker exec -it mcp-hub mcp-hub login outlook

# 3. Verify readiness (internal port 8084) and OAuth discovery/challenge (port 8083):
curl -fsS http://127.0.0.1:8084/readyz
curl -fsS -H 'Host: mcp.example.com' \
  http://127.0.0.1:8083/.well-known/oauth-protected-resource/mcp
curl -i -X POST -H 'Host: mcp.example.com' http://127.0.0.1:8083/mcp   # expect 401 with WWW-Authenticate
```

Then connect your MCP client (e.g., Claude custom connector) to `https://mcp.example.com/mcp`, complete OAuth login, and call `list_accounts`.

## Build, test & local smoke check

Prerequisites: Python 3.13 (`.python-version`), [uv](https://docs.astral.sh/uv/), and Docker (for the image smoke check and provider tests).

```bash
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest -m "not provider"    # unit, contract (gate G6), and integration tests

# Provider tests against local GreenMail, Radicale, and PostgreSQL containers (see CONTRIBUTING.md)
scripts/provider_services.sh up && HUB_PROVIDER_TESTS=1 uv run pytest -m provider -v; scripts/provider_services.sh down

# Hardened container smoke test (read-only FS, uid 10001, 401/200/403/421, throwaway signing key)
docker build -t mcp-hub:dev .
scripts/smoke_image.sh mcp-hub:dev
```

`scripts/smoke_image.sh` generates a throwaway RSA key, JWKS, and secrets directory from the example registry — no real credentials are involved.

## Project status & architecture spec

**Architecture contract:** Spec [`docs/080-mcp-hub.md`](https://github.com/doemefu/homelab/blob/main/docs/080-mcp-hub.md) and ADR [`docs/adr/0003-mcp-hub-authorization.md`](https://github.com/doemefu/homelab/blob/main/docs/adr/0003-mcp-hub-authorization.md) in the public [`doemefu/homelab`](https://github.com/doemefu/homelab) repository (Epic [doemefu/homelab#168](https://github.com/doemefu/homelab/issues/168)). This repository links to them and summarises the interface in [INTERFACES.md](INTERFACES.md).

| Area | Scope | Status |
|------|-------|--------|
| Core & auth (WP5a) | Streamable HTTP (`2026-07-28` + `2025-11-25`), RS256 JWKS verifier, allowlist kill switch, `list_accounts`, CI, multi-arch image | Done |
| IMAP mail (WP5b, WP8) | Read-only IMAP guard & adapter (iCloud, Gmail), `list_unread`, `get_message`, sanitiser, output budget | Done |
| CalDAV calendar (WP5c) | Read-only CalDAV adapter, bounded RFC 5545 recurrence expansion, `get_events`, background status check | Done |
| Metrics (WP5d) | Prometheus `/metrics` exposition on internal port 8084 (`mcp_hub_*`) | Done |
| Microsoft Graph (WP9) | Encrypted PostgreSQL token store, `mcp-hub login`, `mcp-hub check-registry`, Graph mail adapter (`outlook`) | Done |
| Mail history & search (WP13a–d) | `list_messages`, signed keyset cursors, `get_message` by `ref`, `get_message_status`, `get_thread`, `search_messages` | Done |
| Calendar feeds (WP10) | ICS calendar feed adapter & additional account types | Planned |

## Documentation

| File | Covers |
|------|--------|
| [INTERFACES.md](INTERFACES.md) | MCP endpoint, OAuth/JWT validation rules, all 8 tools, error codes, config, secrets, logging, CLI |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Kubernetes manifests, Flux bootstrap, registry checks, Graph login, kill switch, metrics, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Prerequisites, local environment, test suites, memory/IMAP probes, review process |
| [CHANGELOG.md](CHANGELOG.md) | Changes per release |
| [CLAUDE.md](CLAUDE.md) | Conventions for Claude Code sessions in this repo |

## Licence

MIT — see [LICENSE](LICENSE).
