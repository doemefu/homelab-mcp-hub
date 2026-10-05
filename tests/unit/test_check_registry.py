import base64
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from mcp_hub import cli
from mcp_hub.tokenstore.crypto import key_id_for
from mcp_hub.tokenstore.store import TokenStoreUnavailableError
from tests.support.token_fakes import FakeStore

FIXTURES = Path(__file__).parent.parent / "fixtures"
SENTINEL = "leak-sentinel@example.test"


class StoreWithKeyId(FakeStore):
    def __init__(self, key_id: str | None, error: Exception | None = None) -> None:
        super().__init__(None)
        self._key_id, self._error = key_id, error
        self.asked: list[str] = []

    def stored_key_id(self, account_id: str) -> str | None:
        self.asked.append(account_id)
        if self._error is not None:
            raise self._error
        return self._key_id


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    shutil.copy(FIXTURES / "accounts.example.json", tmp_path / "accounts.json")
    for ref in (
        "allowed-subjects",
        "icloud-username",
        "icloud-app-password",
        "gmail-username",
        "gmail-app-password",
        "outlook-ms-client-id",
        "db-username",
        "db-password",
    ):
        (tmp_path / ref).write_text("placeholder\n")
    (tmp_path / "token-encryption-key").write_text(base64.b64encode(os.urandom(32)).decode())
    return tmp_path


def run(secrets: Path, argv: list[str], store: FakeStore | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        ["check-registry", *argv],
        {"HUB_SECRETS_DIR": str(secrets)},
        out=out,
        err=err,
        store_factory=lambda config: store or StoreWithKeyId(None),
    )
    return code, out.getvalue(), err.getvalue()


def sha(secrets: Path) -> str:
    return hashlib.sha256((secrets / "accounts.json").read_bytes()).hexdigest()[:12]


def current_key_id(secrets: Path) -> str:
    return key_id_for(base64.b64decode((secrets / "token-encryption-key").read_text()))


def test_valid_registry_passes_and_reports_states(secrets: Path) -> None:
    code, out, err = run(secrets, ["--expect-sha", sha(secrets)], StoreWithKeyId(current_key_id(secrets)))
    assert code == 0
    assert out.splitlines() == [
        f"registry sha={sha(secrets)} projected=unknown",
        "registry ok",
        "account icloud mail imap ok",
        "account icloud calendar caldav ok",
        "account gmail mail imap ok",
        "account outlook mail graph ok",
        "account outlook key ok",
        "account outlook token current",
        "account uzh disabled (not checked)",
    ]
    assert err == ""


def test_output_contract_lines_parse_with_the_runbook_patterns(secrets: Path) -> None:
    """The infrastructure runbook (homelab PR #191) parses these exact shapes."""
    _, out, _ = run(secrets, ["--expect-sha", sha(secrets)], StoreWithKeyId(current_key_id(secrets)))
    lines = out.splitlines()
    assert re.fullmatch(r"registry sha=[0-9a-f]{12} projected=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|unknown)", lines[0])
    assert any(re.fullmatch(r"account [a-z][a-z0-9-]{1,31} key ok", line) for line in lines)
    assert any(
        re.fullmatch(r"account [a-z][a-z0-9-]{1,31} token (current|previous|other|none|unreachable)", line)
        for line in lines
    )


def test_missing_token_row_is_not_an_error(secrets: Path) -> None:
    code, out, _ = run(secrets, [])
    assert code == 0
    assert "account outlook token none" in out


def test_stale_file_fails_with_its_own_message(secrets: Path) -> None:
    store = StoreWithKeyId(None)
    code, out, _ = run(secrets, ["--expect-sha", "000000000000"], store)
    assert code == 1
    assert out.splitlines()[1:] == [
        "registry file is not the expected one yet (kubelet refresh pending): wait a minute and run again"
    ]
    assert store.asked == []


def test_missing_credential_named_by_key_only(secrets: Path) -> None:
    (secrets / "db-password").unlink()
    code, out, _ = run(secrets, [])
    assert code == 1
    assert "account outlook mail graph missing db-password" in out


