# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (CI and the image pin 0.12.19; another local patch version works)
- Python 3.13 (`.python-version`)
- Docker (image build, `scripts/smoke_image.sh` and the provider test containers)
- Optional: `actionlint`, `kubectl` and `conftest` for the workflow and manifest checks

## Local environment

All tool state stays inside the checkout, so a working copy can be removed without leaving anything behind:

- The project virtual environment is `.venv/` in the checkout (`uv sync --locked`).
- Agents working in a worktree keep the uv cache, uv tool/python directories and `TMPDIR` under `.local/` (gitignored) via a `.local/env.sh` that exports `UV_CACHE_DIR`, `UV_TOOL_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_DOWNLOADS=never`, `UV_PROJECT_ENVIRONMENT` and `TMPDIR`, and source it before every command.
- No global or user-level installs (`pip install --user`, `uv tool install`, `uv python install`, Homebrew).
- Cleanup: remove the worktree with `git worktree remove` (it removes the ignored `.venv/` and `.local/` with it) and the local images built for the work package; shared caches are left alone.

## Local development

```bash
uv sync --locked                           # install the locked dependencies into .venv
PYTHONPATH=src uv run python -m mcp_hub   # needs HUB_SECRETS_DIR with accounts.json and allowed-subjects
docker build -t mcp-hub:dev . && scripts/smoke_image.sh mcp-hub:dev
```

Dependencies are exact `==` pins in `pyproject.toml` with hashes in `uv.lock`. After editing `pyproject.toml`, run `uv lock`. New packages need the owner's approval (spec 080 §9.3).

## Project structure

```
src/mcp_hub/
  __main__.py      entry point: one uvicorn server on ports 8083 and 8084
  app.py           ASGI composition: SDK MCP app, challenge wrapper, request log, health app, port dispatcher
  auth.py          token verifier (spec §4.3 rows 1-8) and subject allowlist
  jwks.py          JWKS cache with refetch throttling
  config.py        environment settings
  registry.py      accounts.json validation and capability filtering
  health.py        in-memory per-capability status
  errors.py        tool error codes
  logging.py       JSON logs, logger levels
  sanitize.py      untrusted-content rules and field limits (spec §5.3, §5.4)
  budget.py        output budget (spec §5.4)
  ids.py           opaque message and event ids (spec §5.1)
  cursors.py       signed paging cursors (spec §5.5)
  own_addresses.py own-address lookup for the registry
  providers/       base.py (deadlines, connection limits, inbound limits), deadline.py, folders.py (folder model), headers.py, imap_guard.py, imap_search.py, mime.py, imap.py (read-only IMAP adapter), caldav.py (read-only CalDAV adapter and recurrence expansion), graph.py (read-only Graph mail adapter), graph_auth.py, msidentity.py
  tools/           list_accounts, mail (list_unread, list_messages, get_message); common.py: account selection and aggregation
tests/
  unit/            per-module tests
  contract/        gate G6: the authorization rules of spec §10.3 over HTTP
  integration/     the real process on two ports; test_imap.py against GreenMail, test_imap_history.py (mail history and the `get_message_status` scenarios, GreenMail), test_caldav.py against Radicale (marker `provider`)
  support/         keys, tokens, clock, MCP wire helpers, GreenMail and Radicale helpers, IMAP fake, ICS fixture loader, canned DAV answers, log capture
  fixtures/        example registry (neutral ids, no real data), ics/ calendar fixtures, radicale/ test server config
scripts/           dev_token.py, smoke_image.sh, html_scaling_check.py, provider_services.sh, memory_probe.py, imap_probe.py (development and owner probes, not in the image)
k8s/               Deployment and Service for namespace apps
```

## Running tests

```bash
uv run pytest tests/unit -v
uv run pytest tests/contract -v      # gate G6
uv run pytest tests/integration -v   # starts python -m mcp_hub as a subprocess
```

### Memory probe

`scripts/memory_probe.py` measures `get_events` memory and CPU against an in-process fake CalDAV server (synthetic data only, no network). Run it in the production image under the pod's limits; the repository is mounted read-only and the script is not part of the image:

```bash
docker build -t mcp-hub:dev .
docker run --rm --memory=320m --memory-swap=320m --cpus=1 --read-only --tmpfs /tmp \
  -v "$PWD":/work:ro -e PYTHONPATH=/app/src:/work mcp-hub:dev \
  python /work/scripts/memory_probe.py --scenario nonascii-240k --calls 10
```

It prints peak RSS and CPU per call, then the container's cgroup `memory.peak` and `memory.events`; `--accounts 2` queries two accounts concurrently. Re-run the scenarios in INTERFACES.md's memory table after any change to the calendar limits.

`--scenario graph-message` drives `get_message` for an `outlook` entry through the real Graph adapter and token source (in-memory token store, in-process Graph transport): the message answer fills the 2 MiB cap with a non-ASCII body and 5,000 recipients, the attachment answer fills the 1 MiB list cap. `--with-calendar <scenario>` runs that calendar scenario concurrently in the same process; the gate for the Graph limits is `--scenario graph-message --with-calendar nonascii-240k --calls 10` with cgroup `memory.peak` at most 290 MiB (spec 080 D86; 230 MiB at the former 256 Mi limit) and `oom_kill 0`. Record both cgroup `memory.peak` and the last call's peak RSS: the cgroup figure also counts page cache and varies between runs and hosts, while peak RSS is the process alone. Re-run it after any change to the Graph response caps.

`--scenario status` drives the IMAP `get_message_status` path for 50 refs through an in-process IMAP fake: every location, reply and thread SEARCH answers 500 hits with hostile header blocks (8 KiB in the 500-UID location chunks, 64 KiB in the 50-UID lookup chunks). `--accounts 2 --parallel 2 --calls 10`, alone and with `--with-calendar nonascii-240k`, is the gate (cgroup `memory.peak` at most 290 MiB (spec 080 D86; 230 MiB at the former 256 Mi limit), `oom_kill 0`). Re-run it after any change to the status caps (`MAX_LOOKUP_HITS`, `LOOKUP_FETCH_UIDS`, the header item sizes).

