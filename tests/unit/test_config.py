from pathlib import Path

import pytest

from mcp_hub.config import ConfigError, load_settings


def test_defaults_match_spec_8_1() -> None:
    s = load_settings({})
    assert s.public_host == "mcp.furchert.ch"
    assert s.resource == "https://mcp.furchert.ch/mcp"
    assert (s.port, s.internal_port) == (8083, 8084)
    assert s.auth_issuer == "https://auth.furchert.ch"
    assert s.auth_jwks_url == "http://auth-service.apps.svc.cluster.local:8080/oauth2/jwks"
    assert s.auth_expected_client_id == "claude-mcp-hub"
    assert s.clock_skew_seconds == 60
    assert s.secrets_dir == Path("/etc/mcp-hub/secrets")
    assert s.default_timezone == "Europe/Zurich"
    assert s.log_level == "INFO"
    assert s.mcp_path == "/mcp"
    assert s.resource_metadata_url == "https://mcp.furchert.ch/.well-known/oauth-protected-resource/mcp"
    assert s.resource_metadata_path == "/.well-known/oauth-protected-resource/mcp"


def test_empty_values_fall_back_to_defaults() -> None:
    assert load_settings({"HUB_PORT": " "}).port == 8083


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"HUB_RESOURCE": "http://mcp.furchert.ch/mcp"}, "HUB_RESOURCE"),
        ({"HUB_RESOURCE": "https://mcp.furchert.ch"}, "HUB_RESOURCE"),
        ({"HUB_RESOURCE": "https://mcp.furchert.ch/mcp?x=1"}, "HUB_RESOURCE"),
        ({"HUB_PUBLIC_HOST": "other.example.org"}, "HUB_PUBLIC_HOST"),
        ({"AUTH_ISSUER": "auth.furchert.ch"}, "AUTH_ISSUER"),
        ({"AUTH_JWKS_URL": "ftp://x/jwks"}, "AUTH_JWKS_URL"),
        ({"HUB_PORT": "abc"}, "HUB_PORT"),
        ({"HUB_PORT": "70000"}, "HUB_PORT"),
        ({"HUB_INTERNAL_PORT": "8083"}, "HUB_INTERNAL_PORT"),
        ({"AUTH_CLOCK_SKEW_SECONDS": "301"}, "AUTH_CLOCK_SKEW_SECONDS"),
        ({"HUB_DEFAULT_TIMEZONE": "Mars/Base"}, "HUB_DEFAULT_TIMEZONE"),
        ({"LOG_LEVEL": "TRACE"}, "LOG_LEVEL"),
    ],
)
def test_invalid_values_name_the_variable(env: dict[str, str], name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(env)


def test_log_level_is_case_insensitive() -> None:
    assert load_settings({"LOG_LEVEL": "debug"}).log_level == "DEBUG"


def test_response_budget_default_and_override() -> None:
    assert load_settings({}).response_budget_chars == 30000
    assert load_settings({"HUB_RESPONSE_BUDGET_CHARS": "12000"}).response_budget_chars == 12000


@pytest.mark.parametrize("value", ["9999", "70001", "abc"])
def test_response_budget_out_of_range_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="HUB_RESPONSE_BUDGET_CHARS"):
        load_settings({"HUB_RESPONSE_BUDGET_CHARS": value})


def test_health_check_interval_default_and_override() -> None:
    assert load_settings({}).health_check_interval_seconds == 1800
    assert load_settings({"HUB_HEALTH_CHECK_INTERVAL_SECONDS": "600"}).health_check_interval_seconds == 600


@pytest.mark.parametrize("value", ["59", "86401", "x"])
def test_health_check_interval_out_of_range_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError, match="HUB_HEALTH_CHECK_INTERVAL_SECONDS"):
        load_settings({"HUB_HEALTH_CHECK_INTERVAL_SECONDS": value})
