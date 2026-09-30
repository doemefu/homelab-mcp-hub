# Spec 080 rev. 4.3 §9.7 allowed fields; `reason` only on `startup_failed`.
ALLOWED_FIELDS = frozenset(
    {
        "ts",
        "level",
        "logger",
        "event",
        "method",
        "route",
        "status",
        "duration_ms",
        "mcp_protocol_version",
        "sub",
        "client_id",
        "jti",
        "check",
        "exception",
        "tool",
        "outcome",
        "accounts",
        "result_count",
        "key_count",
        "account",
        "capability",
        "key",
    }
)
EVENT_FIELDS: dict[str, frozenset[str]] = {"startup_failed": frozenset({"reason"})}


def allowed_fields(event: str) -> frozenset[str]:
    return ALLOWED_FIELDS | EVENT_FIELDS.get(event, frozenset())
