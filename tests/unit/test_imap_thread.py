"""IMAP `get_thread`: the source check, the seed, the body pass, the soft stop and the tool errors (spec 080 §5.2
`get_thread`, §5.4, §6.5; WP13c T7, Option 6a, A-sec1, A-td3/3a, P1, P10, IR5, SC1, Q-c)."""

import json
import logging
import re
from collections.abc import Callable, Iterator

import pytest

import mcp_hub.providers.imap_thread as imap_thread
import mcp_hub.providers.threads as threads
from mcp_hub.ids import MessageRef, ThreadKey
from mcp_hub.providers.base import MailMeta, MailSummary, ProviderError, ThreadMember, ThreadPage
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM
from mcp_hub.providers.imap_session import ImapSession
from mcp_hub.providers.threads import ThreadCopy
from tests.support.imap_fake import PLAIN, FakeImapClient, FakeImapServer
from tests.support.imap_threads import (
    GMAIL_ALL,
    GMAIL_TRASH,
    INBOX_L,
    SENT_L,
    THREAD,
    clock_of,
    deadline,
    examined,
    fetches,
    folder,
    gmail,
    gmail_key,
    icloud,
    index_of,
    mailbox,
    mid_ref,
    msg,
    names,
    ref,
    stop_after,
    structure,
    thread,
    with_attachment,
)
from tests.support.logcapture import Capture

OWNER = "Owner <owner@example.org>"
BODY_ITEM = re.compile(r"BODY\.PEEK\[[0-9.]+\]<0\.65536>")
LOCATE_ITEM = re.compile(r"BODY\.PEEK\[HEADER\.FIELDS \((MESSAGE-ID|MESSAGE-ID FROM)\)\]<0\.(2048|8192)>")
PLAIN_ITEMS = {"FLAGS", "INTERNALDATE", "X-GM-LABELS", "X-GM-THRID", "BODYSTRUCTURE", SUMMARY_HEADER_ITEM}


@pytest.fixture
def provider_log() -> Iterator[Capture]:
    """WARNING lines (item_degraded) of the providers; the level is pinned, since another test may have changed the
    hub's."""
    handler = Capture()
    logger = logging.getLogger("mcp_hub.providers")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.WARNING)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def subjects(page: ThreadPage) -> list[str | None]:
    return [member.summary.subject for member in page.members]


def by_subject(page: ThreadPage) -> dict[str | None, ThreadMember]:
    return {member.summary.subject: member for member in page.members}


def refused(run: Callable[[], object]) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        run()
    return caught.value


def two_folders(**extra: object) -> FakeImapServer:
    """A Gmail thread with three members in All Mail and one in Trash (the newest)."""
    return gmail(
        all_mail=[
            msg(1, 60, "one", INBOX_L, thrid=THREAD, message_id="<a@example.test>", **extra),  # type: ignore[arg-type]
            msg(2, 40, "two", SENT_L, thrid=THREAD, sender=OWNER, message_id="<b@example.test>"),
            msg(3, 20, "three", INBOX_L, thrid=THREAD, message_id="<c@example.test>"),
        ],
        trash=[msg(5, 1, "four", thrid=THREAD, message_id="<t@example.test>")],
    )


def hook(server: FakeImapServer, before: Callable[[str, object], None]) -> None:
    """Run `before(command, argument)` ahead of every command (it may raise the command's answer)."""
    original = server.run

    def run(command: str, argument: object = None) -> None:
        before(command, argument)
        original(command, argument)

    server.run = run  # type: ignore[method-assign]


# --- source check ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "key"),
    [
        (icloud, ThreadKey("icloud", "gmail", THREAD)),
        (icloud, ThreadKey("icloud", "graph", "AAQkAGI2")),
        (gmail, ThreadKey("gmail", "headers", "root@example.test")),
        (gmail, ThreadKey("gmail", "subject", "0123456789abcdef0123456789abcdef")),
        (gmail, ThreadKey("gmail", "graph", "AAQkAGI2")),
    ],
)
def test_source_mismatch_before_list_or_examine(make: Callable[[], FakeImapServer], key: ThreadKey) -> None:
    server = make()
    page = thread(server, key=key)
    assert page.outcome == "source_mismatch"
    assert page.members == ()
    assert page.overview is None
    assert names(server) == ["LOGIN", "CAPABILITY", "LOGOUT"]


# --- seed ------------------------------------------------------------------------------------------------------


