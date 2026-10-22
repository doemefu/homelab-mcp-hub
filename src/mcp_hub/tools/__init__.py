"""MCP tools. Each domain module exposes register(server, ctx)."""

from dataclasses import dataclass, field

from mcp.server.mcpserver import MCPServer

from mcp_hub.config import Settings
from mcp_hub.cursors import CursorCodec
from mcp_hub.health import StatusStore
from mcp_hub.providers import Adapters
from mcp_hub.registry import Registry


@dataclass(frozen=True, slots=True)
class HubContext:
    settings: Settings
    registry: Registry
    status: StatusStore
    adapters: Adapters = field(default_factory=Adapters)
    cursors: CursorCodec = field(default_factory=CursorCodec)  # random key per process: a restart voids cursors


def register_tools(server: MCPServer, ctx: HubContext) -> None:
    from mcp_hub.tools import accounts, calendar, mail, mail_status, mail_thread

    accounts.register(server, ctx)
    mail.register(server, ctx)
    mail_status.register(server, ctx)
    mail_thread.register(server, ctx)
    calendar.register(server, ctx)
