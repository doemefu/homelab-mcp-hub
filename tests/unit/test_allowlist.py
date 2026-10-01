from pathlib import Path

import pytest

from mcp_hub.auth import SubjectAllowlist
from tests.support.clock import FakeClock


def test_exact_case_sensitive_match(tmp_path: Path) -> None:
    path = tmp_path / "allowed-subjects"
    path.write_text("owner-test\n")
    allowlist = SubjectAllowlist(path, clock=FakeClock())
    assert allowlist.contains("owner-test")
    assert not allowlist.contains("Owner-Test")
    assert not allowlist.contains("owner-tes")
    assert not allowlist.contains("owner-test2")
    assert not allowlist.contains("")


@pytest.mark.parametrize(
    "content",
    ["\ufeffowner-test\r\n", "  owner-test  \n\n", "other\r\nowner-test\r\n", "\nowner-test"],
    ids=["bom-crlf", "spaces-blank-lines", "two-lines-crlf", "leading-blank"],
)
def test_allowlist_line_handling(tmp_path: Path, content: str) -> None:
    path = tmp_path / "allowed-subjects"
    path.write_bytes(content.encode("utf-8"))
    allowlist = SubjectAllowlist(path, clock=FakeClock())
    assert allowlist.contains("owner-test")
    assert not allowlist.contains(" owner-test")


@pytest.mark.parametrize("content", [None, "", "\n  \n"], ids=["missing", "empty", "blank-lines"])
def test_missing_or_empty_file_rejects_everyone(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "allowed-subjects"
    if content is not None:
        path.write_text(content)
    assert not SubjectAllowlist(path, clock=FakeClock()).contains("owner-test")


def test_non_utf8_file_rejects_everyone_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "allowed-subjects"
    path.write_bytes(b"owner-test\xff\n")
    assert not SubjectAllowlist(path, clock=FakeClock()).contains("owner-test")


def test_change_takes_effect_within_60_seconds(tmp_path: Path) -> None:
    path = tmp_path / "allowed-subjects"
    path.write_text("owner-test\n")
    clock = FakeClock()
    allowlist = SubjectAllowlist(path, clock=clock)
    assert allowlist.contains("owner-test")
    path.write_text("")  # kill switch step 1 (spec 080 §4.6 L2)
    clock.advance(59)
    assert allowlist.contains("owner-test")  # not yet re-read
    clock.advance(1)
    assert not allowlist.contains("owner-test")
