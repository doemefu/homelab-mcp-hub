import io
import json
import logging
import shutil
from pathlib import Path

import httpx2
import pytest

from mcp_hub import cli
from tests.support.logcapture import Capture
from tests.support.ms_transport import FAKE_CLIENT_ID, MsTransport, json_answer
from tests.support.token_fakes import FakeStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
DEVICE = {
    "device_code": "DC-SENTINEL",
    "user_code": "WXYZ-1234",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 1,
}
TOKENS = {"access_token": "AT-SENTINEL", "refresh_token": "RT-SENTINEL", "expires_in": 3600, "scope": "Mail.Read"}


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    shutil.copy(FIXTURES / "accounts.example.json", tmp_path / "accounts.json")
    (tmp_path / "allowed-subjects").write_text("someone\n")
    (tmp_path / "outlook-ms-client-id").write_text(FAKE_CLIENT_ID + "\n")
    return tmp_path


def run(
    secrets: Path, t: MsTransport, argv: list[str], store: FakeStore | None = None, out: io.StringIO | None = None
) -> tuple[int, str, str, FakeStore]:
    terminal, err = out if out is not None else Terminal(), io.StringIO()
    holder = store or FakeStore(None)
    code = cli.main(
        argv,
        {"HUB_SECRETS_DIR": str(secrets)},
        out=terminal,
        err=err,
        client_factory=lambda timeout: httpx2.Client(transport=t.transport(), trust_env=False, follow_redirects=False),
        store_factory=lambda config: holder,
        sleep=lambda s: None,
        clock=lambda: 0.0,
    )
    return code, terminal.getvalue(), err.getvalue(), holder


def happy() -> MsTransport:
    return MsTransport(
        {
            "/consumers/oauth2/v2.0/devicecode": [json_answer(200, DEVICE)],
            "/consumers/oauth2/v2.0/token": [
                json_answer(400, {"error": "authorization_pending"}),
                json_answer(200, TOKENS),
            ],
        }
    )


def test_login_stores_encrypted_token_and_requests_registry_scopes(secrets: Path) -> None:
    t = happy()
    code, out, _, store = run(secrets, t, ["login", "outlook"])
    assert code == 0
    assert store.current() == "RT-SENTINEL"
    assert t.seen[0][4]["scope"] == "Mail.Read offline_access"
    assert "WXYZ-1234" in out
    assert "https://microsoft.com/devicelogin" in out


def test_login_prints_code_only_to_terminal(secrets: Path) -> None:
    capture = Capture()  # on the hub logger: cli.main calls configure_logging, which replaces the root handlers
    logging.getLogger("mcp_hub").addHandler(capture)
    try:
        code, out, err, _ = run(secrets, happy(), ["login", "outlook"])
    finally:
        logging.getLogger("mcp_hub").removeHandler(capture)
    logs = "\n".join(capture.lines + [m for _, _, m in capture.raw])
    for secret in ("WXYZ-1234", "DC-SENTINEL", "AT-SENTINEL", "RT-SENTINEL", FAKE_CLIENT_ID):
        assert secret not in logs
        assert secret not in err
    for secret in ("DC-SENTINEL", "AT-SENTINEL", "RT-SENTINEL", FAKE_CLIENT_ID):
        assert secret not in out
    assert code == 0
    assert '"event": "login"' in logs
    assert '"outcome": "ok"' in logs


def test_login_refuses_without_a_terminal(secrets: Path) -> None:
    t = happy()
    code, out, err, store = run(secrets, t, ["login", "outlook"], out=io.StringIO())
    assert code == 2
    assert t.seen == []
    assert store.sealed is None
    assert "terminal" in err
    assert out == ""


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], cli.USAGE),
        (["login"], cli.USAGE),
        (["login", "nope"], "No account with that id."),
        (["login", "icloud"], "That account does not use Microsoft Graph."),
        (["serve"], cli.USAGE),
    ],
)
def test_usage_and_config_errors_exit_2(secrets: Path, argv: list[str], message: str) -> None:
    t = happy()
    code, _, err, _ = run(secrets, t, argv)
    assert (code, err) == (2, message + "\n")
    assert t.seen == []


