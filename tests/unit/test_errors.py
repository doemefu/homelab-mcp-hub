import json

from mcp.types import TextContent

from mcp_hub.errors import ToolError


def test_tool_error_result_is_structured_json() -> None:
    result = ToolError("unknown_account", "No account with that id").to_result()
    assert result.is_error is True
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert json.loads(content.text) == {"code": "unknown_account", "message": "No account with that id"}
