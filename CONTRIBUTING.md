# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (CI and the image pin 0.12.19; another local patch version works)
- Python 3.13 (`.python-version`)
- Docker (image build and `scripts/smoke_image.sh`)
- Optional: `actionlint`, `kubectl` and `conftest` for the workflow and manifest checks

## Local environment

All tool state stays inside the checkout, so a working copy can be removed without leaving anything behind:

- The project virtual environment is `.venv/` in the checkout (`uv sync --locked`).
- Agents working in a worktree keep the uv cache, uv tool/python directories and `TMPDIR` under `.local/` (gitignored) via a `.local/env.sh` that exports `UV_CACHE_DIR`, `UV_TOOL_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_DOWNLOADS=never`, `UV_PROJECT_ENVIRONMENT` and `TMPDIR`, and source it before every command.
- No global or user-level installs (`pip install --user`, `uv tool install`, `uv python install`, Homebrew).
- Cleanup: remove the worktree with `git worktree remove` (it removes the ignored `.venv/` and `.local/` with it) and the local images built for the work package; shared caches are left alone.

## Local development

```bash
uv sync --locked                           # install the locked dependencies into .venv
PYTHONPATH=src uv run python -m mcp_hub   # needs HUB_SECRETS_DIR with accounts.json and allowed-subjects
docker build -t mcp-hub:dev . && scripts/smoke_image.sh mcp-hub:dev
```

Dependencies are exact `==` pins in `pyproject.toml` with hashes in `uv.lock`. After editing `pyproject.toml`, run `uv lock`. New packages need the owner's approval (spec 080 §9.3).

## Project structure

```
src/mcp_hub/
  __main__.py      entry point: one uvicorn server on ports 8083 and 8084
  app.py           ASGI composition: SDK MCP app, challenge wrapper, request log, health app, port dispatcher
  auth.py          token verifier (spec §4.3 rows 1-8) and subject allowlist
  jwks.py          JWKS cache with refetch throttling
  config.py        environment settings
  registry.py      accounts.json validation and capability filtering
  health.py        in-memory per-capability status
  errors.py        tool error codes
  logging.py       JSON logs, logger levels
  tools/           list_accounts (later: mail, calendar)
tests/
  unit/            per-module tests
  contract/        gate G6: the authorization rules of spec §10.3 over HTTP
  integration/     the real process on two ports
  support/         keys, tokens, clock, MCP wire helpers
  fixtures/        example registry (neutral ids, no real data)
scripts/           dev_token.py, smoke_image.sh (development only, not in the image)
k8s/               Deployment and Service for namespace apps
```

## Running tests

```bash
uv run pytest tests/unit -v
uv run pytest tests/contract -v      # gate G6
uv run pytest tests/integration -v   # starts python -m mcp_hub as a subprocess
```

The contract tests mint real RS256 tokens with a key generated per test session and serve the JWKS with pytest-httpserver; they send the 2026-07-28 wire format (headers `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` and `params._meta`) and the 2025-11-25 handshake. Each row of spec §10.3 maps to one test; the map is in [INTERFACES.md](INTERFACES.md) §1.

## Process

- Six-phase workflow with a worklog per change (`.claude/rules/workflow.md`).
- Branch `feat/<issue>-<slug>` (or `fix/…`, `chore/…`), conventional commits, one pull request per change against `main`.
- Review: the Copilot review requested by the repository ruleset; if its quota is exhausted, the substitute review path (reviewer agent + `/code-review`) is used and disclosed in the pull request.
- Every review comment is fixed or answered before the merge; merging needs the owner's go.

## Code review checklist

See `.claude/rules/review-guidelines.md`. In short: no secrets or request data in logs, gate G6 green, exact pins, read-only tools, third-party strings only inside `untrusted` objects, minimal diffs with tests.

## Developer reminders

- Log only the fields of spec §9.7 through `log_event`; never tokens, `Authorization` values, header values, provider URLs, addresses or content.
- Actions are pinned by commit SHA, images by digest; no `latest`.
- This repository is public: no account identifiers, usernames, addresses or real data in code, tests, fixtures, docs or commit messages.
