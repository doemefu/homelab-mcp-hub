"""Environment configuration (spec 080 §8.1)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALLOWLIST_FILE: Final = "allowed-subjects"
REGISTRY_FILE: Final = "accounts.json"
_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_DEFAULTS: Final[dict[str, str]] = {
    "HUB_PUBLIC_HOST": "mcp.furchert.ch",
    "HUB_RESOURCE": "https://mcp.furchert.ch/mcp",
    "HUB_PORT": "8083",
    "HUB_INTERNAL_PORT": "8084",
    "AUTH_ISSUER": "https://auth.furchert.ch",
    "AUTH_JWKS_URL": "http://auth-service.apps.svc.cluster.local:8080/oauth2/jwks",
    "AUTH_EXPECTED_CLIENT_ID": "claude-mcp-hub",
    "AUTH_CLOCK_SKEW_SECONDS": "60",
    "HUB_SECRETS_DIR": "/etc/mcp-hub/secrets",
    "HUB_DEFAULT_TIMEZONE": "Europe/Zurich",
    "LOG_LEVEL": "INFO",
    "HUB_RESPONSE_BUDGET_CHARS": "30000",
}


class ConfigError(ValueError):
    """Invalid configuration; the message names the variable, never its value."""


@dataclass(frozen=True, slots=True)
class Settings:
    public_host: str
    resource: str
    port: int
    internal_port: int
    auth_issuer: str
    auth_jwks_url: str
    auth_expected_client_id: str
    clock_skew_seconds: int
    secrets_dir: Path
    default_timezone: str
    log_level: str
    response_budget_chars: int

    @property
    def mcp_path(self) -> str:
        return urlsplit(self.resource).path

    @property
    def resource_metadata_path(self) -> str:
        return "/.well-known/oauth-protected-resource" + self.mcp_path

    @property
    def resource_metadata_url(self) -> str:
        parts = urlsplit(self.resource)
        return f"{parts.scheme}://{parts.netloc}{self.resource_metadata_path}"


def load_settings(env: Mapping[str, str]) -> Settings:
    def get(name: str) -> str:
        return env.get(name, "").strip() or _DEFAULTS[name]

    public_host = get("HUB_PUBLIC_HOST")
    resource = get("HUB_RESOURCE")
    parts = urlsplit(resource)
    if parts.scheme != "https" or not parts.netloc or parts.path in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("HUB_RESOURCE must be an https URL with a path and no query")
    if parts.netloc != public_host:
        raise ConfigError("HUB_PUBLIC_HOST must equal the host of HUB_RESOURCE")
    issuer = get("AUTH_ISSUER")
    _require_url("AUTH_ISSUER", issuer, {"https"})
    jwks_url = get("AUTH_JWKS_URL")
    _require_url("AUTH_JWKS_URL", jwks_url, {"http", "https"})
    port = _int("HUB_PORT", get("HUB_PORT"), 1, 65535)
    internal_port = _int("HUB_INTERNAL_PORT", get("HUB_INTERNAL_PORT"), 1, 65535)
    if port == internal_port:
        raise ConfigError("HUB_INTERNAL_PORT must differ from HUB_PORT")
    timezone = get("HUB_DEFAULT_TIMEZONE")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("HUB_DEFAULT_TIMEZONE must be an IANA zone name") from exc
    log_level = get("LOG_LEVEL").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR")
    return Settings(
        public_host=public_host,
        resource=resource,
        port=port,
        internal_port=internal_port,
        auth_issuer=issuer,
        auth_jwks_url=jwks_url,
        auth_expected_client_id=get("AUTH_EXPECTED_CLIENT_ID"),
        clock_skew_seconds=_int("AUTH_CLOCK_SKEW_SECONDS", get("AUTH_CLOCK_SKEW_SECONDS"), 0, 300),
        secrets_dir=Path(get("HUB_SECRETS_DIR")),
        default_timezone=timezone,
        log_level=log_level,
        # Measured on the compact JSON of content[0].text (spec 080 rev. 4.4 §5.4, D59).
        response_budget_chars=_int("HUB_RESPONSE_BUDGET_CHARS", get("HUB_RESPONSE_BUDGET_CHARS"), 10000, 70000),
    )


def _require_url(name: str, value: str, schemes: set[str]) -> None:
    parts = urlsplit(value)
    if parts.scheme not in schemes or not parts.netloc:
        raise ConfigError(f"{name} must be a URL with scheme {'/'.join(sorted(schemes))}")


def _int(name: str, value: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number
