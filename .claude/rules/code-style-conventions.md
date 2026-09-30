# Code Style & Conventions (Python)

- Python 3.13, `from __future__` not needed; full type hints; `mypy --strict src` must pass; no `# type: ignore` without an error code and a reason.
- ruff is the formatter and linter (line length 120, rule set in `pyproject.toml`); no other formatter.
- Modules are small and single-purpose (`auth.py` verifier + allowlist, `jwks.py`, `registry.py`, `config.py`, `logging.py`, `tools/<domain>.py`, later `providers/<protocol>.py`).
- Value objects: frozen `@dataclass(slots=True)` for internal state, pydantic `BaseModel` (`extra="forbid"`) for external input (accounts.json) and tool outputs.
- Async first on the request path; blocking I/O goes through `anyio.to_thread.run_sync`. Time sources are injected (`clock: Callable[[], float]`) where tests need to move time.
- Errors: tool-level failures raise `mcp_hub.errors.ToolError(code, message)` with a self-authored message; never put exception text from third-party code into a result or a log line (log `type(exc).__name__` only).
- Logging: `log_event(logger, level, event, **fields)` on the `mcp_hub.*` loggers only; allowed fields per spec 080 §9.7; never tokens, `Authorization`, header values, provider URLs, addresses, content, queries.
- Configuration via env vars (`config.py`) and files in `HUB_SECRETS_DIR`; no hard-coded hostnames except the documented defaults.
- Tests: pytest, plain functions, fixtures in `tests/conftest.py` and `tests/support/`; real RS256 tokens against a JWKS served by pytest-httpserver; no mocking of the SDK's auth middleware.
- No real account data, usernames or addresses in code, tests or fixtures (neutral labels `icloud`, `gmail`, `outlook`, `uzh`; `example.org`).
