import json
import logging
import threading
from pathlib import Path

import pytest

from mcp_hub.own_addresses import (
    MAX_FILE_BYTES,
    OWN_ADDRESSES,
    OwnAddressReader,
    own_address_count,
    parse_own_addresses,
)

FIXED_TEXTS = {
    "the file holds no address",
    "the file holds more than 20 addresses",
    "the file is larger than 8 KiB",
    "an entry is longer than 254 characters",
    "an entry is not a plain address",
    "the file holds a duplicate",
    "the file is not UTF-8",
}


def test_parse_valid_file() -> None:
    assert parse_own_addresses("Me@Example.org\n\n alias@example.org \r\n") == frozenset(
        {"me@example.org", "alias@example.org"}
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n \n",
        "".join(f"a{i}@example.org\n" for i in range(21)),
        "a" * 50 + "@" + ".".join(["b" * 60] * 4) + ".org",
        "a" * 65 + "@example.org",
        "a b@example.org",
        "a@example",
        "a@exa_mple.org",
        '"a"@example.org',
        "a@b@example.org",
        "a@example.org\nA@Example.org",
        "a@example.org\n" + "\n" * (MAX_FILE_BYTES + 10),
    ],
)
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError, match=r"^[^@]+$") as info:
        parse_own_addresses(text)
    assert str(info.value) in FIXED_TEXTS
    assert "@" not in str(info.value)


def test_twenty_entries_are_accepted() -> None:
    assert len(parse_own_addresses("".join(f"a{i}@example.org\n" for i in range(20)))) == 20


def logged(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "mcp_hub.own_addresses"]


def test_missing_file_falls_back_and_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    reader = OwnAddressReader()
    caplog.set_level(logging.INFO)
    for _ in range(3):
        assert reader.read("icloud", tmp_path, "icloud-own-addresses") == frozenset()
    records = logged(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == "registry_value_invalid"
    assert records[0].fields == {"account": "icloud", "key": "icloud-own-addresses"}  # type: ignore[attr-defined]
    assert "@" not in caplog.text + json.dumps(records[0].fields)  # type: ignore[attr-defined]


def test_no_ref_reads_nothing_and_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert OwnAddressReader().read("icloud", tmp_path, None) == frozenset()
    assert logged(caplog) == []


def test_valid_file_is_returned_without_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "own").write_text("Me@Example.org\n")
    caplog.set_level(logging.INFO)
    assert OwnAddressReader().read("icloud", tmp_path, "own") == frozenset({"me@example.org"})
    assert logged(caplog) == []


def test_invalid_and_oversized_and_non_utf8_files_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    reader = OwnAddressReader()
    (tmp_path / "bad").write_text("not an address\n")
    (tmp_path / "big").write_bytes(b"a@example.org\n" + b"\n" * (MAX_FILE_BYTES + 1))
    (tmp_path / "bin").write_bytes(b"\xff\xfe\n")
    for account, ref in (("a1", "bad"), ("a2", "big"), ("a3", "bin")):
        assert reader.read(account, tmp_path, ref) == frozenset()
    assert [r.fields["account"] for r in logged(caplog)] == ["a1", "a2", "a3"]  # type: ignore[attr-defined]


def test_state_changes_warn_again(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    reader = OwnAddressReader()
    path = tmp_path / "own"
    reader.read("icloud", tmp_path, "own")  # missing: warn 1
    path.write_text("a@example.org\n")
    assert reader.read("icloud", tmp_path, "own") == frozenset({"a@example.org"})
    path.write_text("broken\n")
    reader.read("icloud", tmp_path, "own")  # invalid: warn 2
    reader.read("icloud", tmp_path, "own")
    path.unlink()
    reader.read("icloud", tmp_path, "own")  # missing again: warn 3
    assert len(logged(caplog)) == 3


def test_file_is_read_on_every_call(tmp_path: Path) -> None:
    reader = OwnAddressReader()
    (tmp_path / "own").write_text("a@example.org\n")
    assert reader.read("icloud", tmp_path, "own") == frozenset({"a@example.org"})
    (tmp_path / "own").write_text("b@example.org\n")
    assert reader.read("icloud", tmp_path, "own") == frozenset({"b@example.org"})


def test_reset_forgets_state(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    reader = OwnAddressReader()
    reader.read("icloud", tmp_path, "own")
    reader.reset()
    reader.read("icloud", tmp_path, "own")
    assert len(logged(caplog)) == 2


def test_concurrent_reads_warn_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    reader = OwnAddressReader()
    barrier = threading.Barrier(2)

    def work() -> None:
        barrier.wait()
        for _ in range(50):
            reader.read("icloud", tmp_path, "own")

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(logged(caplog)) == 1


def test_count_and_invalid(tmp_path: Path) -> None:
    (tmp_path / "ok").write_text("a@example.org\nb@example.org\n")
    (tmp_path / "bad").write_text("x\n")
    assert own_address_count(tmp_path, "ok") == 2
    assert own_address_count(tmp_path, "bad") is None
    assert own_address_count(tmp_path, "absent") is None


def test_module_reader_exists() -> None:
    assert isinstance(OWN_ADDRESSES, OwnAddressReader)