def test_id_seed_read_in_place() -> None:
    server = icloud(inbox=[msg(1, 30, "seed", message_id="<s@example.test>"), msg(2, 20, "draft", flags=(b"\\Draft",))])
    page = thread(server, seed=mid_ref(server, "INBOX", 1, 7))
    assert subjects(page) == ["seed"]
    assert page.members[0].summary.ref == MessageRef("icloud", "INBOX", 7, 1)
    assert page.members[0].linked_by == "headers"
    assert page.thread == ThreadKey("icloud", "headers", "s@example.test")
    assert page.members[0].body.text == "Hello"
    assert names(server)[:4] == ["LOGIN", "CAPABILITY", "EXAMINE", "UID FETCH"]  # the registry inbox needs no LIST

    for target, cause in (
        (mid_ref(server, "INBOX", 1, 8), "UidValidityChanged"),
        (mid_ref(server, "INBOX", 9, 7), "UnknownUid"),
        (mid_ref(server, "INBOX", 2, 7), "Draft"),
        (mid_ref(server, "Junk", 1, 7), "UnboundFolder"),
    ):
        error = refused(lambda target=target: thread(server, seed=target))  # type: ignore[misc]
        assert (error.code, error.cause) == ("not_found", cause)


def test_gmail_id_seed_reads_thread_in_place() -> None:
    server = two_folders()
    page = thread(server, seed=mid_ref(server, GMAIL_ALL, 2, 11))
    assert subjects(page) == ["one", "two", "three", "four"]
    assert [m.linked_by for m in page.members] == ["server"] * 4
    assert page.thread == gmail_key()
    summary_reads = [uids for uids, items in fetches(server) if SUMMARY_HEADER_ITEM in items]
    assert summary_reads == [[2], [1, 2, 3], [5]]  # in place (for its thread id), then All Mail and Trash


def test_incomplete_ref_is_upstream_timeout() -> None:
    server = icloud(inbox=[msg(1, 30, "seed", message_id="<s@example.test>")])

    def refuse_archive(command: str, argument: object) -> None:
        if command == "EXAMINE" and argument == "Archive":
            raise server.no("EXAMINE")

    hook(server, refuse_archive)
    error = refused(lambda: thread(server, seed=ref("missing@example.test", server)))
    assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")
    error = refused(lambda: thread(server, seed=ref("s@example.test", server)))  # found, but uncertain (S-e)
    assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")


def test_not_found_after_complete_search() -> None:
    server = icloud(inbox=[msg(1, 30, "seed", message_id="<s@example.test>")])
    error = refused(lambda: thread(server, seed=ref("missing@example.test", server)))
    assert (error.code, error.cause) == ("not_found", "NotFound")


def test_seed_in_other_mailbox_kept() -> None:
    """CodeRabbit ruling on #221: the seed is kept before any folder filter, also in an `other` mailbox."""
    server = icloud(other=[folder("Projects", (), [msg(4, 30, "filed", message_id="<p@example.test>")])])
    page = thread(server, seed=ref("p@example.test", server), include_trash=False)
    assert subjects(page) == ["filed"]
    assert page.members[0].summary.meta.folder == "other"
    assert page.members[0].summary.ref == MessageRef("icloud", "Projects", 7, 4)
    assert page.members[0].body.text == "Hello"


def test_ref_seed_compares_uidvalidity_after_real_and_skipped_examine() -> None:
    """IR5 for the seed: located in INBOX, then re-opened (a real EXAMINE) with a new UIDVALIDITY → the read did not
    complete; located in Sent (still selected after the location) → read without a new EXAMINE."""
    server = icloud(inbox=[msg(1, 30, "seed", message_id="<s@example.test>")])
    inbox = next(f for f in server.folders if f.raw_name == "INBOX")

    def renumber_after_location(command: str, argument: object) -> None:
        if command == "EXAMINE" and argument == "INBOX" and examined(server).count("INBOX") == 1:
            inbox.uidvalidity = 8

    hook(server, renumber_after_location)
    error = refused(lambda: thread(server, seed=ref("s@example.test", server)))
    assert (error.code, error.cause) == ("upstream_timeout", "IncompleteSearch")

    in_sent = icloud(sent=[msg(1, 30, "sent seed", sender=OWNER, message_id="<s@example.test>")])
    page = thread(in_sent, seed=ref("s@example.test", in_sent))
    assert subjects(page) == ["sent seed"]
    seed_read = index_of(in_sent, "UID FETCH", lambda argument: SUMMARY_HEADER_ITEM in argument[1])
    assert examined(in_sent)[:4] == ["INBOX", "Archive", "Deleted Messages", "Sent Messages"]
    assert names(in_sent)[:seed_read].count("EXAMINE") == 4  # no EXAMINE between the location and the seed read


