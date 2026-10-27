"""The scenario builders of scripts/memory_probe.py for search_messages (WP13d, T8): the fakes are what the memory gate
measures, so their shape is pinned (hit counts, hostile sizes, cap and window)."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import httpx2
import pytest

from mcp_hub.providers.graph import MAX_LIST_RESPONSE_BYTES
from mcp_hub.providers.imap_session import MAX_SEARCH_CANDIDATES


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "memory_probe.py"
    spec = importlib.util.spec_from_file_location("memory_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load()


def test_search_answers_one_hit_above_the_cap_in_every_searched_folder(probe: ModuleType) -> None:
    server = probe.search_server()
    client = server.client()
    for name in ("INBOX", "Sent Messages", "Archive"):
        client.select_folder(name, readonly=True)
        assert len(client.search(b"NOT DRAFT TEXT alpha")) == probe.SEARCH_HITS == MAX_SEARCH_CANDIDATES + 1
        literal = client._raw_command_untagged(b"SEARCH", [b"CHARSET", b"UTF-8", b"TEXT", "größe".encode()], uid=True)
        assert len(literal[0].split()) == probe.SEARCH_HITS


def test_search_query_has_three_words_one_non_ascii(probe: ModuleType) -> None:
    words = probe.SEARCH_QUERY.split(" ")
    assert len(words) == 3
    assert [w.isascii() for w in words] == [True, True, False]


def test_search_candidate_rows_are_hostile_and_the_chosen_items_heavy(probe: ModuleType) -> None:
    inbox = probe.search_messages_of(True)
    bulk = probe.search_messages_of(False)
    assert len(inbox) == len(bulk) == probe.SEARCH_HITS
    assert max(len(m.headers or b"") for m in bulk.values()) > 8192  # cut by the candidate FETCH
    heavy = [m for m in inbox.values() if len(m.headers or b"") > 64 * 1024]
    assert len(heavy) == probe.SEARCH_CHOSEN
    assert all(len(m.text) >= 4096 for m in heavy)
    newest_real = min(m.internaldate for m in heavy if m.internaldate is not None)
    assert newest_real > max(m.internaldate for m in bulk.values() if m.internaldate is not None)
    assert newest_real > datetime.now(UTC) - timedelta(days=30)


def test_search_fetch_builds_a_structure_of_the_configured_parts(probe: ModuleType) -> None:
    server = probe.search_server()
    client = server.client()
    client.select_folder("INBOX", readonly=True)
    rows = client.fetch([probe.SEARCH_HITS], ["BODYSTRUCTURE"])
    structure = rows[probe.SEARCH_HITS][b"BODYSTRUCTURE"]
    assert len(structure[0]) == probe.STRUCTURE_PARTS - 1


def _get(probe: ModuleType, transport: httpx2.MockTransport, path: str, **params: str) -> httpx2.Response:
    with httpx2.Client(transport=transport, base_url="https://graph.microsoft.com/v1.0") as client:
        return client.get(path, params=params)


def test_search_graph_refuses_the_first_search_and_answers_the_halved_one_within_the_cap(probe: ModuleType) -> None:
    transport = probe.search_graph_transport()
    first = _get(probe, transport, "/me/messages", **{"$top": "100"})
    assert len(first.content) > MAX_LIST_RESPONSE_BYTES  # the hub refuses it as too_large
    retry = _get(probe, transport, "/me/messages", **{"$top": "50"})
    assert 0.9 * MAX_LIST_RESPONSE_BYTES < len(retry.content) <= MAX_LIST_RESPONSE_BYTES
    assert len(json.loads(retry.content)["value"]) >= 50


def test_search_graph_folder_lists_are_full_and_move_below_the_bound(probe: ModuleType) -> None:
    transport = probe.search_graph_transport()
    folder = probe.SEARCH_GRAPH_IDS["archive"]
    path = f"/me/mailFolders/{folder}/messages"
    first = _get(probe, transport, path, **{"$top": "101"})
    values = json.loads(first.content)["value"]
    assert len(values) == probe.SEARCH_GRAPH_ITEMS
    assert 0.9 * MAX_LIST_RESPONSE_BYTES < len(first.content) <= MAX_LIST_RESPONSE_BYTES
    assert {v["parentFolderId"] for v in values} == {folder}
    frontier = min(v["receivedDateTime"] for v in values)
    second = _get(probe, transport, path, **{"$top": "101", "$filter": f"receivedDateTime lt {frontier}"})
    assert max(v["receivedDateTime"] for v in json.loads(second.content)["value"]) < frontier


def test_search_graph_answers_the_three_folders_of_all_and_no_other(probe: ModuleType) -> None:
    transport = probe.search_graph_transport()
    found = {name: _get(probe, transport, f"/me/mailFolders/{name}").status_code for name in probe.GRAPH_WELL_KNOWN}
    assert {n for n, code in found.items() if code == 200} == {"inbox", "sentitems", "archive"}