def test_token_store_not_configured_exits_2(secrets: Path) -> None:
    code, _, err, _ = run(secrets, happy(), ["login", "outlook"], FakeStore(None, configured=False))
    assert code == 2
    assert "token store is not configured" in err


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        ("authorization_declined", "declined"),
        ("access_denied", "declined"),
        ("expired_token", "expired"),
        ("invalid_client", "refused"),
    ],
)
def test_failed_login_exits_1_and_stores_nothing(secrets: Path, error: str, outcome: str) -> None:
    t = MsTransport(
        {
            "/consumers/oauth2/v2.0/devicecode": [json_answer(200, DEVICE)],
            "/consumers/oauth2/v2.0/token": [json_answer(400, {"error": error})],
        }
    )
    store = FakeStore(None)
    capture = Capture()
    logging.getLogger("mcp_hub").addHandler(capture)
    try:
        code, _, err, _ = run(secrets, t, ["login", "outlook"], store)
    finally:
        logging.getLogger("mcp_hub").removeHandler(capture)
    events = [json.loads(line) for line in capture.lines if '"login"' in line]
    assert code == 1
    assert err == cli._MESSAGES[outcome] + "\n"
    assert [e["outcome"] for e in events] == [outcome]
    assert store.sealed is None


def test_unexpected_host_prints_only_the_host(secrets: Path) -> None:
    bad = DEVICE | {"verification_uri": "https://evil.example.test/x?code=WXYZ-1234"}
    t = MsTransport({"/consumers/oauth2/v2.0/devicecode": [json_answer(200, bad)]})
    code, out, err, _ = run(secrets, t, ["login", "outlook"])
    assert code == 1
    assert err == cli._MESSAGES["unexpected_host"] + " Refused host: evil.example.test\n"
    assert "WXYZ" not in out + err
    assert "code=" not in err


def test_store_failure_after_sign_in_exits_1(secrets: Path) -> None:
    code, _, err, store = run(secrets, happy(), ["login", "outlook"], FakeStore(None, fail_commit=True))
    assert code == 1
    assert "could not be stored" in err
    assert store.sealed is None


def login_lines(secrets: Path, t: MsTransport) -> tuple[int, str, list[dict[str, object]]]:
    capture = Capture()
    logging.getLogger("mcp_hub").addHandler(capture)
    try:
        code, _, err, _ = run(secrets, t, ["login", "outlook"])
    finally:
        logging.getLogger("mcp_hub").removeHandler(capture)
    return code, err, [json.loads(line) for line in capture.lines if '"login"' in line]


def test_login_failure_logs_its_cause(secrets: Path) -> None:
    t = MsTransport(
        {
            "/consumers/oauth2/v2.0/devicecode": [json_answer(200, DEVICE)],
            "/consumers/oauth2/v2.0/token": [httpx2.Response(200, content=b"not json")],
        }
    )
    code, _, events = login_lines(secrets, t)
    assert code == 1
    assert [(e["outcome"], e.get("exception")) for e in events] == [("error", "MalformedJson")]


def test_unreachable_token_endpoint_is_logged_as_error_with_its_cause(secrets: Path) -> None:
    t = MsTransport(
        {
            "/consumers/oauth2/v2.0/devicecode": [json_answer(200, DEVICE | {"expires_in": 60})],
            "/consumers/oauth2/v2.0/token": [json_answer(503, {})],
        }
    )
    capture = Capture()
    logging.getLogger("mcp_hub").addHandler(capture)
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    try:
        err = io.StringIO()
        code = cli.main(
            ["login", "outlook"],
            {"HUB_SECRETS_DIR": str(secrets)},
            out=Terminal(),
            err=err,
            client_factory=lambda timeout: httpx2.Client(transport=t.transport(), trust_env=False),
            store_factory=lambda config: FakeStore(None),
            sleep=sleep,
            clock=lambda: now[0],
        )
    finally:
        logging.getLogger("mcp_hub").removeHandler(capture)
    events = [json.loads(line) for line in capture.lines if '"login"' in line]
    assert code == 1
    assert err.getvalue() == cli._MESSAGES["unreachable"] + "\n"
    assert "could not be reached" in err.getvalue()
    # Spec §9.7 keeps the login outcomes fixed: the internal reason "unreachable" is logged as outcome=error.
    assert [(e["outcome"], e.get("exception")) for e in events] == [("error", "TokenEndpointStatus")]


def test_login_failure_without_a_cause_logs_no_exception_field(secrets: Path) -> None:
    t = MsTransport(
        {
            "/consumers/oauth2/v2.0/devicecode": [json_answer(200, DEVICE)],
            "/consumers/oauth2/v2.0/token": [json_answer(400, {"error": "authorization_declined"})],
        }
    )
    code, err, events = login_lines(secrets, t)
    assert code == 1
    assert err == cli._MESSAGES["declined"] + "\n"
    assert [(e["outcome"], "exception" in e) for e in events] == [("declined", False)]
