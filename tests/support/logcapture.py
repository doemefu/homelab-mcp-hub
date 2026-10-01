"""Log capture for provider tests (same pattern as tests/contract/test_logging.py, which stays unchanged)."""

import logging

from mcp_hub.logging import JsonFormatter


class Capture(logging.Handler):
    """Formatted lines plus raw records, so third-party records are visible even though the formatter masks them."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []
        self.raw: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        self.raw.append((record.name, record.levelno, record.getMessage()))
