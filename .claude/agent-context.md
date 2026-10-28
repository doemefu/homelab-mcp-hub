# Agent context — mcp-hub

Repo-specific context for the `homelab-workflow` agents; each agent reads only its own section. The contract lives in `docs/080-mcp-hub.md` and ADR `docs/adr/0003-mcp-hub-authorization.md` in the `homelab` repo (locally `../infrastructure/docs/`). On any conflict between the notes below and the contract, the contract wins. This repository is public: no account identifiers, usernames, addresses or real data anywhere.

## architect

- Stack: Python 3.13 / official `mcp` SDK / Starlette / uvicorn / pydantic / PyJWT.
- Contract (single source of truth) is `docs/080-mcp-hub.md` in the `homelab` repo, not this repo. A change to a tool shape, an error code, a token validation rule, an env var, a Secret key, the `accounts.json` schema or the log fields is written there **first**; `INTERFACES.md` then only summarises it.
- Read, in order: the contract sections the change touches, `INTERFACES.md`, ADR 0003, then the code under `src/mcp_hub/`.
- State explicitly:
  - Authorization: every rule of spec §4.3 keeps its check name and its contract test in `tests/contract/` (gate G6); the challenge text of §4.4 is unchanged.
  - Read-only: no tool writes to a provider; annotations `readOnlyHint: true`, `destructiveHint: false`.
  - Output envelope (§5.1): third-party strings only inside `untrusted` objects; error codes from the fixed list.
  - Configuration: new env vars have defaults and validation in `config.py`; new Secret keys follow the §7.1 key pattern.
  - Privacy: no tokens, header values, addresses, provider URLs or content in logs (§9.7).
- Output: the spec diff (contract + `INTERFACES.md`), open questions, and which consumers must follow.

## implementer

- Stack: Python 3.13, `mcp` SDK (`MCPServer`), uvicorn, starlette, pydantic, PyJWT, httpx2, anyio; tooling uv, ruff, mypy `--strict`, pytest + pytest-httpserver. Exact versions live in `pyproject.toml` and `uv.lock` only.
- Also read the contract sections the plan cites (`docs/080-mcp-hub.md` in the `homelab` repo) and `INTERFACES.md` before coding.
- Layout under `src/mcp_hub/`: `config.py` (env), `registry.py` (accounts.json), `auth.py` (verifier + allowlist), `jwks.py`, `app.py` (ASGI composition), `health.py` (in-memory status), `errors.py`, `logging.py`, `tools/<domain>.py`, `providers/<protocol>.py`, `tokenstore/`.
- Auth layer changes need a contract test per spec §10.3 row (`tests/contract/`, gate G6).
- No logging of tokens, headers, addresses or URLs; only the fields of spec §9.7.
- No new dependency without owner approval (approved set: spec §9.3); exact `==` pins, `uv.lock` updated in the same change.
- Source `.local/env.sh` first; `uv run pytest` + `uv run ruff check` + `uv run ruff format --check` + `uv run mypy --strict src` green before a commit.

## reviewer

The plugin reviewer has no Bash: check the items below by reading the code and the tests in the diff.
- [ ] `/mcp` answers 401 with the spec §4.4 challenge (incl. `scope=`) without a valid token; 403 `insufficient_scope` with `scope=`.
- [ ] Token rules of spec §4.3 unchanged or changed in the contract first; every rule has a contract test (gate G6). Every change to `auth.py`, `jwks.py`, `app.py` or the SDK version keeps all `tests/contract` green; a new rule gets a new contract test row.
- [ ] No tokens, `Authorization` values, header values, provider URLs, addresses or content in logs, tool errors or metrics labels (§9.7); a log-capture test covers every new code path that talks to a provider.
- [ ] Health routes only on the internal port 8084; nothing but `/mcp` and the protected-resource metadata on 8083.
- [ ] Tools read-only (`readOnlyHint: true`, `destructiveHint: false`); every third-party string only inside an `untrusted` object (§5.1, §5.3); error codes from the fixed list.
- [ ] Registry and env validation fail closed and never echo input values.
- [ ] Tests: real signed tokens, JWKS via pytest-httpserver, log-capture assertions; unit + contract/integration tests for new behaviour.
- [ ] Exact `==` pins with `uv.lock` updated in the same PR; actions by SHA, images by digest; no unapproved dependency (§9.3); image non-root with a read-only root FS.

