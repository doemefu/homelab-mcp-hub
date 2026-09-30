"""MCP tools. Each domain module exposes register(server, ctx)."""

from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer

from mcp_hub.config import Settings
from mcp_hub.health import StatusStore
from mcp_hub.registry import Registry


@dataclass(frozen=True, slots=True)
class HubContext:
    settings: Settings
    registry: Registry
    status: StatusStore


def register_tools(server: MCPServer, ctx: HubContext) -> None:
    from mcp_hub.tools import accounts

    accounts.register(server, ctx)
