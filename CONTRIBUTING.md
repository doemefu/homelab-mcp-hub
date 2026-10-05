# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (CI and the image pin 0.12.19; another local patch version works)
- Python 3.13 (`.python-version`)
- Docker (image build, `scripts/smoke_image.sh` and the provider test containers)
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
  sanitize.py      untrusted-content rules and field limits (spec §5.3, §5.4)
  budget.py        output budget (spec §5.4)
  ids.py           opaque message and event ids (spec §5.1)
  providers/       base.py (deadlines, connection limits, inbound limits), mime.py, imap.py (read-only IMAP adapter), caldav.py (read-only CalDAV adapter and recurrence expansion)
  tools/           list_accounts, mail (list_unread, get_message); common.py: account selection and aggregation
tests/
  unit/            per-module tests
  contract/        gate G6: the authorization rules of spec §10.3 over HTTP
  integration/     the real process on two ports; test_imap.py against GreenMail (marker `provider`)
  support/         keys, tokens, clock, MCP wire helpers, GreenMail helpers, log capture
  fixtures/        example registry (neutral ids, no real data)
scripts/           dev_token.py, smoke_image.sh, html_scaling_check.py, provider_services.sh (development only, not in the image)
  integration/     the real process on two ports; test_imap.py against GreenMail, test_caldav.py against Radicale (marker `provider`)
  support/         keys, tokens, clock, MCP wire helpers, GreenMail and Radicale helpers, ICS fixture loader, canned DAV answers, log capture
  fixtures/        example registry (neutral ids, no real data), ics/ calendar fixtures, radicale/ test server config
scripts/           dev_token.py, smoke_image.sh, provider_services.sh (development only, not in the image)
k8s/               Deployment and Service for namespace apps
```

## Running tests

```bash
uv run pytest tests/unit -v
uv run pytest tests/contract -v      # gate G6
uv run pytest tests/integration -v   # starts python -m mcp_hub as a subprocess
```

### Memory probe

`scripts/memory_probe.py` measures `get_events` memory and CPU against an in-process fake CalDAV server (synthetic data only, no network). Run it in the production image under the pod's limits; the repository is mounted read-only and the script is not part of the image:

```bash
docker build -t mcp-hub:dev .
docker run --rm --memory=256m --memory-swap=256m --cpus=1 --read-only --tmpfs /tmp \
  -v "$PWD":/work:ro -e PYTHONPATH=/app/src:/work mcp-hub:dev \
  python /work/scripts/memory_probe.py --scenario nonascii-240k --calls 10
```

It prints peak RSS and CPU per call, then the container's cgroup `memory.peak` and `memory.events`; `--accounts 2` queries two accounts concurrently. Re-run the scenarios in INTERFACES.md's memory table after any change to the calendar limits.

`--scenario graph-message` drives `get_message` for an `outlook` entry through the real Graph adapter and token source (in-memory token store, in-process Graph transport): the message answer fills the 2 MiB cap with a non-ASCII body and 5,000 recipients, the attachment answer fills the 1 MiB list cap. `--with-calendar <scenario>` runs that calendar scenario concurrently in the same process; the gate for the Graph limits is `--scenario graph-message --with-calendar nonascii-240k --calls 10` with cgroup `memory.peak` at most 230 MiB and `oom_kill 0`. Re-run it after any change to the Graph response caps.

### Provider integration tests

The IMAP adapter is tested against a local GreenMail container, the CalDAV adapter against a local Radicale container and the token store against PostgreSQL 17 (the cluster's image, port 5433; the tests run as a non-superuser role that owns database `mcp_hub`, as production does) — all pinned by digest. `scripts/provider_services.sh up` starts all three on `127.0.0.1` with neutral `example.test` users and passwords generated per run (written to `$HUB_PROVIDER_STATE_DIR` or `$TMPDIR`, never committed; Radicale reads a per-run copy of `tests/fixtures/radicale/config`); `down` stops and removes them. The calendar fixtures in `tests/fixtures/ics/` (DST change, overrides, `EXDATE`, `RDATE`, cancellations, all-day, floating, cross-zone, window edge, hostile text) are checked in unit tests and again through Radicale; `tests/support/ics.py` loads them with CRLF line endings and turns the `{ZWSP}` placeholder into a zero-width space, so no invisible character is committed.

```bash
scripts/provider_services.sh up
HUB_PROVIDER_TESTS=1 uv run pytest -m provider -v
scripts/provider_services.sh down
```

Without `HUB_PROVIDER_TESTS=1` the provider tests are skipped; CI runs them in the separate `providers` job, and `lint-and-test` runs `pytest -m "not provider"`. TLS is verified against the certificate the local container presents (pinned per run); only the host-name check is off in the test helper.

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
