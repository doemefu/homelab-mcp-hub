"""IMAP `search_messages` on the list pipeline: value keys, cause -> reason table, cursor keys (spec 080 §5.2, §5.5;
WP13d T4)."""

from collections.abc import Sequence
from datetime import timedelta

import pytest

import mcp_hub.providers.imap as imap
from mcp_hub.cursors import Position
from mcp_hub.providers.base import FolderPage, ListQuery, ProviderError, SearchTerms
from mcp_hub.providers.imap_guard import MAX_EXAMINE_NAME_CHARS
from mcp_hub.providers.imap_session import MAX_SEARCH_CANDIDATES
from tests.support.imap_fake import FakeImapServer, FakeMessage
from tests.unit import test_imap_gmail as g
from tests.unit import test_imap_list_messages as lm

NOW, SINCE, OWN = lm.NOW, lm.SINCE, lm.OWN
BASE = b"NOT DRAFT SINCE 19-Oct-2026"
SENDER = "Alice <alice@example.org>"


def terms(*words: str, sender: str | None = None, subject: str | None = None) -> SearchTerms:
    return SearchTerms(words=words, sender=sender, recipient=None, subject=subject)


def squery(
    search: SearchTerms | None = None,
    folder: str = "inbox",
    *,
    position: Position | None = None,
    unread_only: bool = False,
    direction: str | None = None,
    limit: int = 20,
) -> ListQuery:
    return ListQuery(
        folder,  # type: ignore[arg-type]
        SINCE,
        unread_only,
        direction,  # type: ignore[arg-type]
        limit,
        position,
        search if search is not None else terms("rechnung"),
    )


def hit(uid: int, minutes_ago: float = 5, subject: str = "Rechnung", **h: str) -> FakeMessage:
    return lm.msg(uid, minutes_ago, subject, sender=h.pop("sender", SENDER), **h)


def three(**kw: Sequence[FakeMessage]) -> FakeImapServer:
    return lm.icloud(**kw)


def search_args(server: FakeImapServer) -> list[object]:
    return lm.searches(server)


def refuse_nth(server: FakeImapServer, command: str, n: int) -> None:
    """The n-th `command` (1-based) of the call is answered NO."""
    original = server.run
    count = {"n": 0}

    def run_command(name: str, argument: object = None) -> None:
        if name == command:
            count["n"] += 1
            if count["n"] == n:
                server.fail[command] = server.no(command)
        original(name, argument)

    server.run = run_command  # type: ignore[method-assign]


# --- criteria on the wire -----------------------------------------------------------------------------------------


def test_search_inbox_criteria_bytes() -> None:
    server = three(inbox=[hit(1)])
    page = lm.run(server, squery(terms("rechnung", sender="example.org")))
    assert search_args(server) == [BASE + b' TEXT "rechnung" FROM "example.org"']
    assert [item.ref.uid for item in page.items] == [1]


def test_search_unread_window_keys_come_first() -> None:
    server = three(inbox=[hit(1)])
    lm.run(server, squery(terms("rechnung", subject="oktober"), unread_only=True))
    assert search_args(server) == [b'NOT DRAFT UNSEEN SINCE 19-Oct-2026 TEXT "rechnung" SUBJECT "oktober"']


def test_search_non_ascii_uses_literal_path() -> None:
    server = three(inbox=[hit(1, sender="Müller <m@example.org>")])
    page = lm.run(server, squery(terms("Müller")))
    assert search_args(server) == [[b"CHARSET", b"UTF-8", BASE, b"TEXT", "Müller".encode()]]
    assert [item.ref.uid for item in page.items] == [1]


def test_search_non_ascii_value_with_spaces_stays_one_literal() -> None:
    """T4-I1: a literal with a space is one value, not two tokens."""
    server = three(
        inbox=[lm.msg(1, 5, "Gr\u00fcsse aus Bern", sender=SENDER), lm.msg(2, 6, "Gr\u00fcsse", sender=SENDER)]
    )
    page = lm.run(server, squery(terms("rechnung", subject="Gr\u00fcsse aus")))
    assert search_args(server) == [
        [b"CHARSET", b"UTF-8", BASE, b"TEXT", b'"rechnung"', b"SUBJECT", "Gr\u00fcsse aus".encode()]
    ]
    assert page.complete is True


