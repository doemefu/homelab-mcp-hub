# Review Guidelines

- Never log secrets, tokens, `Authorization` headers, header values, provider URLs, addresses or content (spec 080 §9.7); a log-capture test covers every new code path that talks to a provider.
- Every change to `auth.py`, `jwks.py`, `app.py` or the SDK version keeps all `tests/contract` (gate G6) green; a new rule gets a new contract test row.
- Exact pins only (`==`), `uv.lock` updated in the same PR; actions by SHA, images by digest; new packages need owner approval (spec 080 §9.3).
- The hub stays read-only: no tool writes to a provider; every tool declares `readOnlyHint: true`, `destructiveHint: false`.
- Every third-party string goes into an `untrusted` object (spec 080 §5.1, §5.3).
- Keep diffs minimal; no unrelated refactors. Tests required (unit + contract/integration).
