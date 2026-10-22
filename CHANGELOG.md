# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Milestone 1 (stage a: no mailbox enabled, 2026-10-01; stage b: icloud mail + calendar enabled, 2026-10-02).

### Added

- Tool `list_messages` (spec 080 rev. 4.9 §5.2, WP13a): mail of `inbox`, `sent`, `archive`, `trash` or `all` (inbox + sent + archive, deduplicated, a message with a copy in sent shown as that copy; never trash, junk or drafts) since a point in time, optionally unread only or by `direction`, newest first across accounts. Pages continue with HMAC-signed keyset cursors (`next_cursor`, valid 1 h, bound to the tool and to the arguments as given, random key per process); `next_cursor` is set exactly when more items can be fetched with the same arguments, and a soft stop or an incomplete folder search returns the items found with `truncated: true` and no cursor.
- Extended `MessageSummary` for the mail list tools: `ref` (stable key from the Message-ID), `direction`, `flags`, `thread_key` and `thread_key_source`, and inside `untrusted` the first 10 `to_addresses` and `cc_addresses`, `message_id`, `in_reply_to`, the newest 3 `references` and `folder_name`; `id` is `null` for a message in a mailbox whose name cannot be carried in an id. A single item above the output budget drops references (oldest first), then cc, then to entries before its text is shortened, so a page always holds at least one item.
- `get_message` by `ref` (spec 080 rev. 4.9 §5.2, WP13a): exactly one of `id` and `ref` (`id` is no longer required in the input schema). A `ref` finds the message in any readable folder after a move and returns the `id` of the copy found (`null` when it cannot be encoded); a search that did not finish answers `upstream_timeout`, only a complete one `not_found`. The result gains `ref`, `direction`, `flags`, `thread_key`, `thread_key_source` and, inside `untrusted`, `message_id`, `in_reply_to`, the newest 10 `references` and `folder_name`; the output budget drops references (oldest first) before cc, to and attachment entries.
- Optional `mail.own_addresses_ref` per mail account: a credential file with the owner's own addresses (1–20, one per line), read on every use and never logged or returned; a missing or invalid file falls back to the folder rule with one `registry_value_invalid` WARN per state change. `check-registry` prints `account <id> own_addresses=<n>` or `invalid`.
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
- Bounded recurrence expansion (spec 080 rev. 4.5 D62): calendar objects larger than 1 MiB (after dropping unused heavy properties), with more than 1,000 events or 20 time zones, more than 1,000 `RDATE`/`EXDATE` values, sub-daily rules, more than one `BYMINUTE`/`BYSECOND` value, multiple or exclusion rules, more than one series or UID per object, or a daily/weekly rule starting before 1900 are skipped; 4 s CPU per object; `INTERVAL` below 1 or an unparseable `RRULE` refused; pod CPU limit 1 core; rules estimated to iterate more than 20,000 occurrences from their start are refused; per account and call 5 MiB of calendar data, 2,000 instances and 5 s of expansion; single events first; time-zone definitions isolated per object. Invalid bytes or forbidden characters inside a CalDAV response are replaced instead of failing the calendar; XML depth and size bounded; compressed responses refused; an explicit `:443` equals the default port.
- Calendar rules may use only the RFC 5545 rule parts, and only where RFC 5545 allows them for the frequency (`BYEASTER` and `MONTHLY` with `BYYEARDAY`/`BYWEEKNO` are refused); expansion runs without dateutil's occurrence cache, so memory stays flat.
- Rules inside time-zone definitions (`VTIMEZONE`) are screened before the object is parsed: only the shape real zones use (yearly, one month, one weekday entry or up to seven month days), at most 200 `RDATE` values per zone, zone sub-components counted towards the 1,000-component limit, and the rules of one zone held to 20,000 occurrences up to the year 9999.
- Line-boundary characters other than CR/LF are replaced by a space before a calendar object is read, and dateutil evaluates only recurrence rules the screens approved for the object.
- `get_events` reports `skipped_objects`: calendar entries that could not be read and are missing from the result. Heavy unused properties (`X-ALT-DESC`, inline attachments) are dropped before the 1 MiB object limit; up to 1,000 events per object; year-less birthdays and other early yearly/monthly series, and `BYHOUR` lists, are accepted; text is cut and cleaned off the event loop.
- Background status check for IMAP and CalDAV accounts (`HUB_STATUS_CHECK_ENABLED`, `HUB_HEALTH_CHECK_INTERVAL_SECONDS`), feeding `list_accounts`.
- CalDAV integration tests against Radicale in the `providers` job.
- Token store and `mcp-hub login` for Graph accounts (inert until the token-store keys and the `outlook` account exist): refresh tokens AES-256-GCM-encrypted in PostgreSQL (`psycopg[binary]` 3.3.6) with key ids derived from the key, built-in migrations, a row-locked rotation that commits before a new token is used, a Microsoft identity client for the device-code and refresh grants (one call deadline, capped streamed answers, no redirects), the `mcp-hub` wrapper in the image, and a PostgreSQL 17 container for the provider tests.
- Microsoft Graph mail adapter for `outlook` (`list_unread`, `get_message`, background status check): inbox only, `GET` only to `graph.microsoft.com:443`, no redirects, `@odata.nextLink` never followed (first page only), uncompressed answers capped at 1 MiB (lists) and 2 MiB (one message, with a preview fallback above it), one 20 s deadline per call including a token refresh, Graph message ids (kind `g`, folder `inbox`) bound to the inbox through `parentFolderId`; 401 → one refresh and retry, then `auth_expired`; 403 → `upstream_error` (`Forbidden`); 429/503 → a per-account back-off from `Retry-After`. Registry rules for Graph accounts: scope pattern, `offline_access` required, per-provider scope allow-lists, token-store files counted as credentials.
- `mcp-hub check-registry [--expect-sha <12 hex>]`: validates the mounted `accounts.json` with the server's own model before a pod deletion, proves its freshness by hash, checks credential files, the token key and the key id of each Graph account's stored token.
- `scripts/memory_probe.py --scenario graph-message [--with-calendar <scenario>]`: memory gate for the Graph caps next to the worst calendar call.

