# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Authorization layer: offline validation of auth-service access tokens (RS256 via a throttled JWKS cache, `typ`, issuer, audience, client, time with 60 s leeway, required claims, subject allowlist re-read every 60 s), both scopes required, Bearer challenges with `resource_metadata` and `scope`, protected-resource metadata, Host/Origin checks.
- MCP endpoint `/mcp` on port 8083 for protocol versions 2026-07-28 and 2025-11-25; `/healthz` and `/readyz` on port 8084.
- Tool `list_accounts` with per-capability status from the account registry (`accounts.json`) and in-memory state.
- JSON logging with a fixed field set; third-party loggers capped.
- Contract tests for gate G6, unit and integration tests.
- Multi-stage non-root image, local image check (`scripts/smoke_image.sh`), CI, native multi-arch build, CodeQL, Dependabot.
- Kubernetes manifests for namespace `apps`; README, CONTRIBUTING, INTERFACES, DEPLOYMENT, LICENSE (MIT).
- Read-only IMAP adapter (IMAPClient 4.1.0; `EXAMINE` and `BODY.PEEK` only, partial fetches within the §5.4 inbound limits) and the tools `list_unread` and `get_message`, with per-account errors, a 20 s provider and 60 s tool deadline and at most two connections per account.
- Sanitiser for third-party content (invisible and control characters, links reduced to their host, hidden HTML dropped), opaque message and event ids, and the output budget `HUB_RESPONSE_BUDGET_CHARS`.
- Provider integration tests against GreenMail (`scripts/provider_services.sh`) and the CI job `providers`.

### Fixed

- Undecodable header bytes no longer make `list_unread` fail for every account; one message that cannot be processed degrades only its own entry.
- `get_message` stays within `HUB_RESPONSE_BUDGET_CHARS` when recipients and attachments alone exceed it.
- HTML with many unclosed tags converts in linear time.
- A failing snippet fetch or a deeply nested MIME structure degrades only the affected messages; a text part of exactly 256 KiB is not reported as truncated.
- Hidden HTML content is removed by a region rule with constant state that errs towards hiding: a hidden element hides everything up to its balancing end tag (out-of-scope end tags, self-closing hidden elements and script escapes included), and an unterminated comment or declaration hides the rest. Hidden-content removal is best effort; its known limits are listed in INTERFACES.md.
- `scripts/smoke_image.sh` checks on the image's own interpreter that HTML conversion scales linearly on malformed input and keeps unterminated constructs hidden (`scripts/html_scaling_check.py`).
