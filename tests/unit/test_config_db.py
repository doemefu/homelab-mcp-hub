import pytest

from mcp_hub.config import ConfigError, load_settings


def test_db_defaults_follow_spec() -> None:
    s = load_settings({})
    assert (s.db_host, s.db_port, s.db_name) == ("postgresql.apps.svc.cluster.local", 5432, "mcp_hub")


@pytest.mark.parametrize(
    ("name", "value"),
    [("DB_PORT", "0"), ("DB_PORT", "abc"), ("DB_HOST", "bad host"), ("DB_NAME", "Mcp-Hub")],
)
def test_invalid_values_refuse_start(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({name: value})
    assert str(info.value).startswith(name)


def test_key_id_variables_do_not_exist() -> None:
    s = load_settings({"TOKEN_ENCRYPTION_KEY_ID": "k9"})
    assert not hasattr(s, "token_key_id")
