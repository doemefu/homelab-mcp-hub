"""Prometheus metrics on the internal port (spec 080 §9.7 rev 4.17, D90; homelab#178): a hand-written text exposition,
no metrics library. One `Metrics` per application instance. Label values are registry ids and the fixed enums below
only; every counter series exists from the start at 0, and the status gauge is computed on each scrape."""

import threading
from collections.abc import Iterable
from typing import Final, Literal, get_args

from mcp_hub.errors import ErrorCode

ToolName = Literal[
    "list_accounts",
    "get_events",
    "list_unread",
    "list_messages",
    "get_message",
    "get_message_status",
    "get_thread",
    "search_messages",
]
ToolOutcome = Literal["ok", "partial", "error"] | ErrorCode
# The `check` values of the `token_rejected` (token verifier) and `request` (refused request) log lines.
RejectionCheck = Literal[
    "malformed",
    "algorithm",
    "signature",
    "type",
    "issuer",
    "audience",
    "client",
    "time",
    "required_claims",
    "subject",
    "scope_format",
    "internal",
    "scope",
    "host",
    "origin",
    "body_size",
]
StatusName = Literal["ok", "auth_expired", "unreachable", "error", "unknown", "disabled"]  # health.HealthStatus

TOOL_NAMES: Final[tuple[str, ...]] = get_args(ToolName)
TOOL_OUTCOMES: Final[tuple[str, ...]] = ("ok", "partial", "error", *get_args(ErrorCode))
REJECTION_CHECKS: Final[tuple[str, ...]] = get_args(RejectionCheck)
STATUSES: Final[tuple[str, ...]] = get_args(StatusName)
CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"


def _label(value: str) -> str:
    """A label value escaped for the text format (backslash, double quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Counters of finished tool calls and refused requests. Values outside the enums are never emitted: an unknown
    tool is dropped, an unknown outcome counts as `error`, an unknown check as `internal`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tool_calls = {(tool, outcome): 0 for tool in TOOL_NAMES for outcome in TOOL_OUTCOMES}
        self._rejections = dict.fromkeys(REJECTION_CHECKS, 0)

    def count_tool_call(self, tool: ToolName, outcome: ToolOutcome) -> None:
        if tool not in TOOL_NAMES:
            return
        key = (tool, outcome if outcome in TOOL_OUTCOMES else "error")
        with self._lock:
            self._tool_calls[key] += 1

    def count_rejection(self, check: RejectionCheck) -> None:
        with self._lock:
            self._rejections[check if check in self._rejections else "internal"] += 1

    def render(self, statuses: Iterable[tuple[str, str, str]]) -> str:
        """The exposition: `statuses` holds (account id, capability, current status) per configured capability."""
        with self._lock:
            tool_calls = dict(self._tool_calls)
            rejections = dict(self._rejections)
        lines = [
            "# HELP mcp_hub_account_status Current status per account and capability (1 = current).",
            "# TYPE mcp_hub_account_status gauge",
        ]
        for account, capability, current in sorted(statuses):
            for status in STATUSES:
                labels = f'account="{_label(account)}",capability="{_label(capability)}",status="{status}"'
                lines.append(f"mcp_hub_account_status{{{labels}}} {1 if status == current else 0}")
        lines += [
            "# HELP mcp_hub_tool_calls_total Finished tool calls by tool and outcome.",
            "# TYPE mcp_hub_tool_calls_total counter",
        ]
        lines += [
            f'mcp_hub_tool_calls_total{{tool="{tool}",outcome="{outcome}"}} {count}'
            for (tool, outcome), count in sorted(tool_calls.items())
        ]
        lines += [
            "# HELP mcp_hub_token_rejections_total Refused requests by check.",
            "# TYPE mcp_hub_token_rejections_total counter",
        ]
        lines += [f'mcp_hub_token_rejections_total{{check="{check}"}} {n}' for check, n in sorted(rejections.items())]
        return "\n".join(lines) + "\n"
