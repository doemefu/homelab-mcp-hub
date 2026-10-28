# CLAUDE.md — homelab-mcp-hub

In-progress worklogs are listed at session start by the `homelab-workflow` plugin; start work with `/homelab-workflow:start-task`. Durable facts go to auto memory.

> `.claude/memory/`, `.claude/worklogs/` and `.claude/worktrees/` are gitignored — local-only; cross-check `git log` and GitHub when they look stale.

## Service Overview

A read-only remote MCP server for the owner's mail and calendars. It is a pure OAuth resource server: auth-service issues the access tokens, the hub validates them offline against the auth-service JWKS and serves MCP tools over Streamable HTTP.

**Ports:** 8083 (MCP endpoint `/mcp` + protected-resource metadata; the only port behind the Cloudflare Tunnel), 8084 (cluster-internal `/healthz`, `/readyz`)
**Package:** `src/mcp_hub`
**Contract:** `docs/080-mcp-hub.md` and ADR `docs/adr/0003-mcp-hub-authorization.md` in the `doemefu/homelab` (infrastructure) repo — linked, never copied.

## The contract

**`docs/080-mcp-hub.md` in the `doemefu/homelab` repo is the single source of truth** (locally `../infrastructure/docs/080-mcp-hub.md`). Tool shapes, error codes, token validation rules, the challenge text, env vars, Secret keys, the `accounts.json` schema and the log fields are implemented from it. A deviation is a spec change first, then code. At task start, also read the `README.md` status table, `INTERFACES.md` and the contract sections the task touches.

## Non-Negotiables

- Do **not** touch secrets, SOPS files or credentials; secrets reach the pod only as files in `HUB_SECRETS_DIR` (Secret `mcp-hub-secrets`, written by the `homelab` repo's playbook 59).
- Do **not** use `latest` or version ranges — exact `==` pins, `uv.lock` with hashes, actions by SHA, base images by digest.
- Do **not** introduce new dependencies without explicit user approval (the approved set is spec 080 §9.3).
- Do **not** log tokens, `Authorization` headers, header values, provider URLs, addresses or content; only the fields of spec 080 §9.7.
- The hub is read-only: no tool writes to a provider.
- This repository is public: no account identifiers, usernames, addresses or real data in code, tests, fixtures, docs or commit messages.
- Commit, push and open PRs on feature branches without asking. Merging PRs is always the user's. Rolling out changes the user has already merged needs no extra go; force-pushes, playbook runs and other cluster mutations need an explicit go. SOPS files are never read or edited by Claude — the user edits them.
- Before any merge, wait for the Copilot review and fix or answer every comment (see `/homelab-workflow:review-gate`).
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
| Tooling | uv 0.12.19 (CI) / 0.12.23 (image), ruff 0.16.9, mypy 2.3.1 `--strict`, pytest 9.1.1, pytest-httpserver 1.1.5 |

## Conventions

- Every token validation rule (spec §4.3) has a check name in `auth.py` and a contract test in `tests/contract/` (gate G6); the SDK enforces the scopes and Host/Origin, the hub the rest.
- The account registry (`accounts.json`) is configuration, validated by pydantic at start-up; an invalid registry stops the process (exit 2).
- No content storage: account status is kept in memory only; tool results are built per request.
- Provider adapters return decoded text; the tool layer sanitises it into `untrusted` and applies the output budget.
- The background status check runs only from the production entry point (`create_app(..., background_checks=True)` in `__main__`) and only when `HUB_STATUS_CHECK_ENABLED=true`; tests and local runs never contact a provider.
- Third-party loggers are capped at WARNING (`mcp.server.transport_security` at ERROR); hub logs are JSON lines on stdout.

## Agent Team

Plugin agents `homelab-workflow:<role>` (`architect`, `implementer`, `reviewer`, `plan-reviewer`, `doc-auditor`, `documenter`, `devops`). Repo specifics (contract location, layout, checklists, read-only `devops`) are in `.claude/agent-context.md`, one section per role.

Repo-only agents in `.claude/agents/`: the domain advisors `imap-advisor`, `graph-advisor`, `mailformat-advisor`, `security-advisor`, `tooldesign-advisor`. For mail, provider or tool-interface work, run the matching advisors in Phase 3 and for the final review, in parallel, as a named team that messages each other and writes to a shared notes folder.

## Process & Conventions

Plugin skills: `/homelab-workflow:start-task`, `workflow` (6-phase workflow, worklogs, plan structure), `review-gate`, `project-board` (Project #5), `cross-repo`.

Rules in `.claude/rules/` (path-scoped): `code-style-conventions.md` (Python, errors, logging, tests), `documentation-files.md` (docs purpose and owner; docs are written in parallel with the code; link to the contract, never copy it).

Review: request Copilot first; CodeRabbit is not installed here. If Copilot does not review, use `homelab-workflow:reviewer` plus `/code-review` and state in a PR comment which path was used.

## Commands

Source `.local/env.sh` first (worktree-local venv, uv cache and TMPDIR; no global installs; see CONTRIBUTING.md "Local environment"). Python 3.13 per `.python-version`.

```bash
uv sync --locked                          # create .venv from uv.lock (fails if the lock is stale)
uv run pytest                             # all tests (unit, contract, integration)
uv run pytest tests/contract -v           # gate G6 contract tests only
scripts/provider_services.sh up           # GreenMail + Radicale test containers (per-run passwords in $TMPDIR)
HUB_PROVIDER_TESTS=1 uv run pytest -m provider -v   # provider integration tests
scripts/provider_services.sh down         # stop and remove the test containers
uv run pytest tests/unit/test_verifier.py::test_name -v
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
PYTHONPATH=src uv run python -m mcp_hub  # run locally; needs HUB_SECRETS_DIR with accounts.json + allowed-subjects
docker build -t mcp-hub:dev .             # container image
scripts/smoke_image.sh mcp-hub:dev        # local image check (read-only FS, uid 10001, 401/200/403/421)
uv lock                                   # after editing pyproject.toml (exact == pins only)
uv lock --upgrade-package <name>          # bump one package (owner approval for new packages)
actionlint                                # GitHub workflow lint
kubectl kustomize k8s                     # render manifests (offline)
# Policy path relative to a clone at homelab/mcp-hub; from a worktree under .claude/worktrees/<name>/
# use ../../../../infrastructure/policy/kubernetes/ instead.
conftest test --rego-version v0 --policy ../infrastructure/policy/kubernetes/ --all-namespaces <(kubectl kustomize k8s)
```