### Changed

- `list_unread` is rebuilt on `list_messages` (`folder=inbox`, `unread_only=true`): the signature gains `cursor`, items are the extended `MessageSummary`, and pages continue with `next_cursor` (bound to `list_unread`, `account` and `since`). `folder` in `list_unread` items is the normalised `inbox` for IMAP and Graph accounts alike (for IMAP it was the registry mailbox name, e.g. `INBOX`). The adapter-level unread path (`Mailbox.list_unread`, `UnreadPage`) is removed; both adapters serve `list_messages` only.
- IMAP `get_message`: an `EXAMINE` of the registry inbox answered `NO` now gives `upstream_timeout` (the read did not complete) instead of `upstream_error`.
- `get_message` ids are bound by D75 instead of the inbox-only rule: an id may name the inbox, sent, archive, trash or another readable mailbox (Gmail: inbox, All Mail, Trash); junk, drafts and excluded mailboxes answer `not_found`, without a provider call while the folder map is cached. `folder` in the result is the normalised folder of the message read (it was the id's mailbox name). Ids of the registry inbox issued earlier keep working.
- `gmail` account live (homelab#172): mail over the existing IMAP adapter, no code change (registry entry and credentials only, runbook in the `homelab` repository's `DEPLOYMENT.md`).

### Fixed

- IMAP searches on `Message-ID`, `In-Reply-To` and `References` send the id as the bracketed token `"<id>"`: iCloud matches only whole tokens, so `get_message` by `ref` found nothing there (O35).
- Undecodable header bytes no longer make `list_unread` fail for every account; one message that cannot be processed degrades only its own entry.
- `get_message` stays within `HUB_RESPONSE_BUDGET_CHARS` when recipients and attachments alone exceed it.
- HTML with many unclosed tags converts in linear time.
- A failing snippet fetch or a deeply nested MIME structure degrades only the affected messages; a text part of exactly 256 KiB is not reported as truncated.
- Hidden HTML content is removed by a region rule with constant state that errs towards hiding: a hidden element hides everything up to its balancing end tag (out-of-scope end tags, self-closing hidden elements and script escapes included), and an unterminated comment or declaration hides the rest. Hidden-content removal is best effort; its known limits are listed in INTERFACES.md.
- `scripts/smoke_image.sh` checks on the image's own interpreter that HTML conversion scales linearly on malformed input and keeps unterminated constructs hidden (`scripts/html_scaling_check.py`).

### Security

- Base image python:3.13-slim moved to a digest with Debian ca-certificates 20250419+deb13u1, which no longer trusts the emSign roots behind the compromised eMudhra intermediates; Python 3.13.15 → 3.13.16 with it (#45, #46; doemefu/homelab#212).
