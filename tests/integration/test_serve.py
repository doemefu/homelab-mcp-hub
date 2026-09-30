import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

from tests.support.logfields import allowed_fields

pytestmark = pytest.mark.integration
SRC = Path(__file__).parents[2] / "src"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start(secrets_dir: Path, port: int, internal: int) -> subprocess.Popen[str]:
    env = os.environ | {
        "PYTHONPATH": str(SRC),
        "HUB_PORT": str(port),
        "HUB_INTERNAL_PORT": str(internal),
        "HUB_SECRETS_DIR": str(secrets_dir),
        "AUTH_JWKS_URL": "http://127.0.0.1:9/unused",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_hub"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def wait_ready(internal: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"http://127.0.0.1:{internal}/readyz", trust_env=False).status_code == 200:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("hub did not become ready")


def test_serves_both_ports_and_shuts_down_cleanly(secrets_dir: Path) -> None:
    port, internal = free_port(), free_port()
    process = start(secrets_dir, port, internal)
    try:
        wait_ready(internal)
        response = httpx2.post(
            f"http://127.0.0.1:{port}/mcp",
            headers={
                "Host": "mcp.furchert.ch",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            trust_env=False,
        )
        assert response.status_code == 401
        assert 'scope="mail:read calendar:read"' in response.headers["www-authenticate"]
        assert httpx2.get(f"http://127.0.0.1:{port}/healthz", trust_env=False).status_code == 404
        assert httpx2.post(f"http://127.0.0.1:{internal}/mcp", trust_env=False).status_code == 404
    finally:
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
    assert process.returncode == 0
    for line in output.splitlines():
        json.loads(line)  # every line is JSON


def test_invalid_registry_exits_2_without_traceback_or_values(secrets_dir: Path) -> None:
    # Invalid values that look like account data: none of them may appear in the log (spec 080 §9.7, reason rule).
    (secrets_dir / "accounts.json").write_text(
        '{"version": 1, "accounts": [{"id": "Sentinel-Acct-7q", "label": "sentinel-label-7q-' + "x" * 80 + '",'
        ' "provider": "sentinel-provider-7q", "enabled": true, "capabilities": {"mail": false, "calendar": false}}]}'
    )
    process = start(secrets_dir, free_port(), free_port())
    output, _ = process.communicate(timeout=15)
    assert process.returncode == 2
    assert "Traceback" not in output
    assert "sentinel" not in output.lower()
    events = [json.loads(line) for line in output.splitlines()]
    failed = [e for e in events if e["event"] == "startup_failed"]
    assert len(failed) == 1
    assert failed[0] == events[-1]
    assert failed[0]["reason"].startswith("accounts.json is invalid: ")
    assert all(set(e) <= allowed_fields(e["event"]) for e in events)


def test_port_in_use_exits_non_zero_with_json_only(secrets_dir: Path) -> None:
    with socket.socket() as taken:  # hold the MCP port so the bind fails
        taken.bind(("0.0.0.0", 0))  # noqa: S104 - same address the hub binds
        taken.listen()
        process = start(secrets_dir, int(taken.getsockname()[1]), free_port())
        output, _ = process.communicate(timeout=15)
    assert process.returncode != 0
    assert "Traceback" not in output
    lines = output.splitlines()
    assert lines
    assert all(json.loads(line) for line in lines)  # JSON only
    assert json.loads(lines[-1])["event"] == "startup_failed"
