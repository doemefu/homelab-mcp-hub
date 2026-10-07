# homelab-mcp-hub

A read-only remote [MCP](https://modelcontextprotocol.io) server for mail and calendars, part of the doemefu homelab (Epic [doemefu/homelab#168](https://github.com/doemefu/homelab/issues/168)).

The hub is a pure OAuth resource server: the homelab's auth-service issues the access tokens, the hub validates them offline against the auth-service JWKS and serves MCP tools over Streamable HTTP. Claude connects to it as a custom connector.

**Contract:** spec [`docs/080-mcp-hub.md`](https://github.com/doemefu/homelab/blob/main/docs/080-mcp-hub.md) and ADR [`docs/adr/0003-mcp-hub-authorization.md`](https://github.com/doemefu/homelab/blob/main/docs/adr/0003-mcp-hub-authorization.md) in the `doemefu/homelab` repository (published there with [doemefu/homelab#182](https://github.com/doemefu/homelab/pull/182)). This repository links to them and carries no copy.

## Status

Milestone 1 (#170): iCloud mail and calendar live at mcp.furchert.ch. `outlook` mail live since 2026-10-06 (#171), `gmail` mail live since 2026-10-07 (#172, configuration only). Calendars of other accounts are served through iCloud.

| Work package | Scope | Status |
|--------------|-------|--------|
| WP5a | Repository bootstrap, authorization layer, `list_accounts`, image, CI, `k8s/` | Done |
| WP5b | IMAP adapter, `list_unread`, `get_message`, sanitiser, output budget, provider CI job | Done |
| WP5c | CalDAV adapter, `get_events`, background status check | Done |
| WP5d | `search_mail`, paging, metrics on the internal port | Planned |
| WP9 | Microsoft Graph for `outlook`: token store, `mcp-hub login`, mail adapter, `mcp-hub check-registry` | Done (live) |
| WP8 | `gmail` mail over the existing IMAP adapter (registry entry and credentials only) | Done (live) |
| WP10 | Further accounts (calendar feed) | Planned |

## Quick reference

| Item | Value |
|------|-------|
| MCP endpoint | `https://mcp.furchert.ch/mcp` (container port **8083**; the only port behind the Cloudflare Tunnel) |
| Protected-resource metadata | `/.well-known/oauth-protected-resource/mcp` on port 8083 |
| Health | `/healthz`, `/readyz` on port **8084** (cluster-internal) |
| Protocol versions | `2026-07-28` (stateless) and `2025-11-25` (initialize handshake) |
| Scopes | `mail:read calendar:read` (both required) |
| Image | `ghcr.io/doemefu/homelab-mcp-hub:main-<UTC timestamp>` (linux/amd64 + linux/arm64) |
| Tools | `list_accounts`, `list_unread`, `get_message`, `get_events` (all read-only); mail over IMAP or Microsoft Graph (`outlook`), calendars over CalDAV |
| Stack | Python 3.13, `mcp` 2.2.0, uvicorn, starlette, pydantic, PyJWT, IMAPClient, icalendar, recurring-ical-events |
| Deployment | Flux from `k8s/` into namespace `apps` (see [DEPLOYMENT.md](DEPLOYMENT.md)) |

## Build and test

```bash
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest -m "not provider"    # unit, contract (gate G6) and integration tests
# provider tests against local GreenMail, Radicale and PostgreSQL containers (see CONTRIBUTING.md)
scripts/provider_services.sh up && HUB_PROVIDER_TESTS=1 uv run pytest -m provider; scripts/provider_services.sh down
```

## Run locally

```bash
docker build -t mcp-hub:dev .
scripts/smoke_image.sh mcp-hub:dev   # starts the image read-only as uid 10001, checks 401/200/403/421 and the HTML converter
```

`scripts/smoke_image.sh` generates a throwaway signing key, a JWKS and a secrets directory with the example registry, so no real credentials are involved.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for prerequisites, the local environment, the test suites and the review process.

## Documentation

| File | Covers |
|------|--------|
| [INTERFACES.md](INTERFACES.md) | MCP endpoint, authorization rules, tools, configuration, logging |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Manifests, image, bootstrap sequence, operations, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local development, tests, process |
| [CHANGELOG.md](CHANGELOG.md) | Changes per release |
| [CLAUDE.md](CLAUDE.md) | Conventions for Claude Code sessions |

## Licence

MIT, see [LICENSE](LICENSE).
