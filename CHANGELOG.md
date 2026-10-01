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
- Read-only CalDAV adapter and the tool `get_events`: own discovery and time-range `REPORT` over HTTPS (credentials only to the configured host or the iCloud partition hosts, every response capped at 5 MiB, XML with document type or entity declarations refused), recurrence expansion with `recurring-ical-events` 3.8.2 and `icalendar` 7.3.0 (`RRULE`, `RDATE`, `EXDATE`, overridden and cancelled instances), time-zone conversion, all-day events as dates.
- Bounded recurrence expansion (spec 080 rev. 4.5 D62): calendar objects with sub-daily, multiple or exclusion rules, more than 1,000 `RDATE`/`EXDATE` values or a start before 1900 are skipped; 2 s CPU per object, 2,000 instances and 5 s of expansion per account and call; single events first. Invalid UTF-8 inside a CalDAV response is replaced instead of failing the calendar.
- Background status check for IMAP and CalDAV accounts (`HUB_STATUS_CHECK_ENABLED`, `HUB_HEALTH_CHECK_INTERVAL_SECONDS`), feeding `list_accounts`.
- CalDAV integration tests against Radicale in the `providers` job.
