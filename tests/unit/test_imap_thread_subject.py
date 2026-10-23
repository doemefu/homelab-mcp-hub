"""IMAP `get_thread` on servers other than Gmail: the subject fallback (3) and `subject` keys (spec 080 §6.5, §5.2
`get_thread`, §5.5 participants; WP13c T9, Option 8a, Q3, Q4, Q5, Q-d, IR4, MF-P1, P11, P12)."""

from collections.abc import Callable

import pytest

import mcp_hub.providers.imap_thread as imap_thread
from mcp_hub.ids import MessageRef, ThreadKey
from mcp_hub.providers.base import ProviderError, ThreadPage
from mcp_hub.providers.headers import SUMMARY_HEADER_ITEM, subject_digest
from mcp_hub.providers.imap_session import LOOKUP_FETCH_UIDS
from mcp_hub.providers.imap_thread import MAX_SUBJECT_CANDIDATES, SUBJECT_SCAN_ITEM
from tests.support.imap_fake import FakeImapClient, FakeImapServer, FakeMessage
from tests.support.imap_threads import (
    INBOX_L,
    NOW,
    THREAD,
    clock_of,
    deadline,
    fetches,
    gmail,
    hdr,
    icloud,
    mailbox,
    mid_ref,
    msg,
    ref,
    searches,
    thread,
    with_attachment,
)

DAY = 24 * 60.0  # minutes
SECOND = 1 / 60  # minutes
ALICE = "Alice <alice@example.test>"
BOB = "Bob <bob@example.test>"
CAROL = "Carol <carol@example.test>"
OWNER = "Owner <owner@example.org>"
CORE = ["INBOX", "Sent Messages", "Archive", "Deleted Messages"]


@pytest.fixture(autouse=True)
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan of a `subject` key counts its days from the call's date: pin it to the fixtures' NOW."""
    monkeypatch.setattr(imap_thread, "_utc_now", lambda: NOW)


def ids(page: ThreadPage) -> list[str | None]:
    return [member.summary.meta.message_id for member in page.members]


def linked(page: ThreadPage) -> list[tuple[str | None, str]]:
    return [(member.summary.meta.message_id, member.linked_by) for member in page.members]


def exact(page: ThreadPage) -> bool:
    assert page.overview is not None
    return page.overview.message_count_exact


def scans(server: FakeImapServer) -> list[bytes]:
    """Subject scans: the only searches with a date."""
    return [criteria for criteria in searches(server) if b"SINCE" in criteria]


def header_searches(server: FakeImapServer) -> list[bytes]:
    return [criteria for criteria in searches(server) if b"HEADER" in criteria]


def scanned_in(server: FakeImapServer) -> list[tuple[object, list[int]]]:
    """(mailbox, UIDs) of every FETCH of the 8 KiB scan item."""
    out: list[tuple[object, list[int]]] = []
    current: object = None
    for name, argument in server.commands:
        if name == "EXAMINE":
            current = argument
        elif name == "UID FETCH":
            uids, items = argument  # type: ignore[misc]
            if SUBJECT_SCAN_ITEM in items:
                out.append((current, sorted(uids)))
    return out


def scan_boxes(server: FakeImapServer) -> list[object]:
    """The mailbox selected at each subject scan."""
    out: list[object] = []
    current: object = None
    for name, argument in server.commands:
        if name == "EXAMINE":
            current = argument
        elif name == "UID SEARCH" and b"SINCE" in argument:  # type: ignore[operator]
            out.append(current)
    return out


def after(server: FakeImapServer, predicate: Callable[[str, object], bool], seconds: float) -> None:
    """The clock jumps by `seconds` right after the first command that satisfies `predicate`."""
    original = server.run
    state = {"done": False}

    def run(name: str, argument: object = None) -> None:
        original(name, argument)
        if not state["done"] and predicate(name, argument):
            state["done"] = True
            clock_of(server).advance(seconds)

    server.run = run  # type: ignore[method-assign]


def fail_scan(server: FakeImapServer, box: str, command: str = "UID SEARCH") -> None:
    """Answer `command` in `box` with NO once the subject scan has started: the scan's SEARCH, or the EXAMINE."""
    original = server.run
    state: dict[str, object] = {"box": None, "started": False}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = argument
        scan = name == "UID SEARCH" and b"SINCE" in argument  # type: ignore[operator]
        state["started"] = state["started"] or scan
        if name == command and state["box"] == box and state["started"] and (scan or name == "EXAMINE"):
            server.commands.append((name, argument))
            raise server.no(name)
        original(name, argument)

    server.run = run  # type: ignore[method-assign]