def test_search_soft_stop_on_the_literal_path() -> None:
    """T4-m3: a stop before the second folder's literal SEARCH keeps the page empty and `time_limit`."""
    _, page = lm.soft_stop_before(three_search, squery(terms("M\u00fcller"), folder="all"), second_search)
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert (page.incomplete, page.refused_only) == ({"time_limit"}, False)


def test_search_without_terms_sends_the_list_criteria() -> None:
    server = three(inbox=[hit(1)])
    lm.run(server, lm.query())
    assert search_args(server) == [BASE]


def test_search_empty_value_reaches_the_guard_and_sends_nothing() -> None:
    """MF-T1-3: the adapter never relies on `SearchTerms` being non-empty; the guard refuses an empty value."""
    bad = object.__new__(SearchTerms)  # the constructor refuses "": hand-built
    for name, value in (("words", ("",)), ("sender", None), ("recipient", None), ("subject", None)):
        object.__setattr__(bad, name, value)
    server = three(inbox=[hit(1)])
    with pytest.raises(ProviderError) as info:
        lm.run(server, squery(bad))
    assert (info.value.code, info.value.cause) == ("upstream_error", "ImapCommandRefused")
    assert "UID SEARCH" not in lm.names(server)


def test_search_all_non_gmail_three_folders_dedup_hits_only() -> None:
    """MF-D6: the inbox copy hits, the sent copy of the same message does not; one item, the inbox copy."""
    mid = "<m1@example.org>"
    inbox_copy = hit(1, message_id=mid)
    sent_copy = lm.msg(21, 5, "Nothing", sender=SENDER, message_id=mid, text=b"unrelated")
    server = three(inbox=[inbox_copy], sent=[sent_copy])
    page = lm.run(server, squery(folder="all"))
    assert [(i.ref.uid, i.meta.folder, i.meta.direction) for i in page.items] == [(1, "inbox", "incoming")]
    assert len(search_args(server)) == 3
    own = lm.run(
        three(inbox=[hit(1, message_id=mid)], sent=[sent_copy]),
        squery(folder="all"),
        own=frozenset({"alice@example.org"}),
    )
    assert [(i.ref.uid, i.meta.folder, i.meta.direction) for i in own.items] == [(1, "inbox", "outgoing")]


def gmail_server() -> FakeImapServer:
    return g.gmail(
        inbox=[g.gm(1, 5, "Rechnung", g.INBOX_L)],
        all_mail=[g.gm(2, 6, "Rechnung", g.INBOX_L), g.gm(3, 7, "Rechnung", g.SENT_L), g.gm(4, 8, "Rechnung")],
        sent=[g.gm(5, 9, "Rechnung", g.SENT_L)],
        trash=[g.gm(6, 10, "Rechnung")],
    )


def glist(folder: str) -> ListQuery:
    return ListQuery(folder, SINCE, False, None, 20)  # type: ignore[arg-type]


@pytest.mark.parametrize("folder", g.FOLDER_VALUES)
def test_search_gmail_all_mail_label_keys(folder: str) -> None:
    """The value keys follow the label keys of the list criteria, unchanged (sent, archive, all, trash, inbox)."""
    plain = gmail_server()
    g.run(plain, glist(folder))
    server = gmail_server()
    g.run(server, ListQuery(folder, SINCE, False, None, 20, None, terms("rechnung", sender="example")))  # type: ignore[arg-type]
    assert search_args(server) == [b'%b TEXT "rechnung" FROM "example"' % a for a in g.searches(plain)]  # type: ignore[misc]


def test_search_gmail_label_keys_literal_bytes() -> None:
    server = gmail_server()
    g.run(server, ListQuery("sent", SINCE, False, None, 20, None, terms("rechnung")))
    assert search_args(server) == [g.SENT_KEYS + b' TEXT "rechnung"']
    server = gmail_server()
    g.run(server, ListQuery("archive", SINCE, False, None, 20, None, terms("rechnung")))
    assert search_args(server) == [g.ARCHIVE_KEYS + b' TEXT "rechnung"']


@pytest.mark.parametrize("folder", ["sent", "archive", "all", "trash"])
def test_search_gmail_without_all(folder: str) -> None:
    plain = g.gmail(with_all=False, inbox=[g.gm(1, 5, "Rechnung", g.INBOX_L)], sent=[g.gm(5, 9, "Rechnung", g.SENT_L)])
    g.run(plain, glist(folder))
    server = g.gmail(with_all=False, inbox=[g.gm(1, 5, "Rechnung", g.INBOX_L)], sent=[g.gm(5, 9, "Rechnung", g.SENT_L)])
    page = g.run(server, ListQuery(folder, SINCE, False, None, 20, None, terms("rechnung")))  # type: ignore[arg-type]
    assert search_args(server) == [b'%b TEXT "rechnung"' % a for a in g.searches(plain)]  # type: ignore[misc]
    assert page.complete is True


