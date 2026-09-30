# Repository Commands

Toolchain: local uv (0.12.17 at bootstrap; CI and image pin 0.12.19), Python 3.13 per `.python-version`. Agents source `.local/env.sh` first (worktree-local venv, uv cache and TMPDIR; no global installs — see CONTRIBUTING.md "Local environment").

## Build, test, run
```bash
uv sync --locked                          # create .venv from uv.lock (fails if the lock is stale)
uv run pytest                             # all tests (unit, contract, integration)
uv run pytest tests/contract -v           # gate G6 contract tests only
uv run pytest tests/unit/test_auth.py::test_name -v
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
PYTHONPATH=src uv run python -m mcp_hub  # run locally; needs HUB_SECRETS_DIR with accounts.json + allowed-subjects
docker build -t mcp-hub:dev .             # container image
scripts/smoke_image.sh mcp-hub:dev        # local image check (read-only FS, uid 10001, 401/200/403/421)
```

## Dependencies
```bash
uv lock                                   # after editing pyproject.toml (exact == pins only)
uv lock --upgrade-package <name>          # bump one package (owner approval for new packages)
```

## Checks
```bash
actionlint                                # GitHub workflow lint
kubectl kustomize k8s                     # render manifests (offline)
# Policy path relative to a clone at homelab/mcp-hub; from a worktree under .claude/worktrees/<name>/
# use ../../../../infrastructure/policy/kubernetes/ instead.
conftest test --rego-version v0 --policy ../infrastructure/policy/kubernetes/ --all-namespaces <(kubectl kustomize k8s)
```
