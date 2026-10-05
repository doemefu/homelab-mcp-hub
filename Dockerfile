# syntax=docker/dockerfile:1
# Base images are pinned by tag + multi-arch index digest; Dependabot's docker ecosystem bumps both.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY src ./src
RUN /app/.venv/bin/python -m compileall -q /app/src

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
# Root-owned, read-only for the runtime user; nothing is written at runtime (readOnlyRootFilesystem).
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY --chmod=0755 bin/mcp-hub /usr/local/bin/mcp-hub
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001
EXPOSE 8083 8084
CMD ["python", "-m", "mcp_hub"]