def test_search_cursor_page_before_key() -> None:
    """S-a: a plan searched alone gets `BEFORE`; the merged `all` searches the whole window again (no `BEFORE`)."""
    position = Position(int((NOW - timedelta(minutes=5)).timestamp()), "k" * 16)
    server = three(inbox=[hit(1, 60)])
    lm.run(server, squery(position=position))
    assert search_args(server) == [BASE + b' BEFORE 23-Oct-2026 TEXT "rechnung"']
    merged = three(inbox=[hit(1, 60)])
    lm.run(merged, squery(folder="all", position=position))
    assert search_args(merged) == [BASE + b' TEXT "rechnung"'] * 3


def test_server_word_match_hit_is_kept() -> None:
    """The hub does not re-check a server hit: the word is far beyond the snippet, in no header, and kept."""
    server = three(inbox=[lm.msg(1, 5, "Hello", text=b"x" * 6000 + b" rechnung ")])
    server.word_match = True
    page = lm.run(server, squery())
    assert [item.ref.uid for item in page.items] == [1]


# --- the list tools' bytes did not move ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("inbox", [BASE]),
        ("sent", [BASE]),
        ("archive", [BASE]),
        ("trash", [BASE]),
        ("all", [BASE] * 3),
    ],
)
def test_list_messages_wire_unchanged(folder: str, expected: list[bytes]) -> None:
    server = three(inbox=[lm.msg(1, 5, "a")], sent=[lm.msg(21, 6, "b")], archive=[lm.msg(11, 7, "c")])
    lm.run(server, lm.query(folder))
    assert search_args(server) == expected


@pytest.mark.parametrize(
    ("folder", "expected"),
    [("sent", g.SENT_KEYS), ("archive", g.ARCHIVE_KEYS)],
)
def test_list_messages_wire_unchanged_gmail(folder: str, expected: bytes) -> None:
    server = gmail_server()
    g.run(server, glist(folder))
    assert search_args(server) == [expected]


# --- cause -> reason ---------------------------------------------------------------------------------------------


def test_search_badcharset_folder_incomplete() -> None:
    server = three(inbox=[hit(1)], sent=[hit(21, 6)], archive=[hit(11, 7)])
    refuse_nth(server, "UID SEARCH", 1)
    page = lm.run(server, squery(folder="all"))
    assert sorted(item.ref.uid for item in page.items) == [11, 21]  # the other folders were searched
    assert (page.complete, page.truncated) == (False, True)
    assert page.incomplete == {"search_refused"}
    assert page.refused_only is True
    assert len(search_args(server)) == 3


def test_reason_search_no_after_the_literal() -> None:
    """`NO [BADCHARSET]` after a literal: every folder is refused, nothing is an account error."""
    server = three(inbox=[hit(1, sender="Müller <m@example.org>")])
    server.badcharset = True
    page = lm.run(server, squery(terms("Müller"), folder="all"))
    assert (page.items, page.complete, page.truncated) == ([], False, True)
    assert (page.incomplete, page.refused_only) == ({"search_refused"}, True)


def test_reason_examine_no_pass_one() -> None:
    server = three(inbox=[hit(1)])
    server.fail["EXAMINE"] = server.no("EXAMINE")
    page = lm.run(server, squery())
    assert (page.items, page.complete, page.truncated) == ([], False, True)
    assert (page.incomplete, page.refused_only) == ({"search_refused"}, True)


def test_reason_missing_uidvalidity() -> None:
    server = three(inbox=[hit(1)], sent=[hit(2, 6)])
    server.folders[1].uidvalidity = None
    page = lm.run(server, squery(folder="all"))
    assert [item.ref.uid for item in page.items] == [1]
    assert (page.complete, page.truncated) == (False, True)
    assert (page.incomplete, page.refused_only) == ({"search_refused"}, True)


def test_reason_mailbox_name_over_1000() -> None:
    server = three(sent=[hit(5, 30)], sent_name="S" * (MAX_EXAMINE_NAME_CHARS + 1))
    page = lm.run(server, squery(folder="sent"))
    assert "EXAMINE" not in lm.names(server)
    assert (page.items, page.complete, page.truncated) == ([], False, True)
    assert (page.incomplete, page.refused_only) == ({"search_refused"}, True)