# --- body pass -------------------------------------------------------------------------------------------------


def test_body_pass_groups_newest_first_selected_on_tie() -> None:
    server = gmail(
        with_all=False,
        inbox=[msg(1, 5, "newest", thrid=THREAD, message_id="<a@example.test>")],
        sent=[msg(2, 30, "older", thrid=THREAD, sender=OWNER, message_id="<b@example.test>")],
        trash=[msg(3, 60, "oldest", thrid=THREAD, message_id="<c@example.test>")],
    )
    page = thread(server, key=gmail_key())
    assert [m.body.text for m in page.members] == ["Hello"] * 3
    start = index_of(server, "UID FETCH", lambda argument: argument[1] == ["BODYSTRUCTURE"])
    body_boxes = [argument for name, argument in server.commands[start - 1 :] if name == "EXAMINE"]
    assert body_boxes == ["INBOX", "[Gmail]/Sent Mail", GMAIL_TRASH]  # newest group first

    tie = gmail(
        with_all=False,
        inbox=[msg(1, 5, "inbox", thrid=THREAD, message_id="<a@example.test>")],
        trash=[msg(3, 5, "trash", thrid=THREAD, message_id="<c@example.test>")],
    )
    thread(tie, key=gmail_key())
    start = index_of(tie, "UID FETCH", lambda argument: argument[1] == ["BODYSTRUCTURE"])
    assert tie.commands[start - 1][0] != "EXAMINE"  # Trash is still selected: read first, without an EXAMINE
    assert [argument for name, argument in tie.commands[start:] if name == "EXAMINE"] == ["INBOX"]


PDF = (b"application", b"pdf", (b"name", b"a.pdf"), None, None, b"base64", 4000, None, (b"attachment", None), None)


def test_bodies_in_slices_newest_first_structures_in_chunks() -> None:
    """Rev 4.13 §5.2 "Bodies", §5.4 (T14): within a folder group the members are read newest first in slices of at
    most BODY_SLICE_MEMBERS; per slice their BODYSTRUCTUREs in chunks of at most STRUCTURE_CHUNK_UIDS, then one text
    FETCH per section for the slice."""
    assert (imap_thread.STRUCTURE_CHUNK_UIDS, imap_thread.BODY_SLICE_MEMBERS) == (2, 10)
    second = ([PDF, PLAIN], b"mixed", None, None, None, None)  # its text is section 2
    server = gmail(
        all_mail=[
            msg(
                uid,
                100 - uid,
                f"m{uid}",
                thrid=THREAD,
                message_id=f"<m{uid}@example.test>",
                **({"bodystructure": second} if uid == 20 else {}),  # type: ignore[arg-type]
            )
            for uid in range(1, 24)
        ]
    )
    page = thread(server, key=gmail_key())
    assert len(page.members) == 23
    assert all(m.body.text == "Hello" for m in page.members)
    one, two = "BODY.PEEK[1]<0.65536>", "BODY.PEEK[2]<0.65536>"
    newest_first = list(range(23, 0, -1))
    expected: list[tuple[list[int], list[str]]] = []
    for start in range(0, len(newest_first), imap_thread.BODY_SLICE_MEMBERS):
        piece = newest_first[start : start + imap_thread.BODY_SLICE_MEMBERS]
        for chunk in range(0, len(piece), imap_thread.STRUCTURE_CHUNK_UIDS):
            expected.append((sorted(piece[chunk : chunk + imap_thread.STRUCTURE_CHUNK_UIDS]), ["BODYSTRUCTURE"]))
        expected.append((sorted(uid for uid in piece if uid != 20), [one]))
        if 20 in piece:
            expected.append(([20], [two]))
    body_pass = [(uids, items) for uids, items in fetches(server) if items in (["BODYSTRUCTURE"], [one], [two])]
    assert body_pass == expected
    assert body_pass[0] == ([22, 23], ["BODYSTRUCTURE"])  # the newest first
    assert body_pass[5] == ([14, 15, 16, 17, 18, 19, 21, 22, 23], [one])  # the first slice's text before the next


