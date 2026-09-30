import json
from pathlib import Path

import pytest

import mcp_hub.__main__ as entry


@pytest.mark.parametrize("stage", ["settings", "registry", "app"])
def test_unexpected_startup_error_is_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], secrets_dir: Path, stage: str
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret-sentinel from a library")

    target = {"settings": "load_settings", "registry": "load_registry", "app": "create_app"}[stage]
    monkeypatch.setattr(entry, target, boom)
    code = entry.main({"HUB_SECRETS_DIR": str(secrets_dir)})
    output = capsys.readouterr()
    assert code == 2
    assert "secret-sentinel" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    events = [json.loads(line) for line in output.out.splitlines()]
    failed = [e for e in events if e["event"] == "startup_failed"]
    assert len(failed) == 1
    assert (failed[0]["reason"], failed[0]["exception"]) == ("unexpected_error", "RuntimeError")
