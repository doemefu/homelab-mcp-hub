"""Account registry accounts.json (spec 080 §8.2) and capability filtering (§5.1)."""

from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from mcp_hub.errors import ToolError

Capability = Literal["mail", "calendar"]
CAPABILITIES: Final[tuple[Capability, ...]] = ("mail", "calendar")
Protocol = Literal["imap", "caldav", "ics", "graph"]
Provider = Literal["icloud", "google", "microsoft", "microsoft-org", "ics"]
AccountId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,31}$")]
# Keys of mcp-hub-secrets that are not account credentials (spec 080 §7.1); a *_ref must not point at them.
RESERVED_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "accounts.json",
        "allowed-subjects",
        "db-username",
        "db-password",
        "token-encryption-key",
        "token-encryption-key-previous",
    }
)


def _not_reserved(ref: str) -> str:
    if ref in RESERVED_SECRET_KEYS:
        raise ValueError("reference names a reserved key")
    return ref


# A file name inside HUB_SECRETS_DIR (spec §7.1 key pattern): no separators, and the leading alphanumeric character
# excludes ".", ".." and the Secret volume's "..data" entries, so it cannot leave the directory.
SecretRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$"), AfterValidator(_not_reserved)]
Tenant = Annotated[str, StringConstraints(pattern=r"^(consumers|organizations|[0-9a-f-]{36})$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImapMail(_Strict):
    protocol: Literal["imap"]
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    port: Annotated[int, Field(ge=1, le=65535)]
    username_ref: SecretRef
    password_ref: SecretRef
    inbox: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "INBOX"


class CalDavCalendar(_Strict):
    protocol: Literal["caldav"]
    url: Annotated[str, StringConstraints(pattern=r"^https://[^\s/]+(/\S*)?$")]
    username_ref: SecretRef
    password_ref: SecretRef
    include_calendars: (
        Literal["all"] | Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)]
    )


class IcsCalendar(_Strict):
    protocol: Literal["ics"]
    url_ref: SecretRef


class GraphBlock(_Strict):
    protocol: Literal["graph"]


class GraphSettings(_Strict):
    tenant: Tenant
    client_id_ref: SecretRef
    scopes: Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)]


class Capabilities(_Strict):
    mail: bool
    calendar: bool


MailBlock = Annotated[ImapMail | GraphBlock, Field(discriminator="protocol")]
CalendarBlock = Annotated[CalDavCalendar | IcsCalendar | GraphBlock, Field(discriminator="protocol")]


class Account(_Strict):
    id: AccountId
    label: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    provider: Provider
    enabled: bool
    capabilities: Capabilities
    mail: MailBlock | None = None
    calendar: CalendarBlock | None = None
    graph: GraphSettings | None = None

    @model_validator(mode="after")
    def _blocks_match_capabilities(self) -> Self:
        for name, flag, block in (
            ("mail", self.capabilities.mail, self.mail),
            ("calendar", self.capabilities.calendar, self.calendar),
        ):
            if flag and block is None:
                raise ValueError(f"capability {name} is true but its block is missing")
            if not flag and block is not None:
                raise ValueError(f"capability {name} is false but a block is present")
        uses_graph = any(isinstance(b, GraphBlock) for b in (self.mail, self.calendar))
        if uses_graph and self.graph is None:
            raise ValueError("graph settings are required for protocol graph")
        return self

    def has(self, capability: Capability) -> bool:
        return self.capabilities.mail if capability == "mail" else self.capabilities.calendar

    def _block(self, capability: Capability) -> ImapMail | CalDavCalendar | IcsCalendar | GraphBlock | None:
        return self.mail if capability == "mail" else self.calendar

    def protocol(self, capability: Capability) -> Protocol:
        block = self._block(capability)
        if block is None:
            raise KeyError(capability)
        return block.protocol

    def credential_refs(self, capability: Capability) -> tuple[str, ...]:
        block = self._block(capability)
        if isinstance(block, ImapMail | CalDavCalendar):
            return (block.username_ref, block.password_ref)
        if isinstance(block, IcsCalendar):
            return (block.url_ref,)
        if isinstance(block, GraphBlock) and self.graph is not None:
            return (self.graph.client_id_ref,)
        return ()


class Registry(_Strict):
    version: Literal[1]
    accounts: list[Account]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account ids must be unique")
        return self

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)


class RegistryError(ValueError):
    """accounts.json is missing or invalid; the message lists locations and error types only."""


def load_registry(path: Path) -> Registry:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError("accounts.json is missing or unreadable") from exc
    try:
        return Registry.model_validate_json(raw)
    except ValidationError as exc:
        raise RegistryError(f"accounts.json is invalid: {_describe(exc)}") from None


def _describe(exc: ValidationError) -> str:
    """Schema paths and error types only. Unknown keys are operator-typed text, so they are never echoed (§9.7)."""
    problems: list[str] = []
    unknown: dict[str, int] = {}
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        if err["type"] == "extra_forbidden":
            parent = ".".join(str(p) for p in err["loc"][:-1])
            unknown[parent] = unknown.get(parent, 0) + 1
            continue
        problems.append(f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}")
    for parent, count in unknown.items():
        location = f"{parent}.<unknown key>" if parent else "<unknown key>"
        problems.append(f"{location} x{count}: extra_forbidden")
    return "; ".join(problems)


def select_accounts(registry: Registry, capability: Capability, account: str | None) -> list[Account]:
    if account is None:
        return [a for a in registry.accounts if a.enabled and a.has(capability)]
    found = registry.get(account)
    if found is None:
        raise ToolError("unknown_account", "No account with that id")
    if not found.has(capability):
        raise ToolError("capability_unavailable", f"Account has no {capability} capability")
    if not found.enabled:
        raise ToolError("capability_unavailable", "Account is disabled")
    return [found]