class DatelessClient(FakeImapClient):
    """A server whose subject scans answer every message of the folder, whatever the dates."""

    def search(self, criteria: bytes) -> list[int]:
        if b"SINCE" not in criteria:
            return super().search(criteria)
        self._server.run("UID SEARCH", criteria)
        return sorted(self._selected("UID SEARCH").messages)


def seed_in_inbox(server: FakeImapServer, uid: int = 1) -> MessageRef:
    return mid_ref(server, "INBOX", uid, 7)


def plan(**seed_headers: str) -> list[FakeMessage]:
    """A seed "Plan" from Alice and a later "Re: Plan" from Alice without thread headers."""
    subject = seed_headers.pop("subject", "Plan")
    return [
        msg(1, 5 * DAY, subject, sender=ALICE, message_id="<s@example.test>", **seed_headers),
        msg(2, 3 * DAY, "Re: Plan", sender=ALICE, message_id="<c@example.test>"),
    ]


# --- when (3) runs ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["fallback", "member found", "references", "in-reply-to", "empty subject", "gmail"],
)
def test_subject_fallback_only_without_members_and_headers(case: str) -> None:
    """§6.5 (3): only when rounds (1)-(2) found no member besides the seed, the seed has neither References nor
    In-Reply-To, its normalised subject is not empty, and never on Gmail."""
    if case == "gmail":
        server = gmail(
            all_mail=[
                msg(1, 5 * DAY, "Plan", INBOX_L, thrid=THREAD, sender=ALICE, message_id="<s@example.test>"),
                msg(2, 3 * DAY, "Re: Plan", INBOX_L, thrid=THREAD + 1, sender=ALICE, message_id="<c@example.test>"),
            ]
        )
        page = thread(server, seed=ref("s@example.test", server))
        assert linked(page) == [("s@example.test", "server")]
        assert scans(server) == []
        return
    inbox = {
        "fallback": plan(),
        "member found": [
            *plan(),
            msg(3, 4 * DAY, "Re: Plan", sender=BOB, message_id="<r@example.test>", in_reply_to="<s@example.test>"),
        ],
        "references": plan(references="<x@example.test>"),
        "in-reply-to": plan(in_reply_to="<x@example.test>"),
        "empty subject": [
            msg(1, 5 * DAY, "Re: [list]", sender=ALICE, message_id="<s@example.test>"),
            msg(2, 3 * DAY, "AW: [list]", sender=ALICE, message_id="<c@example.test>"),
        ],
    }[case]
    server = icloud(inbox=inbox)
    page = thread(server, seed=seed_in_inbox(server))
    if case == "fallback":
        assert linked(page) == [("s@example.test", "headers"), ("c@example.test", "subject")]
        assert len(scans(server)) == 4
    elif case == "member found":
        assert linked(page) == [("s@example.test", "headers"), ("r@example.test", "headers")]
        assert scans(server) == []
    else:
        assert linked(page) == [("s@example.test", "headers")]
        assert scans(server) == []
    assert exact(page) is True
    assert page.truncated is False