## documenter

Docs to keep in sync (all English):
- `README.md`: status table per work package, quick reference.
- `CONTRIBUTING.md`: prerequisites, local environment, tests, process.
- `INTERFACES.md`: MCP endpoint, authorization rules, tools, errors, config, logging; summarises (never contradicts) `docs/080-mcp-hub.md` in the `homelab` repo.
- `DEPLOYMENT.md`: manifests, bootstrap/owner actions, verification, troubleshooting.
- `CHANGELOG.md`: `[Unreleased]` entry.
- `CLAUDE.md`: only when conventions change.
- Public repository: describe behaviour and tasks, never account identifiers, usernames or addresses. Never copy the contract into this repo; link to it.

## devops

- Read-only role in this repo (original tools: Read, Bash, Grep; no Write/Edit): report findings, do not edit files and never mutate the cluster.
- Namespace `apps`, Deployment/Service `mcp-hub` (:8083 MCP, :8084 health). Flux objects `flux-system/mcp-hub` come from the `homelab` repo's `cluster/apps/mcp-hub/`.
- The Secret `mcp-hub-secrets` is provisioned by the `homelab` repo's playbook 59 via SOPS; never by this agent.
- Do not touch `k8s/` manifests (Flux owns the image tag), SOPS files or anything in the `homelab` repo.
- Read-only checks:
  ```bash
  flux get kustomizations mcp-hub -n flux-system
  flux get images all -n flux-system | grep mcp-hub
  kubectl -n apps rollout status deployment/mcp-hub --timeout=120s
  kubectl -n apps get pods -l app=mcp-hub
  kubectl -n apps logs deployment/mcp-hub --tail=50
  ```
- Offline manifest checks: `kubectl kustomize k8s`; `conftest test --rego-version v0 --policy ../infrastructure/policy/kubernetes/ --all-namespaces <(kubectl kustomize k8s)`; `actionlint` for workflows.

## plan-reviewer

- **Contract:** does the plan implement `docs/080-mcp-hub.md` exactly (tool names, input/output shapes, error codes, status codes, challenge text, check names)? Is any deviation raised as a spec change first?
- **Authorization:** every §4.3 rule still enforced and covered by a contract test; no path that answers 2xx without a valid token; no 500 on malformed input.
- **Secrets:** only files in `HUB_SECRETS_DIR` / env vars; no credentials in code, fixtures or logs; test keys generated in memory.
- **Pinning:** exact `==` pins with `uv.lock` in the same PR, actions by SHA, images by digest; new dependencies approved by the owner (spec §9.3)?
- **Read-only:** no tool writes to a provider; every tool declares `readOnlyHint: true`, `destructiveHint: false`.
- **Logging:** only §9.7 fields; third-party loggers capped; a log-capture test for new provider code.
- **Untrusted content:** every third-party string goes into an `untrusted` object (§5.1, §5.3).
- **Tests:** real RS256 tokens against a JWKS served by pytest-httpserver; no mocking of the SDK's auth middleware; unit + contract/integration tests required.
- **K8s (if touched):** limits, probes on 8084, pinned tag + Flux marker, namespace `apps`, read-only root FS.
- **Domain advisors:** for mail, provider or tool-interface work, the plan has been through the matching advisors (`imap-`, `graph-`, `mailformat-`, `security-`, `tooldesign-advisor`).
- **Diff size:** nothing beyond the stated goal.

## doc-auditor

- Context: mcp-hub is a read-only remote MCP server for mail and calendars. It is a pure OAuth resource server for tokens issued by auth-service, deployed by Flux into namespace `apps` and reachable through the Cloudflare Tunnel on port 8083 only. Secrets come from the `homelab` repo's playbook 59 (Secret `mcp-hub-secrets`).
- `README.md`: status table, quick reference still accurate?
- `CONTRIBUTING.md`: new command, test suite or local-environment step?
- `INTERFACES.md`: new/changed tool, error code, validation rule, env var, Secret key, log field, consumed service?
- `DEPLOYMENT.md`: new env var or Secret key (and the owner action to create it), resource change, new outbound destination, rollback note?
- `CHANGELOG.md`: entry under `[Unreleased]`?
- Contract drift: does the change require an edit to `docs/080-mcp-hub.md` in the `homelab` repo?
