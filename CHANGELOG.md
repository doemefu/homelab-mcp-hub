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
