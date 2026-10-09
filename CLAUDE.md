# CLAUDE.md — homelab-mcp-hub

> **Session start:** Read `.claude/memory/MEMORY.md` completely if it exists. The topmost entry shows the current state. If there is an entry with `status: in_progress`, read the linked worklog and ask the user: *"I see we were interrupted at [SLUG]. Continue?"* — before doing anything else.

> **After each completed change:** Insert a new block **at the top** of `.claude/memory/MEMORY.md`.

> `.claude/memory/`, `.claude/worklogs/` and `.claude/worktrees/` are gitignored — local-only; cross-check `git log` and GitHub when they look stale.

## Service Overview

A read-only remote MCP server for the owner's mail and calendars. It is a pure OAuth resource server: auth-service issues the access tokens, the hub validates them offline against the auth-service JWKS and serves MCP tools over Streamable HTTP.

**Ports:** 8083 (MCP endpoint `/mcp` + protected-resource metadata; the only port behind the Cloudflare Tunnel), 8084 (cluster-internal `/healthz`, `/readyz`)
**Package:** `src/mcp_hub`
**Contract:** `docs/080-mcp-hub.md` and ADR `docs/adr/0003-mcp-hub-authorization.md` in the `doemefu/homelab` (infrastructure) repo — linked, never copied.

## The contract

**`docs/080-mcp-hub.md` in the `doemefu/homelab` repo is the single source of truth** (locally `../infrastructure/docs/080-mcp-hub.md`). Tool shapes, error codes, token validation rules, the challenge text, env vars, Secret keys, the `accounts.json` schema and the log fields are implemented from it. A deviation is a spec change first, then code.

## Non-Negotiables

- Do **not** touch secrets, SOPS files or credentials; secrets reach the pod only as files in `HUB_SECRETS_DIR` (Secret `mcp-hub-secrets`, written by the `homelab` repo's playbook 59).
- Do **not** use `latest` or version ranges — exact `==` pins, `uv.lock` with hashes, actions by SHA, base images by digest.
- Do **not** introduce new dependencies without explicit user approval (the approved set is spec 080 §9.3).
- Do **not** log tokens, `Authorization` headers, header values, provider URLs, addresses or content; only the fields of spec 080 §9.7.
- The hub is read-only: no tool writes to a provider.
- This repository is public: no account identifiers, usernames, addresses or real data in code, tests, fixtures, docs or commit messages.
- Commit, push and open PRs on feature branches without asking (standing permission, 2026-08-28). Merging, force-pushes, playbook runs, cluster mutations and anything touching SOPS/secrets need an explicit go for that task.
- Before any merge, wait for the Copilot review and fix or answer every comment (see `.claude/rules/workflow.md` Phase 5).
- All code, comments and documentation in **English**. Minimize diff size: no drive-by refactors.

## Tech Stack (pinned)

| Component | Version |
|-----------|---------|
| Python | 3.13 (`python:3.13-slim@sha256:…` runtime image) |
| MCP SDK | `mcp` 2.2.0 (`MCPServer`, Streamable HTTP) |
| HTTP | uvicorn 0.54.0, starlette 1.7.0, httpx2 2.13.1, anyio 4.15.1 |
| Validation / tokens | pydantic 2.13.5, PyJWT 2.15.1 (`[crypto]`) |
| Mail | IMAPClient 4.1.0 |
| Calendar | icalendar 7.3.0, recurring-ical-events 3.8.2 (CalDAV requests over httpx2) |
| Token store | psycopg[binary] 3.3.6 (sync API), cryptography 50.0.1 (AES-256-GCM; direct runtime pin) |
| Tooling | uv 0.12.19, ruff 0.16.9, mypy 2.3.1 `--strict`, pytest 9.1.1, pytest-httpserver 1.1.5 |

## Conventions

- Every token validation rule (spec §4.3) has a check name in `auth.py` and a contract test in `tests/contract/` (gate G6); the SDK enforces the scopes and Host/Origin, the hub the rest.
- The account registry (`accounts.json`) is configuration, validated by pydantic at start-up; an invalid registry stops the process (exit 2).
- No content storage: account status is kept in memory only; tool results are built per request.
- Provider adapters return decoded text; the tool layer sanitises it into `untrusted` and applies the output budget.
- The background status check runs only from the production entry point (`create_app(..., background_checks=True)` in `__main__`) and only when `HUB_STATUS_CHECK_ENABLED=true`; tests and local runs never contact a provider.
- Third-party loggers are capped at WARNING (`mcp.server.transport_security` at ERROR); hub logs are JSON lines on stdout.

## Agent Team

Project agents in `.claude/agents/`: `architect` (contract first), `implementer`, `reviewer`, `plan-reviewer` (Phase 3), `doc-auditor` (Phase 6), `documenter`, `devops` (read-only cluster checks).

## Process & Conventions

Rules in `.claude/rules/`: `workflow.md` (6-phase workflow), `worklog-conventions.md`, `plan-structure.md`, `commands.md`, `code-style-conventions.md`, `review-guidelines.md`, `documentation-files.md`, `github-project.md` (Project #5 status transitions). Skill: `.claude/skills/start-task/`. Worklog template: `.claude/worklog-template.md`.