def test_invalid_registry_prints_paths_and_types_only(secrets: Path) -> None:
    data = json.loads((secrets / "accounts.json").read_text())
    data["accounts"][2]["graph"]["scopes"].append("Mail.Send")
    data["accounts"][2]["label"] = SENTINEL * 4  # too long and contains a sentinel value
    data["accounts"][0][SENTINEL] = 1  # unknown key named with a sentinel
    (secrets / "accounts.json").write_text(json.dumps(data))
    store = StoreWithKeyId(None)
    code, out, err = run(secrets, [], store)
    assert code == 1
    assert "registry error accounts.2" in out
    assert "registry error accounts.0.<unknown key> x1: extra_forbidden" in out
    assert "registry ok" not in out
    assert SENTINEL not in out + err
    assert store.asked == []


def test_unparseable_registry_is_an_error_line(secrets: Path) -> None:
    (secrets / "accounts.json").write_bytes(b'{"version": 1, "accounts": [' + SENTINEL.encode())
    code, out, err = run(secrets, [])
    assert code == 1
    assert "registry error <root>: json_invalid" in out
    assert SENTINEL not in out + err


def test_missing_registry_fails(secrets: Path) -> None:
    (secrets / "accounts.json").unlink()
    code, out, _ = run(secrets, [])
    assert (code, out) == (1, "registry missing\n")


def test_invalid_key_reported_without_bytes(secrets: Path) -> None:
    short = base64.b64encode(b"\x01" * 16).decode()
    (secrets / "token-encryption-key").write_text(short)
    code, out, _ = run(secrets, [])
    key_lines = [line for line in out.splitlines() if line.startswith("account outlook key")]
    assert code == 1
    assert key_lines == ["account outlook key invalid"]  # the key line carries no length or bytes
    assert short not in out
    assert "16" not in out.replace(sha(secrets), "")


def test_previous_and_other_key_states(secrets: Path) -> None:
    previous = os.urandom(32)
    (secrets / "token-encryption-key-previous").write_text(base64.b64encode(previous).decode())
    assert "account outlook token previous" in run(secrets, [], StoreWithKeyId(key_id_for(previous)))[1]
    assert "account outlook token other" in run(secrets, [], StoreWithKeyId("f" * 16))[1]


def test_unreachable_store_fails(secrets: Path) -> None:
    code, out, _ = run(secrets, [], StoreWithKeyId(None, TokenStoreUnavailableError("OperationalError")))
    assert code == 1
    assert "account outlook token unreachable" in out


def test_projection_time_read_from_the_snapshot(secrets: Path) -> None:
    snap = secrets / "..2026_10_02_14_01_22.123456"
    snap.mkdir()
    for f in list(secrets.iterdir()):
        if f.is_file():
            shutil.move(str(f), snap / f.name)
            (secrets / f.name).symlink_to(f"..data/{f.name}")
    (secrets / "..data").symlink_to(snap.name)
    code, out, _ = run(secrets, [])
    assert code == 0
    assert out.splitlines()[0].endswith(" projected=2026-10-02T14:01:22Z")


@pytest.mark.parametrize(
    "argv",
    [["--expect-sha"], ["--expect-sha", "xyz"], ["--expect-sha", "ABCDEF012345"], ["--bogus"], ["a", "b", "c"]],
)
def test_usage_errors(secrets: Path, argv: list[str]) -> None:
    code, out, err = run(secrets, argv)
    assert (code, out) == (2, "")
    assert err == cli.USAGE + "\n"


def test_configuration_error_exits_2(secrets: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["check-registry"], {"HUB_SECRETS_DIR": str(secrets), "DB_PORT": "0"}, out=out, err=err)
    assert (code, out.getvalue(), err.getvalue()) == (2, "", "The hub configuration is invalid.\n")


def test_usage_text_still_names_login() -> None:
    assert cli.USAGE == "usage: mcp-hub login <account-id> | mcp-hub check-registry [--expect-sha <12 hex>]"
    assert "usage: mcp-hub login <account-id>" in cli.USAGE  # scripts/smoke_image.sh greps this


def test_deployment_mounts_the_secret_as_a_whole_volume() -> None:
    """Whole-volume mount is what makes Secret updates (and the ..data snapshot) work (spec 080 §9.6)."""
    manifest = (Path(__file__).parent.parent.parent / "k8s" / "deployment.yaml").read_text()
    assert not re.search(r"^\s*-?\s*subPath\s*:", manifest, flags=re.MULTILINE)
