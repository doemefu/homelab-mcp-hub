"""Sec13: only `providers/imap_guard.py` may touch the raw IMAP client's write or raw-command methods."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_hub"
GUARD = SRC / "providers" / "imap_guard.py"
RAW_OR_WRITE = re.compile(r"\b(select_folder|add_flags|expunge|delete_messages|_raw_command\w*|gmail_search)\b")
# The bare words append, copy and move collide with list.append and model_copy (conflict C6): attribute calls on a
# name containing "client" only.
CLIENT_WRITE = re.compile(r"\b\w*client\w*\.(move|copy|append)\(")


def test_patterns_catch_what_they_should() -> None:
    assert RAW_OR_WRITE.search("client.select_folder(name, readonly=True)")
    assert RAW_OR_WRITE.search("self._client._raw_command_untagged(b'X')")
    assert CLIENT_WRITE.search("self._client.append(folder, message)")
    assert CLIENT_WRITE.search("raw_client.move([1], 'Trash')")
    assert not CLIENT_WRITE.search("items.append(row)")
    assert not CLIENT_WRITE.search("result.model_copy(update={})")


def test_no_write_or_raw_calls_outside_the_guard() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if path != GUARD
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if RAW_OR_WRITE.search(line) or CLIENT_WRITE.search(line)
    ]
    assert offenders == []