def test_search_cap_5000_result_cap() -> None:
    count = MAX_SEARCH_CANDIDATES + 3
    inbox = [
        FakeMessage(u, NOW - timedelta(seconds=count - u + 1), headers=lm.hdr("Rechnung")) for u in range(1, count + 1)
    ]
    page = lm.run(three(inbox=inbox), squery(limit=3))
    assert (page.more, page.truncated, page.complete) == (True, True, True)
    assert (page.incomplete, page.refused_only) == ({"result_cap"}, False)


def three_search() -> FakeImapServer:
    return three(inbox=[hit(1, 5), hit(2, 50)], archive=[hit(11, 10)], sent=[hit(21, 20)])


def second_search(commands: lm.Commands) -> int:
    return lm.nth(commands, lambda name, _: name == "UID SEARCH", 2)


def test_search_soft_stop_between_folders_time_limit() -> None:
    _, page = lm.soft_stop_before(three_search, squery(folder="all"), second_search)
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert (page.incomplete, page.refused_only) == ({"time_limit"}, False)


@pytest.mark.parametrize("command", ["LOGIN", "CAPABILITY"])
def test_reason_soft_stop_at_login(command: str) -> None:
    _, page = lm.soft_stop_before(three_search, squery(folder="all"), lambda c: lm.nth(c, lambda n, _: n == command))
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert page.incomplete == {"time_limit"}


