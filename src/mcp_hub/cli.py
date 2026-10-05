"""Owner commands inside the hub container (spec 080 §7.3, rev. 4.6 S9): `mcp-hub login <account-id>` (device-code
login for a Graph account). The user code and verification address go to this terminal only, never to the log; the
command refuses to run without a terminal so the code cannot land in a collected log."""

import logging
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

import httpx2

from mcp_hub.config import REGISTRY_FILE, ConfigError, load_settings
from mcp_hub.logging import configure_logging, log_event
from mcp_hub.providers import msidentity
from mcp_hub.providers.base import ProviderError, read_credential
from mcp_hub.providers.graph_auth import TokenStoreLike
from mcp_hub.providers.msidentity import GraphAccount, LoginFailedError
from mcp_hub.registry import GraphBlock, RegistryError, load_registry
from mcp_hub.tokenstore.crypto import KeyUnavailableError
from mcp_hub.tokenstore.store import LOGIN_WAIT_MS, StoreConfig, TokenStore, TokenStoreUnavailableError

USAGE = "usage: mcp-hub login <account-id>"
_log = logging.getLogger("mcp_hub.cli")
_MESSAGES = {
    "declined": "Sign-in was declined.",
    "expired": "The code expired before the sign-in finished. Run the command again.",
    "timeout": "No sign-in within the code's lifetime. Run the command again.",
    "refused": (
        "Microsoft refused the app registration (wrong client id, 'Allow public client flows' is off, "
        "or its account types exclude personal accounts)."
    ),
    "unexpected_host": "Microsoft answered with a sign-in address on an unexpected host; nothing was shown or stored.",
    "unreachable": "Microsoft's token endpoint could not be reached during the sign-in. Run the command again.",
    "error": "The sign-in failed. Run the command again; if it keeps failing, check the hub log.",
}


def main(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    client_factory: Callable[[float], httpx2.Client] = msidentity.default_client,
    store_factory: Callable[[StoreConfig], TokenStoreLike] = TokenStore,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    configure_logging("INFO")
    if len(argv) == 2 and argv[0] == "login":
        return _login(argv[1], env, out, err, client_factory, store_factory, sleep, clock)
    print(USAGE, file=err)
    return 2


def _login(
    account_id: str,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    client_factory: Callable[[float], httpx2.Client],
    store_factory: Callable[[StoreConfig], TokenStoreLike],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    if not out.isatty():
        print(
            "Run this command in an interactive terminal (kubectl exec -it …); the sign-in code must not end up "
            "in a log.",
            file=err,
        )
        return 2
    try:
        settings = load_settings(env)
        configure_logging(settings.log_level)
        registry = load_registry(settings.secrets_dir / REGISTRY_FILE)
    except (ConfigError, RegistryError):
        print("The hub configuration is invalid; see the hub's startup_failed log line.", file=err)
        return 2
    account = registry.get(account_id)
    if account is None:
        print("No account with that id.", file=err)
        return 2
    if not isinstance(account.mail, GraphBlock) or account.graph is None:
        print("That account does not use Microsoft Graph.", file=err)
        return 2
    store = store_factory(StoreConfig.from_settings(settings))
    if not store.configured():
        print("The token store is not configured (db-username, db-password or token-encryption-key missing).", file=err)
        return 2
    try:
        client_id = read_credential(settings.secrets_dir, account.graph.client_id_ref)
        cipher = store.cipher()
    except (ProviderError, KeyUnavailableError):
        print("The client id file or the encryption key is missing or unreadable.", file=err)
        return 2
    graph = GraphAccount(account.id, account.provider, account.graph.tenant, client_id, tuple(account.graph.scopes))
    try:
        with client_factory(msidentity.REQUEST_TIMEOUT_SECONDS) as client:
            code = msidentity.start_device_code(
                client, graph, deadline=clock() + msidentity.REQUEST_TIMEOUT_SECONDS, clock=clock
            )
            out.write(
                f"To sign in, open {code.verification_uri} in a private browser window and enter the code "
                f"{code.user_code}\nThe code is valid for {code.expires_in // 60} minutes. Before you approve, check "
                "that the app name is yours and that only mail read and 'Maintain access' are requested.\n"
            )
            out.flush()
            answer = msidentity.poll_device_code(client, graph, code, sleep=sleep, clock=clock)
    except LoginFailedError as exc:
        message = _MESSAGES[exc.reason]
        if exc.reason == "unexpected_host":
            message += f" Refused host: {exc.host or 'unprintable'}"
        print(message, file=err)
        # Spec §9.7 fixes the login outcomes: unexpected_host is logged as refused, unreachable as error.
        outcome = {"unexpected_host": "refused", "unreachable": "error"}.get(exc.reason, exc.reason)
        fields: dict[str, object] = {"account": account.id, "outcome": outcome}
        if exc.cause is not None:  # absent rather than null, like the other events
            fields["exception"] = exc.cause
        log_event(_log, logging.WARNING, "login", **fields)
        return 1
    try:
        sealed = cipher.seal(answer.refresh_token, account_id=account.id, provider=account.provider)
        with store.locked(account.id, wait_ms=LOGIN_WAIT_MS) as row:  # outwaits a refresh's 20 s hold (P4)
            row.replace_login(sealed, account.provider, answer.scope)
    except (TokenStoreUnavailableError, ValueError) as exc:
        print("Signed in, but the token could not be stored. Run the command again.", file=err)
        log_event(_log, logging.ERROR, "login", account=account.id, outcome="error", exception=type(exc).__name__)
        return 1
    log_event(_log, logging.INFO, "login", account=account.id, outcome="ok")
    out.write("Signed in. The hub uses the new token at its next refresh (within 30 minutes) or the next call.\n")
    return 0
