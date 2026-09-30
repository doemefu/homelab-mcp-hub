"""JSON logging with third-party loggers capped (spec 080 §9.7)."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

HUB_LOGGER: Final = "mcp_hub"
PINNED_WARNING: Final = ("httpx2", "httpcore2", "mcp", "caldav", "niquests", "imapclient", "uvicorn")
# Loggers whose WARNING lines contain request data; the hub logs its own check name instead.
PINNED_ERROR: Final = ("mcp.server.transport_security",)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; exceptions are reduced to their class name (no message, no traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage() if record.name.startswith(HUB_LOGGER) else "third_party_log",
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            line.update(fields)
        if record.exc_info and record.exc_info[0] is not None:
            line["exception"] = record.exc_info[0].__name__
        return json.dumps(line, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # Python warnings (e.g. SDK deprecations) would otherwise reach stderr as plain text (review 09 W9).
    logging.captureWarnings(True)
    apply_logger_levels(level)


def apply_logger_levels(level: str) -> None:
    root = logging.getLogger()
    # MCPServer() calls logging.basicConfig(); keep exactly our handler.
    for handler in list(root.handlers):
        if not isinstance(handler.formatter, JsonFormatter):
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger(HUB_LOGGER).setLevel(level)
    for name in PINNED_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in PINNED_ERROR:
        logging.getLogger(name).setLevel(logging.ERROR)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    logger.log(level, event, extra={"fields": fields})