`--scenario thread` and `--scenario thread-graph` drive `get_thread` through the IMAP fake and the in-process Graph transport: `thread` alternates a header-round thread with a subject-fallback thread (hostile 500-hit searches, 64 KiB bodies, 10,000-part BODYSTRUCTUREs; `--structure-parts` sets the part count), `thread-graph` the 2 MiB `uniqueBody` list or a refused list with a 2 MiB fallback list, then 20 single reads of about 2 MiB. `--accounts 2 --parallel 2 --calls 10`, alone and with `--with-calendar nonascii-240k`, is the gate (cgroup `memory.peak` at most 290 MiB, `oom_kill 0`, under the 320 Mi limit; session-lead gate of 2026-10-27 (spec 080 D86), derived from the owner's 245 MiB ceiling of 2026-10-23 at 256 Mi; the measurement at `--memory=320m` is pending). Re-run it after any change to the thread caps (hit, chunk and body sizes, the BODYSTRUCTURE chunk).

`--scenario list` drives `list_messages` (folder `all`) and `list_unread`, 50 items each, through the IMAP fake: the inbox holds 50 unread messages with 64 KiB summary headers and 10,000-part BODYSTRUCTUREs (`--structure-parts`) among 5,000 candidates. `--accounts 2 --parallel 2 --calls 10`, alone and with `--with-calendar nonascii-240k`, is the measurement (cgroup `memory.peak` ≤ 290 MiB at `--memory=320m`, spec 080 D86; `oom_kill` 0; run the image with 320m for this scenario). `--failing-every <n>` (default 5) makes every n-th structure fail its reduction. `--summary-chunk <n>` overrides `SUMMARY_STRUCTURE_CHUNK_UIDS` to compare chunk sizes. Re-run it after any change to that constant or to what pass 2 of the list tools holds per message.

`--scenario search` drives `search_messages` (folder `all`, three words, one non-ASCII) for `icloud` accounts through the IMAP fake: every SEARCH answers 5,001 hits per folder (one more than the cap) with hostile 8 KiB candidate rows, and the 50 chosen items carry 64 KiB summary headers, 4 KiB snippets and 10,000-part BODYSTRUCTUREs (`--structure-parts`). `--scenario search-graph` drives it for `--accounts` Graph accounts through the in-process transport, alternating a word search (a refused over-cap answer, then the 1 MiB answer of the halved `$top`) with a filter-only search whose read-on takes 5 list requests of 1 MiB per folder. `--long-from` gives every bulk hit of `search` an 8 KiB From (CPU heavy; the search returns at about 16.2-16.5 s with the calendar, the batch wall time is longer because the concurrent `get_events` call finishes later on one CPU). The gate is the same as for `list`: `--accounts 2 --parallel 2 --calls 10`, alone and with `--with-calendar nonascii-240k`, cgroup `memory.peak` ≤ 290 MiB and `oom_kill` 0 at `--memory=320m`; results in INTERFACES.md.

### IMAP probe

`scripts/imap_probe.py --host <imap host>` is run by the owner on their own machine. It is read-only (an allowlist proxy, `EXAMINE` only), asks for the credentials at a prompt and writes nothing. It prints mailbox roles, counts and LIST/EXAMINE/SEARCH timings, including the bracketed Message-ID search (O35). Paste only the part after `--- result ---`; `--show-names` adds mailbox names, which are private.

`--status-rounds` runs the `get_message_status` rounds P1–P7 instead (WP13b): reply-header matching, OR width and timing, Gmail `X-GM-THRID` ORs, re-`EXAMINE` and header-`FETCH` cost. Every command goes through the hub's `GuardedImapClient` and every criteria through `imap_search`; the output is yes/no, counts and milliseconds only. Run it once per host (`uv run python scripts/imap_probe.py --host imap.mail.me.com --status-rounds`, then `--host imap.gmail.com`); `--help` starts with the owner's usage lines.

`--search-rounds` runs the `search_messages` rounds S1-S8 instead (WP13d). It connects with the hub's own `HubImapClient`, so a non-ASCII value goes out as a synchronising literal (`{N}`, never `{N+}`) exactly as in production, and every command goes through `GuardedImapClient`: only `EXAMINE`, `UID SEARCH` and one `UID FETCH` of `BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]<0.2048>` (a count of encoded words, nothing printed) reach the server. Run it from your own checkout or from the work-package worktree, once per host:

```bash
uv sync --locked
uv run python scripts/imap_probe.py --host imap.mail.me.com --search-rounds
uv run python scripts/imap_probe.py --host imap.gmail.com --search-rounds
```

Credentials are never passed on the command line. The username and the app-specific password are typed at hidden prompts (the same as the other modes), followed by hidden prompts for the words; none of them is echoed, printed or stored:

1. Three words (ASCII letters only, at least 7 letters each) that occur in inbox mail of the last 30 days: ideally words that appear in some subjects and in some bodies, so the hit counts differ. Prefer words that are not substrings of each other. Words 1 and 2 must both occur in at least one inbox mail, not next to each other (S7 compares the phrase with two separate keys; otherwise both counts are 0 and `s7_hint: decisive=no` is printed).
2. Optional (Enter skips): a sender local part of at least 7 characters (the part before the `@` of someone who wrote to you in the last 30 days).
3. Optional: a word with a non-ASCII letter (for example `Überraschung`) that occurs in the subject of an inbox mail of the last 30 days (S4/S5 run on the inbox). Without it S4/S5 are skipped.
4. Optional, Gmail only: a fragment of an address that appears only in the Cc of some mail in the last 30 days (S8). A word that is refused is asked again (three attempts) with a fixed message.

Paste back only the part after the `--- result ---` line. It holds capability flags, hit counts, `status=ok|no|bad` and milliseconds, with folders named by role (`inbox`, `sent`, `archive`, `all_mail`); it contains no word, address, header or mailbox name. A full run on Gmail can take a few minutes (`TEXT` searches are slow). Folders are found by special-use attribute, else by the hub's fallback names (iCloud has no `\Archive` attribute); a role that is still missing prints `s2_folder: role=<role> skipped=not_found`. If a round prints `error=TimeoutError`, every later line is void (the socket is unusable): paste the output anyway and run it again. `s4_literal: status=no code=badcharset` separates a charset refusal from other `NO` replies. Rounds: S1 capabilities; S2 `TEXT` with one word, three keys, prefix and middle fragment, `SUBJECT` and `FROM`; S3 (Gmail) Gmail operators inside a quoted value (`in:anywhere`, `-word`, `OR`, `w1 OR w2`) with the window size and a `neg_complement`/`or_union`/`in_anywhere_eq_window` hint (approximate, the counts decide); S6 `FROM "utf-8"` and `SUBJECT "=?utf"` plus the control over the newest 500 window rows (`encoded_from`/`encoded_subject` count utf-8 encoded words only; `*_hits_in_scanned` is the number of S6 hits inside those rows: raw matching = hits_in_scanned near the control, decoded matching = control above 0 and hits_in_scanned near 0); S7 phrase against two keys (with `s7_hint`); S8 (Gmail) `TO` against Cc; S4/S5 (last, because a refused literal can leave the connection unusable) `SUBJECT <non-ASCII word>` and its upper-cased form. Every search carries `NOT DRAFT SINCE <today - 30 days>`.

### Provider integration tests

The IMAP adapter is tested against a local GreenMail container, the CalDAV adapter against a local Radicale container and the token store against PostgreSQL 17 (the cluster's image, port 5433; the tests run as a non-superuser role that owns database `mcp_hub`, as production does) — all pinned by digest. `scripts/provider_services.sh up` starts all three on `127.0.0.1` with neutral `example.test` users and passwords generated per run (written to `$HUB_PROVIDER_STATE_DIR` or `$TMPDIR`, never committed; Radicale reads a per-run copy of `tests/fixtures/radicale/config`); `down` stops and removes them. The calendar fixtures in `tests/fixtures/ics/` (DST change, overrides, `EXDATE`, `RDATE`, cancellations, all-day, floating, cross-zone, window edge, hostile text) are checked in unit tests and again through Radicale; `tests/support/ics.py` loads them with CRLF line endings and turns the `{ZWSP}` placeholder into a zero-width space, so no invisible character is committed.

```bash
scripts/provider_services.sh up
HUB_PROVIDER_TESTS=1 uv run pytest -m provider -v
scripts/provider_services.sh down
```

Without `HUB_PROVIDER_TESTS=1` the provider tests are skipped; CI runs them in the separate `providers` job, and `lint-and-test` runs `pytest -m "not provider"`. TLS is verified against the certificate the local container presents (pinned per run); only the host-name check is off in the test helper.

The contract tests mint real RS256 tokens with a key generated per test session and serve the JWKS with pytest-httpserver; they send the 2026-07-28 wire format (headers `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` and `params._meta`) and the 2025-11-25 handshake. Each row of spec §10.3 maps to one test; the map is in [INTERFACES.md](INTERFACES.md) §1.

## Process

- Six-phase workflow with a worklog per change (`.claude/rules/workflow.md`).
- Branch `feat/<issue>-<slug>` (or `fix/…`, `chore/…`), conventional commits, one pull request per change against `main`.
- Review: the Copilot review requested by the repository ruleset; if its quota is exhausted, the substitute review path (reviewer agent + `/code-review`) is used and disclosed in the pull request.
- Every review comment is fixed or answered before the merge; merging needs the owner's go.

## Code review checklist

See `.claude/rules/review-guidelines.md`. In short: no secrets or request data in logs, gate G6 green, exact pins, read-only tools, third-party strings only inside `untrusted` objects, minimal diffs with tests.

## Developer reminders

- Log only the fields of spec §9.7 through `log_event`; never tokens, `Authorization` values, header values, provider URLs, addresses or content.
- Actions are pinned by commit SHA, images by digest; no `latest`.
- This repository is public: no account identifiers, usernames, addresses or real data in code, tests, fixtures, docs or commit messages.