def test_subject_needs_shared_participant() -> None:
    """§5.5 participants: incoming From + Cc, outgoing To + Cc, own addresses removed (a `+tag` and the case
    ignored); a candidate joins only when it shares an address with the seed, compared casefolded (F-1)."""
    server = icloud(
        inbox=[
            msg(
                1,
                5.0 * DAY,
                "Plan",
                sender="Alice <Alice@Example.Test>",
                cc="owner+news@example.org",
                message_id="<s@example.test>",
            ),
            msg(2, 4.9 * DAY, "Plan", sender=BOB, message_id="<b@example.test>"),
            msg(3, 4.8 * DAY, "Plan", sender=CAROL, cc="OWNER+x@Example.org", message_id="<own@example.test>"),
            msg(4, 4.7 * DAY, "Plan", sender="ALICE@EXAMPLE.TEST", message_id="<case@example.test>"),
        ],
        sent=[
            msg(5, 4.6 * DAY, "Re: Plan", sender=OWNER, to=BOB, message_id="<to-bob@example.test>"),
            msg(6, 4.5 * DAY, "Re: Plan", sender=OWNER, to=ALICE, message_id="<to-alice@example.test>"),
        ],
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert linked(page) == [
        ("s@example.test", "headers"),
        ("case@example.test", "subject"),
        ("to-alice@example.test", "subject"),
    ]
    assert [m.summary.meta.direction for m in page.members] == ["incoming", "incoming", "outgoing"]
    assert exact(page) is True


def test_subject_not_transitive() -> None:
    """Q3: a candidate must share an address with the seed; sharing one with another subject member is not enough."""
    server = icloud(
        inbox=[
            msg(1, 5.0 * DAY, "Plan", sender=ALICE, message_id="<s@example.test>"),
            msg(2, 4.0 * DAY, "Plan", sender=ALICE, cc=BOB, message_id="<ab@example.test>"),
            msg(3, 3.0 * DAY, "Plan", sender=BOB, message_id="<b@example.test>"),
            msg(4, 2.0 * DAY, "Plan", sender=CAROL, cc=BOB, message_id="<cb@example.test>"),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "ab@example.test"]
    assert page.overview is not None
    assert page.overview.participants == ("alice@example.test", "bob@example.test")


def test_subject_window_exact_thirty_days() -> None:
    """± 30 days on INTERNALDATE, both sides: 29 d 23 h in, 30 d 1 s out; the SEARCH sends the day window
    `SINCE <seed - 31> BEFORE <seed + 32>`, which the exact check narrows (IR4)."""
    seed = 40 * DAY
    near, far = 29 * DAY + 23 * 60, 30 * DAY + SECOND
    server = icloud(
        inbox=[
            msg(1, seed, "Plan", sender=ALICE, message_id="<s@example.test>"),
            msg(2, seed - near, "Plan", sender=ALICE, message_id="<in-after@example.test>"),
            msg(3, seed - far, "Plan", sender=ALICE, message_id="<out-after@example.test>"),
            msg(4, seed + near, "Plan", sender=ALICE, message_id="<in-before@example.test>"),
            msg(5, seed + far, "Plan", sender=ALICE, message_id="<out-before@example.test>"),
        ]
    )
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["in-before@example.test", "s@example.test", "in-after@example.test"]
    # the seed arrived on 11 Sep 2026 (NOW - 40 d)
    assert scans(server) == [b"NOT DRAFT SINCE 11-Aug-2026 BEFORE 13-Oct-2026"] * 4
    assert sorted(map(str, scan_boxes(server))) == sorted(CORE)


def test_subject_scan_folders_follow_include_trash() -> None:
    """The scan runs in the round folders: inbox, sent, archive and, with `include_trash`, trash; never junk,
    drafts or `other`."""
    server = icloud(inbox=plan())
    thread(server, seed=seed_in_inbox(server), include_trash=False)
    assert sorted(map(str, scan_boxes(server))) == ["Archive", "INBOX", "Sent Messages"]


def test_subject_candidates_newest_five_hundred_uids_inexact_beyond() -> None:
    """Q4: per folder the 500 highest UIDs are read; more hits leave the count not exact (P11: not truncated)."""
    noise = [msg(3 + i, 4 * DAY, "Plan", sender=BOB, message_id=f"<n{i}@example.test>") for i in range(499)]
    low = msg(2, 4 * DAY, "Plan", sender=ALICE, message_id="<low@example.test>")
    high = msg(502, 4 * DAY, "Plan", sender=ALICE, message_id="<high@example.test>")
    seed = msg(1, 5 * DAY, "Plan", sender=ALICE, message_id="<s@example.test>")
    server = icloud(inbox=[seed, low, *noise, high])
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "high@example.test"]
    assert exact(page) is False
    assert page.truncated is False
    fetched = [uid for box, uids in scanned_in(server) if box == "INBOX" for uid in uids]
    assert len(fetched) == MAX_SUBJECT_CANDIDATES
    assert min(fetched) == 3  # the seed was read already (P7); of the other 501 hits the highest 500
    assert all(len(uids) <= LOOKUP_FETCH_UIDS for _, uids in scanned_in(server))

    server = icloud(inbox=[seed, *noise, high])  # exactly 500 hits besides the seed
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "high@example.test"]
    assert exact(page) is True