def test_body_fetch_items_are_partial_peeks_only() -> None:
    server = two_folders(bodystructure=with_attachment())
    page = thread(server, seed=ref("b@example.test", server))
    assert page.members[0].summary.has_attachments is True
    assert [m.summary.has_attachments for m in page.members[1:]] == [False, False, False]
    for _, items in fetches(server):
        for item in items:
            assert item in PLAIN_ITEMS or BODY_ITEM.fullmatch(item) or LOCATE_ITEM.fullmatch(item), item
            assert "RFC822" not in item
            assert "BODY[]" not in item
            assert "BODY.PEEK[]" not in item
    assert all(
        name in {"LOGIN", "CAPABILITY", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
        for name in names(server)
    )


def test_body_cut_at_64_kib() -> None:
    server = gmail(
        all_mail=[
            msg(1, 30, "sized", thrid=THREAD, text=b"x" * 1000, bodystructure=structure(size=70_000)),
            msg(2, 20, "unsized", thrid=THREAD, text=b"y" * 70_000, bodystructure=structure(size=0)),
            msg(3, 10, "small", thrid=THREAD, text=b"z" * 100, bodystructure=structure(size=100)),
        ]
    )
    page = thread(server, key=gmail_key(), max_chars=5000)
    members = by_subject(page)
    assert members["sized"].body.cut is True
    assert members["unsized"].body.cut is True
    assert members["unsized"].body.original_chars == 65_536  # the fetch read 64 KiB
    assert members["small"].body.cut is False
    assert (members["small"].body.text, members["small"].body.original_chars) == ("z" * 100, 100)


def test_body_fetch_no_omits_group_only() -> None:
    server = two_folders()

    def refuse_all_mail_body(command: str, argument: object) -> None:
        if command == "UID FETCH" and argument[1] == ["BODY.PEEK[1]<0.65536>"] and 1 in argument[0]:  # type: ignore[index,operator]
            raise server.no("UID FETCH")

    hook(server, refuse_all_mail_body)
    page = thread(server, key=gmail_key())
    members = by_subject(page)
    assert [members[s].body.omitted for s in ("one", "two", "three")] == [True, True, True]
    assert [members[s].body.source for s in ("one", "two", "three")] == ["none"] * 3  # §5.2: not read (I-1)
    assert (members["four"].body.omitted, members["four"].body.text) == (False, "Hello")
    assert page.truncated is True
    assert page.overview is not None
    assert page.overview.message_count_exact is True


def test_uidvalidity_changed_before_body_pass_omits_group() -> None:
    """im-M8: All Mail renumbered between the member pass and the body pass → its members keep no body."""
    server = gmail(
        all_mail=[msg(1, 10, "all mail", INBOX_L, thrid=THREAD, message_id="<a@example.test>")],
        trash=[msg(5, 60, "trash", thrid=THREAD, message_id="<t@example.test>")],
    )
    all_mail = next(f for f in server.folders if f.raw_name == GMAIL_ALL)

    def renumber(command: str, argument: object) -> None:
        if command == "EXAMINE" and argument == GMAIL_TRASH:
            all_mail.uidvalidity = 99

    hook(server, renumber)
    page = thread(server, key=gmail_key())
    members = by_subject(page)
    assert members["all mail"].body.omitted is True
    assert members["all mail"].body.source == "none"
    assert members["trash"].body.text == "Hello"
    assert page.truncated is True
    structure_fetches = [uids for uids, items in fetches(server) if items == ["BODYSTRUCTURE"]]
    assert structure_fetches == [[5]]  # nothing fetched in the renumbered mailbox


def test_html_body_converted_and_quotes_removed() -> None:
    html = (
        b"<div>Thanks, see you on Friday.</div>"
        b"<div>On Mon, 20 Oct 2026 at 10:00, Sender &lt;sender@example.test&gt; wrote:</div>"
        b"<blockquote><div>&gt; the old question</div></blockquote>"
    )
    server = gmail(
        all_mail=[msg(1, 10, "Re: plan", thrid=THREAD, text=html, bodystructure=structure((b"text", b"html")))]
    )
    body = thread(server, key=gmail_key()).members[0].body
    assert body.source == "text/html-converted"
    assert body.quote_removal == "heuristic"
    assert body.text.strip() == "Thanks, see you on Friday."
    assert body.original_chars is not None
    assert body.original_chars > len(body.text)


class DeepStructureClient(FakeImapClient):
    """IMAPClient's recursive response parser raises RecursionError on a BODYSTRUCTURE nested ~1000 deep."""

    deep: frozenset[int] = frozenset({2})

    def fetch(self, messages: object, items: object) -> dict[int, dict[bytes, object]]:
        if "BODYSTRUCTURE" in items and any(uid in self.deep for uid in messages):  # type: ignore[operator,attr-defined]
            raise RecursionError("maximum recursion depth exceeded")
        return super().fetch(messages, items)  # type: ignore[arg-type]


def test_recursion_error_bodystructure_degrades_member(provider_log: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """P10: the failing 10-UID chunk is fetched again one UID at a time; only the member that fails alone degrades."""
    server = gmail(all_mail=[msg(uid, 60 - uid, f"m{uid}", thrid=THREAD) for uid in (1, 2, 3)])
    monkeypatch.setattr(server, "client", lambda: DeepStructureClient(server))
    page = thread(server, key=gmail_key())
    members = by_subject(page)
    assert (members["m1"].body.text, members["m3"].body.text) == ("Hello", "Hello")
    assert (members["m2"].body.omitted, members["m2"].body.source) == (True, "none")
    assert page.truncated is True
    structure_fetches = [uids for uids, items in fetches(server) if items == ["BODYSTRUCTURE"]]
    assert sorted(structure_fetches) == [[1], [3]]  # the chunk [1, 2, 3] raised before the fake logged it; [2] too
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["RecursionError"]


def test_soft_stop_before_bodystructure_gives_source_none() -> None:
    """A-td3a, Q-c: bodies not reached keep `body_source: "none"`, `body_omitted`, and `has_attachments: false`."""
    probe = two_folders(bodystructure=with_attachment())
    thread(probe, key=gmail_key())
    last_member_fetch = max(
        i for i, (n, a) in enumerate(probe.commands) if n == "UID FETCH" and SUMMARY_HEADER_ITEM in a[1]
    )  # type: ignore[index]

    server = two_folders(bodystructure=with_attachment())
    stop_after(server, last_member_fetch + 1)
    page = thread(server, key=gmail_key())
    assert len(page.members) == 4
    for member in page.members:
        assert (member.body.text, member.body.source, member.body.omitted) == ("", "none", True)
        assert member.summary.has_attachments is False
    assert page.truncated is True
    assert page.overview is not None
    assert page.overview.message_count_exact is True


@pytest.mark.parametrize("stage", ["decode", "html", "quotes"])
def test_one_hostile_member_degrades_only_itself(
    stage: str, provider_log: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC1: an exception while decoding, converting or removing quotes for one member omits only that body."""
    html = structure((b"text", b"html"))
    server = gmail(
        all_mail=[
            msg(1, 30, "fine", thrid=THREAD, text=b"<p>fine</p>", bodystructure=html),
            msg(2, 20, "hostile", thrid=THREAD, text=b"<p>boom</p>", bodystructure=html),
            msg(3, 10, "also fine", thrid=THREAD, text=b"<p>ok</p>", bodystructure=html),
        ]
    )
    decode, convert, remove = imap_thread.decode_part, imap_thread.html_to_text, threads.remove_quotes

    def explode(text: object) -> None:
        if (b"boom" if isinstance(text, bytes) else "boom") in text:  # type: ignore[operator]
            raise ValueError("hostile")

    if stage == "decode":
        monkeypatch.setattr(imap_thread, "decode_part", lambda data, part: (explode(data), decode(data, part))[1])
    elif stage == "html":
        monkeypatch.setattr(imap_thread, "html_to_text", lambda text: (explode(text), convert(text))[1])
    else:
        monkeypatch.setattr(
            threads, "remove_quotes", lambda text, *, subject: (explode(text), remove(text, subject=subject))[1]
        )
    page = thread(server, key=gmail_key())
    members = by_subject(page)
    assert (members["hostile"].body.omitted, members["hostile"].body.source) == (True, "none")
    assert (members["fine"].body.text.strip(), members["also fine"].body.text.strip()) == ("fine", "ok")
    assert page.truncated is True
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["ValueError"]


@pytest.mark.parametrize("cause", ["fetch no", "soft stop", "decode"])
def test_body_not_read_after_its_structure_has_source_none(cause: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2 "Bodies", final review I-1: once BODYSTRUCTURE was read, a body whose text was not read (the section
    FETCH answered NO, a soft stop between the structure and the text fetch, an exception while decoding, SC1) is
    `""`, `body_omitted`, `body_source: "none"`, `quote_removal: "none"` and `body_original_chars: null`, and the
    result is `truncated` (P11: the count stays exact); `has_attachments` stays as the structure says."""
    server = icloud(inbox=[msg(1, 30, "m", bodystructure=with_attachment(), message_id="<m@example.test>")])
    section_fetch = "BODY.PEEK[1]<0.65536>"
    asked: list[list[str]] = []

    def record(command: str, argument: object) -> None:
        if command == "UID FETCH":
            asked.append(list(argument[1]))  # type: ignore[index]
            if cause == "fetch no" and section_fetch in asked[-1]:
                raise server.no("UID FETCH")

    hook(server, record)
    if cause == "soft stop":
        original = server.run

        def run(command: str, argument: object = None) -> None:
            original(command, argument)
            if command == "UID FETCH" and "BODYSTRUCTURE" in argument[1]:  # type: ignore[index]
                clock_of(server).advance(17.0)

        server.run = run  # type: ignore[method-assign]
    else:

        def explode(data: bytes, part: object) -> str:
            raise ValueError("undecodable")

        monkeypatch.setattr(imap_thread, "decode_part", explode)
    page = thread(server, seed=ref("m@example.test", server))
    member = page.members[0]
    body = member.body
    assert (body.text, body.omitted, body.source, body.quote_removal, body.original_chars) == (
        "",
        True,
        "none",
        "none",
        None,
    )
    assert member.summary.has_attachments is True  # read from the structure
    assert page.truncated is True
    assert page.overview is not None
    assert page.overview.message_count_exact is True
    assert any("BODYSTRUCTURE" in items for items in asked)
    assert any(section_fetch in items for items in asked) is (cause != "soft stop")


def test_uidvalidity_compared_after_real_and_skipped_examine() -> None:
    """IR5: the body pass compares each group's UIDVALIDITY with `session.selected_uidvalidity`, after a real EXAMINE
    and when the mailbox is still selected (no EXAMINE)."""
    server = icloud(inbox=[msg(1, 30, "m", message_id="<m@example.test>")], sent=[msg(2, 20, "s")])
    box = mailbox(server)

    def copy(uidvalidity: int) -> ThreadCopy:
        summary = MailSummary(
            MessageRef("icloud", "INBOX", uidvalidity, 1),
            msg(1, 30).internaldate,
            False,
            False,
            None,
            None,
            "m",
            "",
            meta=MailMeta(),  # type: ignore[arg-type]
        )
        return ThreadCopy(summary, "headers", (), in_sent=False)

    def work(session: ImapSession) -> list[tuple[bool, list[str]]]:
        folder_map = session.folder_map()
        sent = folder_map.role("sent")
        assert sent is not None
        entries = {"INBOX": folder_map.inbox}
        out: list[tuple[bool, list[str]]] = []
        session.examine(sent)
        for uidvalidity in (9, 9, 7):  # real EXAMINE (stale), skipped EXAMINE (stale), skipped (current)
            start = len(server.commands)
            built = imap_thread._body_pass(
                session, [copy(uidvalidity)], entries, max_chars=2000, soft_check=lambda: None
            )
            out.append((built[0].body.omitted, [name for name, _ in server.commands[start:]]))
        return out

    assert box._run(work, deadline(server)) == [
        (True, ["EXAMINE"]),
        (True, []),
        (False, ["UID FETCH", "UID FETCH"]),
    ]


# --- soft stop and tool errors ---------------------------------------------------------------------------------


def _first_after(server: FakeImapServer, stage: str) -> int:
    """How many commands run before the stop, per stage, from a run without a stop."""
    if stage == "seed":
        return index_of(server, "LIST") + 1
    if stage == "members":  # after the All Mail thread fetch, before Trash is opened for the thread search
        return index_of(server, "UID FETCH", lambda a: SUMMARY_HEADER_ITEM in a[1]) + 1  # type: ignore[index]
    return index_of(server, "UID FETCH", lambda a: a[1] == ["BODYSTRUCTURE"]) + 1  # type: ignore[index]


@pytest.mark.parametrize("stage", ["login", "seed", "members", "bodies"])
def test_soft_stop_matrix(stage: str) -> None:
    probe = two_folders()
    thread(probe, seed=ref("b@example.test", probe))
    server = two_folders()
    if stage == "login":
        error = refused(lambda: thread(server, seed=ref("b@example.test", server), call=deadline(server, spent=16.5)))
        assert error.code == "upstream_timeout"
        return
    stop_after(server, _first_after(probe, stage))
    if stage == "seed":
        assert refused(lambda: thread(server, seed=ref("b@example.test", server))).code == "upstream_timeout"
        return
    page = thread(server, seed=ref("b@example.test", server))
    assert page.overview is not None
    assert page.truncated is True
    if stage == "members":
        assert subjects(page) == ["one", "two", "three"]  # Trash never searched
        assert page.overview.message_count_exact is False
        assert [m.body.omitted for m in page.members] == [True] * 3  # no command after the stop
    else:
        assert subjects(page) == ["one", "two", "three", "four"]
        assert page.overview.message_count_exact is True
        assert all(m.body.omitted for m in page.members)
        assert by_subject(page)["four"].body.source == "none"  # its structure was read, its text not (I-1)


def test_hard_deadline_and_bad_are_tool_errors() -> None:
    probe = two_folders()
    thread(probe, key=gmail_key())
    for stage in ("members", "bodies"):
        late = two_folders()
        stop_after(late, _first_after(probe, stage), seconds=100.0)
        late_call = deadline(late, hard=50.0)
        assert (
            refused(lambda late=late, c=late_call: thread(late, key=gmail_key(), call=c)).code
        ) == "upstream_timeout"  # type: ignore[misc]

    hard = two_folders()
    stop_after(hard, _first_after(probe, "members"), seconds=100.0)
    error = refused(lambda: thread(hard, key=gmail_key(), call=deadline(hard, soft=500.0, hard=60.0)))
    assert (error.code, error.cause) == ("upstream_timeout", "CallDeadline")

    for predicate in (
        lambda c, a: c == "UID SEARCH" and b"X-GM-THRID" in a,
        lambda c, a: c == "UID FETCH" and a[1] == ["BODY.PEEK[1]<0.65536>"],
        lambda c, a: c == "UID FETCH" and a[1] == ["BODYSTRUCTURE"],
    ):
        server = two_folders()

        def bad(command: str, argument: object, server: FakeImapServer = server, p: object = predicate) -> None:
            if p(command, argument):  # type: ignore[operator]
                raise server.bad(command)

        hook(server, bad)
        assert refused(lambda server=server: thread(server, key=gmail_key())).code == "upstream_error"  # type: ignore[misc]


def test_member_row_without_internaldate_dropped_not_exact(provider_log: Capture) -> None:
    """Q9: a member row without INTERNALDATE is dropped with one item_degraded line; the count is not exact."""
    server = gmail(
        all_mail=[
            msg(1, 30, "dated", thrid=THREAD, message_id="<a@example.test>"),
            msg(2, None, "undated", thrid=THREAD, message_id="<b@example.test>"),
        ]
    )
    page = thread(server, key=gmail_key())
    assert subjects(page) == ["dated"]
    assert page.overview is not None
    assert page.overview.message_count_exact is False
    assert page.truncated is False
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["MissingInternalDate"]


def test_id_seed_without_internaldate_kept_not_exact(provider_log: Capture) -> None:
    """T7-1: an `id` seed whose row has no INTERNALDATE is kept, as `get_message` opens it (at the call's time), with
    one item_degraded line; the count is not exact, since its place in the order is a guess."""
    server = gmail(
        all_mail=[
            msg(1, 30, "dated", INBOX_L, thrid=THREAD, message_id="<a@example.test>"),
            msg(2, None, "undated seed", INBOX_L, thrid=THREAD, message_id="<b@example.test>"),
        ]
    )
    page = thread(server, seed=mid_ref(server, GMAIL_ALL, 2, 11), include_trash=False)
    assert page.outcome == "ok"
    assert subjects(page) == ["dated", "undated seed"]  # the call's time: the newest
    assert page.members[1].summary.ref == MessageRef("gmail", GMAIL_ALL, 11, 2)
    assert page.members[1].body.text == "Hello"
    assert page.overview is not None
    assert page.overview.message_count_exact is False
    assert page.truncated is False
    assert [json.loads(line)["exception"] for line in provider_log.lines] == ["MissingInternalDate"]
