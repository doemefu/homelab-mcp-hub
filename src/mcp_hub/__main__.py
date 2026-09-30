"""Process entry: one uvicorn server, two sockets (8083 MCP, 8084 health), one worker (spec 080 §9.2, §9.6)."""

import logging
import os
import signal
import socket
import sys
from collections.abc import Mapping

import uvicorn

from mcp_hub.app import create_app
from mcp_hub.config import REGISTRY_FILE, ConfigError, load_settings
from mcp_hub.logging import configure_logging, log_event
from mcp_hub.registry import RegistryError, load_registry

_log = logging.getLogger("mcp_hub.main")


def _listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))  # noqa: S104 - container port; the tunnel routes only 8083
    return sock


def _exit_cleanly(signum: int, frame: object) -> None:
    # uvicorn shuts down gracefully, restores this handler and re-raises the signal it caught: exit 0, no traceback.
    raise SystemExit(0)


def main(env: Mapping[str, str] | None = None) -> int:
    configure_logging("INFO")
    try:
        settings = load_settings(os.environ if env is None else env)
        configure_logging(settings.log_level)
        registry = load_registry(settings.secrets_dir / REGISTRY_FILE)
    except (ConfigError, RegistryError) as exc:
        log_event(_log, logging.ERROR, "startup_failed", reason=str(exc))
        return 2
    try:
        hub = create_app(settings, registry)
        config = uvicorn.Config(
            hub.asgi,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            lifespan="on",
            timeout_graceful_shutdown=10,
        )
        sockets = [_listen(settings.port), _listen(settings.internal_port)]
        log_event(_log, logging.INFO, "startup", accounts=[a.id for a in registry.accounts])  # §9.7 fields only
        server = uvicorn.Server(config)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _exit_cleanly)
        server.run(sockets=sockets)
    except Exception as exc:  # third-party message text is uncontrolled: log the class name only
        log_event(_log, logging.ERROR, "startup_failed", exception=type(exc).__name__)
        return 2
    if not server.started:  # uvicorn returns normally when lifespan startup fails
        log_event(_log, logging.ERROR, "startup_failed", reason="lifespan startup failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