# --- subject keys ----------------------------------------------------------------------------------------------


def test_subject_key_seed_is_newest_match_and_scans_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q-d, IR4, im-M11: a `subject` key runs (3) only. The first scan sends `SINCE <today - 31>` without BEFORE; the
    newest match is the seed; then only the uncovered part of the seed's window, `SINCE <seed - 31> BEFORE
    <today - 31>`, is scanned, and no UID is fetched twice, also from a server whose SEARCH ignores the dates."""
    key = ThreadKey("icloud", "subject", subject_digest("Plan") or "")
    server = icloud(
        inbox=[
            msg(1, 5.0 * DAY, "Plan", sender=BOB, message_id="<bob-only@example.test>"),
            msg(2, 4.0 * DAY, "Plan", sender=BOB, cc=ALICE, message_id="<bob-cc-alice@example.test>"),
            msg(3, 2.0 * DAY, "Re: Plan", sender=ALICE, message_id="<seed@example.test>"),
            msg(4, 31.5 * DAY, "Plan", sender=ALICE, message_id="<old-in@example.test>"),
            msg(5, 33.0 * DAY, "Plan", sender=ALICE, message_id="<old-out@example.test>"),
            msg(6, 1.0 * DAY, "Other", sender=ALICE, message_id="<other@example.test>"),
        ],
        sent=[msg(7, 3.0 * DAY, "Re: Plan", sender=OWNER, to=ALICE, message_id="<sent@example.test>")],
    )
    page = thread(server, key=key)
    assert page.thread == key
    assert linked(page) == [
        ("old-in@example.test", "subject"),
        ("bob-cc-alice@example.test", "subject"),
        ("sent@example.test", "subject"),
        ("seed@example.test", "subject"),
    ]
    assert exact(page) is True
    # today is 21 Oct 2026; the seed arrived on 19 Oct
    first, uncovered = b"NOT DRAFT SINCE 20-Sep-2026", b"NOT DRAFT SINCE 18-Sep-2026 BEFORE 20-Sep-2026"
    assert scans(server) == [first] * 4 + [uncovered] * 4
    assert header_searches(server) == []  # no header rounds (Q-d)
    per_box: dict[object, list[int]] = {}
    for box, uids in scanned_in(server):
        per_box.setdefault(box, []).extend(uids)
    assert all(len(uids) == len(set(uids)) for uids in per_box.values())  # no repeated FETCH for the overlap

    members = linked(page)
    dateless = icloud(inbox=server.folders[0].messages.values(), sent=server.folders[1].messages.values())
    monkeypatch.setattr(dateless, "client", lambda: DatelessClient(dateless))
    page = thread(dateless, key=key)
    assert linked(page) == members
    assert len(scans(dateless)) == 8
    assert [box for box, _ in scanned_in(dateless)] == ["INBOX", "Sent Messages"]  # the second scan reads nothing


def test_subject_seed_linked_by() -> None:
    """MF-P1: the seed of a `subject` key is linked by "subject"; a `ref` seed with fallback members keeps "headers"."""
    server = icloud(inbox=plan())
    page = thread(server, key=ThreadKey("icloud", "subject", subject_digest("Plan") or ""))
    assert linked(page) == [("s@example.test", "subject"), ("c@example.test", "subject")]

    server = icloud(inbox=plan())
    page = thread(server, seed=ref("s@example.test", server))
    assert linked(page) == [("s@example.test", "headers"), ("c@example.test", "subject")]


def test_subject_key_without_match_not_found() -> None:
    """A complete scan without a match is `not_found`; a match older than 30 days is no seed, although the day margin
    of the SEARCH returns it; an incomplete scan that found nothing is `upstream_timeout` (IR3)."""
    key = ThreadKey("icloud", "subject", subject_digest("Plan") or "")
    server = icloud(inbox=[msg(1, 2 * DAY, "Other", sender=ALICE, message_id="<o@example.test>")])
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == "not_found"
    assert len(scans(server)) == 4

    server = icloud(inbox=[msg(1, 30 * DAY + 3 * 60, "Plan", sender=ALICE, message_id="<old@example.test>")])
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == "not_found"
    assert [uids for _, uids in scanned_in(server)] == [[1]]  # read on 20 Sep, but 30 d 3 h old

    server = icloud(inbox=[msg(1, 2 * DAY, "Other", sender=ALICE, message_id="<o@example.test>")])
    fail_scan(server, "Archive")
    with pytest.raises(ProviderError) as caught:
        thread(server, key=key)
    assert caught.value.code == "upstream_timeout"


def test_headers_key_root_member_is_seed_for_fallback() -> None:
    """Q5: with a `headers` key, the member whose Message-ID is the root acts as the seed for (3)."""
    key = ThreadKey("icloud", "headers", "s@example.test")
    server = icloud(inbox=plan())
    page = thread(server, key=key)
    assert page.thread == key
    assert linked(page) == [("s@example.test", "headers"), ("c@example.test", "subject")]
    assert scans(server) == [b"NOT DRAFT SINCE 15-Sep-2026 BEFORE 17-Nov-2026"] * 4  # the root arrived on 16 Oct
    assert exact(page) is True


def test_subject_members_linked_by_subject_with_full_summary() -> None:
    """Option 6a: the 8 KiB scan item lacks In-Reply-To and References, so a subject member's summary header item is
    fetched with its BODYSTRUCTURE in the body pass: its references, In-Reply-To, thread key, flags and attachments
    are those of a header member."""
    member = msg(
        2,
        3 * DAY,
        "Re: Plan",
        flags=(b"\\Seen", b"\\Answered"),
        text=b"Hi there",
        bodystructure=with_attachment(),
        sender=ALICE,
        cc=CAROL,
        message_id="<c@example.test>",
        in_reply_to="<elsewhere@example.test>",
        references="<root0@example.test> <elsewhere@example.test>",
    )
    server = icloud(inbox=[msg(1, 5 * DAY, "Plan", sender=ALICE, message_id="<s@example.test>"), member])
    page = thread(server, seed=seed_in_inbox(server))
    assert linked(page) == [("s@example.test", "headers"), ("c@example.test", "subject")]
    summary = page.members[1].summary
    assert summary.subject == "Re: Plan"
    assert (summary.from_address, summary.cc_addresses) == ("alice@example.test", ("carol@example.test",))
    assert summary.meta.in_reply_to == "elsewhere@example.test"
    assert summary.meta.references == ("root0@example.test", "elsewhere@example.test")
    assert summary.meta.thread == ThreadKey("icloud", "headers", "root0@example.test")
    assert (summary.unread, summary.meta.flags.answered, summary.has_attachments) == (False, True, True)
    assert (page.members[1].body.text, page.members[1].body.omitted) == ("Hi there", False)
    structure = [(uids, items) for uids, items in fetches(server) if "BODYSTRUCTURE" in items]
    assert structure == [([1, 2], ["BODYSTRUCTURE", SUMMARY_HEADER_ITEM])]


def test_subject_members_without_body_pass_keep_scan_fields() -> None:
    """A subject member whose body pass did not run (soft stop) keeps the fields of its scan item; its thread key
    is unknown until the summary header item was read."""
    server = icloud(inbox=plan())
    after(server, lambda name, argument: name == "UID FETCH" and SUBJECT_SCAN_ITEM in argument[1], 17.0)  # type: ignore[index]
    page = thread(server, seed=seed_in_inbox(server))
    assert linked(page) == [("s@example.test", "headers"), ("c@example.test", "subject")]
    summary = page.members[1].summary
    assert (summary.subject, summary.from_address) == ("Re: Plan", "alice@example.test")
    assert summary.meta.thread is None
    assert page.members[1].body.omitted is True


def test_subject_cut_block_without_message_id_inexact() -> None:
    """A scan block cut at 8 KiB whose Message-ID did not end before the cut is no candidate, and the count is not
    exact; a block cut inside its Subject only loses the subject match; a short block without a Message-ID is a
    candidate (never merged)."""
    many = ", ".join(f"x{i}@example.test" for i in range(600)).encode()  # about 10 KiB
    cut_id = (
        b"From: " + ALICE.encode() + b"\r\nSubject: Plan\r\nCc: " + many + b"\r\nMessage-ID: <cut@example.test>\r\n\r\n"
    )
    folded = b"Subject: Plan\r\n " + b"x" * 9000  # cut inside the Subject: the visible part reads "Plan"
    cut_subject = b"From: " + ALICE.encode() + b"\r\nMessage-ID: <cs@example.test>\r\n" + folded + b"\r\n\r\n"
    no_id = b"From: " + ALICE.encode() + b"\r\nSubject: Plan\r\n\r\n"
    seed = msg(1, 5 * DAY, "Plan", sender=ALICE, message_id="<s@example.test>")
    server = icloud(inbox=[seed, msg(2, 4 * DAY, headers=cut_id)])
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test"]
    assert exact(page) is False
    assert page.truncated is False

    server = icloud(inbox=[seed, msg(3, 4 * DAY, headers=cut_subject), msg(4, 3 * DAY, headers=no_id)])
    page = thread(server, seed=seed_in_inbox(server))
    assert linked(page) == [("s@example.test", "headers"), (None, "subject")]
    assert exact(page) is True


def test_no_subject_search_key_on_the_wire() -> None:
    """D74, D80: subjects are compared locally after decoding; no SEARCH carries a subject key or subject text."""
    subject = "=?utf-8?q?Caf=C3=A9_Pl=C3=A4n?="
    inbox = [
        msg(1, 5 * DAY, subject, sender=ALICE, message_id="<s@example.test>"),
        msg(3, 2 * DAY, f"Re: {subject}", sender=ALICE, message_id="<d@example.test>"),
    ]
    server = icloud(inbox=inbox)
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "d@example.test"]
    key_server = icloud(inbox=inbox)
    page = thread(key_server, key=ThreadKey("icloud", "subject", subject_digest("Café Plän") or ""))
    assert ids(page) == ["s@example.test", "d@example.test"]
    for criteria in searches(server) + searches(key_server):
        assert b"SUBJECT" not in criteria.upper()
        assert b"Caf" not in criteria
        assert b"=?" not in criteria
        assert criteria.startswith(b"NOT DRAFT ")


def test_subject_scan_reserve_and_soft_stop() -> None:
    """G2, F8, P11: the scan is not started once the 12 s reserve is spent (the seed alone, truncated, not exact);
    the reserve is checked before every folder, so a scan in progress stops before the next one; a soft stop in the
    scan keeps the members found so far (truncated, not exact) and the body pass sends nothing."""
    server = icloud(inbox=plan())
    after(server, lambda name, argument: name == "UID SEARCH" and len(header_searches(server)) == 8, 12.5)
    page = thread(server, seed=seed_in_inbox(server))
    assert len(header_searches(server)) == 8  # round (1) and one round-(2) iteration in four folders
    assert scans(server) == []
    assert ids(page) == ["s@example.test"]
    assert (page.truncated, exact(page)) == (True, False)
    assert page.members[0].body.omitted is False  # the body pass keeps its time

    # the rounds end in Archive, so the scan starts there (im-M7)
    server = icloud(inbox=plan()[:1], archive=plan()[1:])
    after(server, lambda name, argument: name == "UID SEARCH" and b"SINCE" in argument, 12.5)  # type: ignore[operator]
    page = thread(server, seed=seed_in_inbox(server))
    assert scan_boxes(server) == ["Archive"]
    started = next(
        i for i, (name, argument) in enumerate(server.commands) if name == "UID SEARCH" and b"SINCE" in argument
    )  # type: ignore[operator]
    assert [a for name, a in server.commands[started:] if name == "EXAMINE"] == ["INBOX"]  # the body pass only
    assert ids(page) == ["s@example.test", "c@example.test"]
    assert (page.truncated, exact(page)) == (True, False)

    server = icloud(inbox=plan()[:1], archive=plan()[1:])
    after(server, lambda name, argument: name == "EXAMINE" and bool(scans(server)), 12.5)
    page = thread(server, seed=seed_in_inbox(server))
    assert scan_boxes(server) == ["Archive"]  # INBOX was opened when the reserve ran out: no SEARCH there
    assert ids(page) == ["s@example.test", "c@example.test"]
    assert (page.truncated, exact(page)) == (True, False)

    sent = [msg(3, 2 * DAY, "Re: Plan", sender=OWNER, to=ALICE, message_id="<o@example.test>")]
    noise = [msg(10 + i, 2 * DAY, "Plan", sender=BOB, message_id=f"<n{i}@example.test>") for i in range(60)]
    server = icloud(inbox=[*plan(), *noise], sent=sent)
    after(server, lambda name, argument: name == "UID FETCH" and SUBJECT_SCAN_ITEM in argument[1], 17.0)  # type: ignore[index]
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "c@example.test"]  # read before the stop, inside the first chunk
    assert [uids for _, uids in scanned_in(server)] == [[2, *range(10, 9 + LOOKUP_FETCH_UIDS)]]  # one chunk
    assert (page.truncated, exact(page)) == (True, False)
    assert all(member.body.omitted for member in page.members)

    server = icloud(inbox=plan())
    page = thread(server, seed=seed_in_inbox(server), call=deadline(server, spent=12.5))
    assert searches(server) == []  # neither the rounds nor the scan start
    assert ids(page) == ["s@example.test"]
    assert (page.truncated, exact(page)) == (True, False)


@pytest.mark.parametrize("command", ["UID SEARCH", "EXAMINE"])
def test_subject_scan_search_no_partial(command: str) -> None:
    """G4, P11: a scan SEARCH or an EXAMINE in the scan answered NO leaves that folder's part missing (truncated, not
    exact), never an error."""
    server = icloud(inbox=plan())
    fail_scan(server, "Deleted Messages", command)
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "c@example.test"]
    assert (page.truncated, exact(page)) == (True, False)


def test_subject_scan_drops_drafts() -> None:
    """S1: every scan sends NOT DRAFT, and a draft from a server that ignores it is dropped."""
    draft = msg(3, 2 * DAY, "Re: Plan", flags=(b"\\Draft",), sender=ALICE, message_id="<d@example.test>")
    server = icloud(inbox=[*plan(), draft])
    server.honour_not_draft = False
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test", "c@example.test"]
    assert all(criteria.startswith(b"NOT DRAFT SINCE ") for criteria in scans(server))


def test_cut_seed_block_gets_no_subject_fallback() -> None:
    """A seed block cut at 64 KiB may hide its References: no subject fallback from it; the count is not exact
    already (IR2)."""
    many = ", ".join(f"x{i}@example.test" for i in range(4000)).encode()  # about 76 KiB
    raw = b"From: " + ALICE.encode() + b"\r\nMessage-ID: <s@example.test>\r\nSubject: Plan\r\nTo: " + many + b"\r\n\r\n"
    server = icloud(inbox=[msg(1, 5 * DAY, headers=raw), plan()[1]])
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test"]
    assert scans(server) == []
    assert exact(page) is False


def test_subject_key_scans_share_the_folder_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q4 over both scans of a `subject` key: a folder's second scan reads only what its first scan left of the
    budget (the highest UIDs); a folder whose first scan used it all is not scanned again (not exact already)."""
    monkeypatch.setattr(imap_thread, "MAX_SUBJECT_CANDIDATES", 3)
    key = ThreadKey("icloud", "subject", subject_digest("Plan") or "")

    def plan_msg(uid: int, days: float) -> FakeMessage:
        return msg(uid, days * DAY, "Plan", sender=ALICE, message_id=f"<m{uid}-{days}@example.test>")

    server = icloud(
        inbox=[plan_msg(1, 2.1), plan_msg(2, 2.2), plan_msg(10, 31.5), plan_msg(11, 31.6), plan_msg(12, 31.7)],
        archive=[plan_msg(1, 3.0), plan_msg(2, 3.1), plan_msg(3, 3.2), plan_msg(4, 3.3), plan_msg(9, 31.6)],
    )
    page = thread(server, key=key)
    assert scanned_in(server) == [("INBOX", [1, 2]), ("Archive", [2, 3, 4]), ("INBOX", [12])]
    uncovered = [box for box, criteria in zip(scan_boxes(server), scans(server), strict=True) if b"BEFORE" in criteria]
    assert sorted(map(str, uncovered)) == ["Deleted Messages", "INBOX", "Sent Messages"]
    assert len(page.members) == 6
    assert exact(page) is False


def test_subject_key_seed_copies_merge_outgoing() -> None:
    """A copy of the seed joins as such whatever its participants, so the merge keeps one member, outgoing when a
    copy is in sent (A-mf3), here without an own-addresses file."""
    raw = hdr("Plan", sender=OWNER, to=BOB, message_id="<own@example.test>")
    server = icloud(inbox=[msg(1, 2 * DAY - 1, headers=raw)], sent=[msg(1, 2 * DAY, headers=raw)])
    key = ThreadKey("icloud", "subject", subject_digest("Plan") or "")
    page = thread(server, key=key, box=mailbox(server, own=frozenset()))
    assert linked(page) == [("own@example.test", "subject")]
    assert (page.members[0].summary.meta.folder, page.members[0].summary.meta.direction) == ("sent", "outgoing")


def test_non_gmail_seed_without_ids_or_subject_alone_not_exact() -> None:
    """P12, SG4: a seed without a valid Message-ID, References or In-Reply-To and with an empty normalised subject is
    returned alone, not exact, not truncated, `thread_key` null, and nothing is searched. With a subject, the
    fallback is its only search, and a complete scan leaves the count exact."""
    no_ids = b"From: " + ALICE.encode() + b"\r\nSubject: Re: \r\n\r\n"
    server = icloud(inbox=[msg(1, 5 * DAY, headers=no_ids), msg(2, 3 * DAY, headers=no_ids)])
    page = thread(server, seed=seed_in_inbox(server))
    assert [m.summary.ref for m in page.members] == [seed_in_inbox(server)]
    assert page.thread is None
    assert (page.truncated, exact(page)) == (False, False)
    assert searches(server) == []

    with_subject = b"From: " + ALICE.encode() + b"\r\nSubject: Plan\r\n\r\n"
    server = icloud(inbox=[msg(1, 5 * DAY, headers=with_subject), msg(2, 3 * DAY, headers=with_subject)])
    page = thread(server, seed=seed_in_inbox(server))
    assert linked(page) == [(None, "headers"), (None, "subject")]
    assert page.thread == ThreadKey("icloud", "subject", subject_digest("Plan") or "")
    assert header_searches(server) == []
    assert len(scans(server)) == 4
    assert (page.truncated, exact(page)) == (False, True)


def test_copy_of_the_anchor_keeps_its_link() -> None:
    """A copy of the anchor that only the scan finds (its round-(1) SEARCH answered NO) joins as a copy: the merged
    member keeps the anchor's `linked_by`, also when that copy wins the merge (S-e)."""
    root = hdr("Plan", sender=ALICE, message_id="<s@example.test>")
    server = icloud(inbox=[msg(1, 5 * DAY, headers=root)], archive=[msg(1, 5 * DAY + 1, headers=root)])
    original = server.run
    state: dict[str, object] = {"box": None, "done": False}

    def run(name: str, argument: object = None) -> None:
        if name == "EXAMINE":
            state["box"] = argument
        if not state["done"] and name == "UID SEARCH" and state["box"] == "Archive":
            state["done"] = True
            server.commands.append((name, argument))
            raise server.no(name)
        original(name, argument)

    server.run = run  # type: ignore[method-assign]
    page = thread(server, key=ThreadKey("icloud", "headers", "s@example.test"))
    assert linked(page) == [("s@example.test", "headers")]
    assert page.members[0].summary.meta.folder == "archive"  # the earlier copy
    assert (page.truncated, exact(page)) == (True, False)


def test_subject_scan_trims_a_cut_token() -> None:
    """T7-3: a scan block cut at 8 KiB is parsed without its last token, so a shortened address
    (`bob@example.te` of `bob@example.test`) is never a shared participant."""
    head = b"From: " + CAROL.encode() + b"\r\nSubject: Plan\r\nMessage-ID: <cut@example.test>\r\nCc: "
    tail = b", bob@example.te"
    local = b"a" * (imap_thread.SUBJECT_SCAN_BYTES - len(head) - len(tail) - len(b"@example.org"))
    raw = head + local + b"@example.org" + tail + b"st\r\n\r\n"
    seed = msg(1, 5 * DAY, "Plan", sender="Bob <bob@example.te>", message_id="<s@example.test>")
    server = icloud(inbox=[seed, msg(2, 4 * DAY, headers=raw)])
    page = thread(server, seed=seed_in_inbox(server))
    assert ids(page) == ["s@example.test"]
    assert exact(page) is True
