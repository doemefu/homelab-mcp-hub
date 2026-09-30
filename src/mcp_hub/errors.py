"""Tool error codes and results (spec 080 §5.1)."""

import json
from typing import Literal

from mcp.types import CallToolResult, TextContent

ErrorCode = Literal[
    "invalid_argument",
    "invalid_cursor",
    "unknown_account",
    "capability_unavailable",
    "not_found",
    "auth_expired",
    "unreachable",
    "upstream_timeout",
    "upstream_error",
    "too_large",
]


class ToolError(Exception):
    """A whole tool call cannot run; the message is self-authored and never contains content or credentials."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message

    def to_result(self) -> CallToolResult:
        body = json.dumps({"code": self.code, "message": self.message})
        return CallToolResult(content=[TextContent(type="text", text=body)], is_error=True)