def test_soft_stop_keeps_collected_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `result_cap` folder first, then the soft stop before the next folder: both reasons survive the stop."""
    monkeypatch.setattr(imap, "MAX_SEARCH_CANDIDATES", 1)
    _, page = lm.soft_stop_before(three_search, squery(folder="all"), second_search)
    assert page.incomplete == {"result_cap", "time_limit"}
    assert page.refused_only is False


def test_soft_stop_after_a_refusal_is_not_refused_only() -> None:
    def build() -> FakeImapServer:
        server = three_search()
        server.fail["EXAMINE"] = server.no("EXAMINE")
        return server

    _, page = lm.soft_stop_before(build, squery(folder="all"), second_search)
    assert page.incomplete == {"search_refused", "time_limit"}
    assert page.refused_only is False


def test_reason_soft_stop_in_pass_two() -> None:
    def snippet_fetch(commands: lm.Commands) -> int:
        return lm.nth(commands, lambda n, a: n == "UID FETCH" and "BODY.PEEK[1]<0.4096>" in a[1])  # type: ignore[index]

    _, page = lm.soft_stop_before(lambda: three(inbox=[hit(1)]), squery(), snippet_fetch)
    assert (page.items, page.truncated, page.complete) == ([], True, False)
    assert (page.incomplete, page.refused_only) == ({"time_limit"}, False)


def pass_two_loss(kind: str) -> FolderPage:
    server = three(inbox=[hit(1)], archive=[hit(11, 10)], sent=[hit(21, 20)])
    original = server.run
    count = {"examine": 0}

    def run_command(command: str, argument: object = None) -> None:
        if command == "EXAMINE":
            count["examine"] += 1
            if count["examine"] == 4:  # the first EXAMINE of pass 2 (three folders in pass 1)
                if kind == "examine_no":
                    server.fail["EXAMINE"] = server.no("EXAMINE")
                elif kind == "validity":
                    server.folders[0].uidvalidity = 99
        elif kind == "fetch_no" and command == "UID FETCH" and lm.SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            server.fail["UID FETCH"] = server.no("UID FETCH")
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    return lm.run(server, squery(folder="all"))


@pytest.mark.parametrize("kind", ["examine_no", "validity", "fetch_no"])
def test_pass2_losses_changed_during_call(kind: str) -> None:
    page = pass_two_loss(kind)
    assert (page.complete, page.truncated) == (False, True)
    assert (page.incomplete, page.refused_only) == ({"changed_during_call"}, False)


def test_pass2_row_expunged_changed_during_call() -> None:
    """A chosen UID the summary FETCH does not return (expunged between the passes) is a loss too."""
    server = three(inbox=[hit(1), hit(2, 6)])
    original = server.run

    def run_command(command: str, argument: object = None) -> None:
        if command == "UID FETCH" and lm.SUMMARY_HEADER_ITEM in argument[1]:  # type: ignore[index]
            del server.folders[0].messages[2]
        original(command, argument)

    server.run = run_command  # type: ignore[method-assign]
    page = lm.run(server, squery())
    assert [item.ref.uid for item in page.items] == [1]
    assert page.incomplete == {"changed_during_call"}
    assert page.refused_only is False


def test_list_page_truncated_is_unchanged_by_a_refusal() -> None:
    """The list tools keep their flags: a refused folder is `complete=False` only (reasons are set, unused)."""
    server = three(inbox=[lm.msg(1, 5, "a")])
    server.fail["UID SEARCH"] = server.no("UID SEARCH")
    page = lm.run(server, lm.query())
    assert (page.items, page.complete, page.truncated) == ([], False, False)
    assert page.incomplete == {"search_refused"}


# --- Gmail TEXT allowlist (Q-a): a leading `-` inside a quoted TEXT value negates on Gmail ----------------------


def gmail_inbox(search: SearchTerms) -> tuple[FakeImapServer, FolderPage]:
    server = gmail_server()
    page = g.run(server, ListQuery("inbox", SINCE, False, None, 20, None, search))
    return server, page


GMAIL_INBOX = b"NOT DRAFT SINCE 19-Oct-2026"


def test_gmail_text_word_never_reaches_the_wire_with_a_leading_minus() -> None:
    server, _ = gmail_inbox(terms("-rechnung"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "rechnung"']
    server, _ = gmail_inbox(terms("--rechnung", "-.x"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "rechnung" TEXT "x"']


def test_gmail_text_word_is_sanitised_like_graph() -> None:
    server, _ = gmail_inbox(terms("C++", "Foo.Bar@Example.org"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "c" TEXT "foo.bar@example.org"']


def test_gmail_word_with_two_tokens_is_one_text_value() -> None:
    server, _ = gmail_inbox(terms("foo/bar", "BAR"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "foo bar" TEXT "bar"']


def test_gmail_word_without_a_token_is_dropped() -> None:
    server, _ = gmail_inbox(terms("+++", "rechnung", "-.-"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "rechnung"']


def test_gmail_dropped_words_with_a_filter_search_by_the_filter() -> None:
    server, page = gmail_inbox(SearchTerms(words=("+++",), sender="Alice", recipient=None, subject=None))
    assert search_args(server) == [GMAIL_INBOX + b' FROM "Alice"']
    assert page.complete is True


def test_gmail_umlaut_word_stays_a_literal_after_sanitising() -> None:
    server, _ = gmail_inbox(terms("-M\u00fcller"))
    assert search_args(server) == [[b"CHARSET", b"UTF-8", GMAIL_INBOX, b"TEXT", "m\u00fcller".encode()]]


def test_gmail_sender_recipient_subject_values_stay_as_given() -> None:
    server, _ = gmail_inbox(SearchTerms(words=("-x",), sender="-Alice", recipient="Bob-", subject="-C++"))
    assert search_args(server) == [GMAIL_INBOX + b' TEXT "x" FROM "-Alice" TO "Bob-" SUBJECT "-C++"']


@pytest.mark.parametrize("words", [("+++",), ("-.-", "\u2603")])
def test_gmail_no_token_and_no_filter_refuses_the_folders(words: tuple[str, ...]) -> None:
    server, page = gmail_inbox(terms(*words))
    assert lm.names(server) == ["LOGIN", "CAPABILITY", "LOGOUT"]  # nothing examined or searched
    assert (page.items, page.complete, page.truncated) == ([], False, True)
    assert (page.incomplete, page.refused_only) == ({"search_refused"}, True)


def test_gmail_filter_only_search_has_no_text_key() -> None:
    server, _ = gmail_inbox(SearchTerms(words=(), sender="-Alice", recipient=None, subject=None))
    assert search_args(server) == [GMAIL_INBOX + b' FROM "-Alice"']


def test_non_gmail_text_words_are_unchanged() -> None:
    server = three(inbox=[hit(1)])
    lm.run(server, squery(terms("-Rechnung", "C++")))
    assert search_args(server) == [BASE + b' TEXT "-Rechnung" TEXT "C++"']
    server = three(inbox=[hit(1)])
    lm.run(server, squery(terms("-M\u00fcller")))
    assert search_args(server) == [[b"CHARSET", b"UTF-8", BASE, b"TEXT", "-M\u00fcller".encode()]]
