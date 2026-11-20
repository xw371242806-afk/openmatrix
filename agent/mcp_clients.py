"""External MCP client registry (C: agent as an MCP client).

Lets the orchestrator connect to third-party MCP servers (Tavily, Playwright,
GitHub, postgres-mcp, ...) and call their tools through the same
``ToolGateway`` surface. Each registered server's tools are exposed with an
``ext_<server>_<tool>`` prefix so names never collide with the built-in MCP
tools, and every external tool still passes the agent allowlist (patent red
line) before it can be planned or called.

Servers are configured via the ``OPENMATRIX_MCP_SERVERS`` env var: a
JSON list of {"name", "command", "args", "env"} (stdio) or
{"name", "url"} (streamable HTTP). Connections are lazy and per-server;
``list_tools`` / ``call_tool`` aggregate across all connected servers.

Correctly handles the real MCP SDK shapes: ``list_tools`` returns a
``ListToolsResult`` (tools under ``.tools``); ``call_tool`` returns a
``CallToolResult`` (content blocks under ``.content``, failure under
``.isError``). Transports are entered as context managers and unwound on
close. Errors are sanitized (never leak command paths / URLs / credentials).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import os
import re
from typing import Any

from agent.tools import ToolResult, ToolSpec, ext_allowlist

# A server name must be an identifier that cannot collide with the routing
# separator: letters, digits, underscore and dash are fine, but the tool
# suffix is split on the LAST underscore so a server name may contain
# underscores only if routing resolves via known prefixes (see _unprefix).
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


class McpClientSpec:
    """Configuration for one external MCP server."""

    def __init__(self, name: str, *, command: str | None = None, args: list[str] | None = None,
                 url: str | None = None, env: dict[str, str] | None = None) -> None:
        if not name or not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
            raise ValueError(
                "name must be 1-48 chars of [a-zA-Z0-9_-] and non-empty"
            )
        if bool(command) == bool(url):
            raise ValueError("specify exactly one of command (stdio) or url (http)")
        self.name = name
        self.command = command
        self.args = args or []
        self.url = url
        self.env = env or {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "McpClientSpec":
        return cls(
            name=d["name"],
            command=d.get("command"),
            args=d.get("args"),
            url=d.get("url"),
            env=d.get("env"),
        )


class McpClientRegistry:
    """Registry + aggregated ToolGateway view over external MCP servers.

    Lazy: a server is connected on first use. ``list_tools`` returns every
    external tool (prefixed ``ext_<server>_<tool>``) that is present in the
    ``allowed`` set (the agent ext allowlist). ``call_tool`` routes by known
    server prefix. Failures surface as sanitized ``ToolResult`` errors, never
    leak transport internals.
    """

    def __init__(self, specs: list[McpClientSpec] | None = None) -> None:
        self._specs = specs or []
        self._sessions: dict[str, tuple[Any, AsyncExitStack]] = {}  # name -> (session, exit_stack)
        self._lock = asyncio.Lock()

    @property
    def specs(self) -> list[McpClientSpec]:
        return self._specs

    def is_empty(self) -> bool:
        return not self._specs

    async def _connect(self, spec: McpClientSpec) -> Any:
        """Enter transport + session context managers; return the session."""
        if spec.name in self._sessions:
            return self._sessions[spec.name][0]
        async with self._lock:
            if spec.name in self._sessions:
                return self._sessions[spec.name][0]
            session, stack = await self._open(spec)
            self._sessions[spec.name] = (session, stack)
            return session

    async def _open(self, spec: McpClientSpec) -> tuple[Any, AsyncExitStack]:
        from mcp import ClientSession

        stack = AsyncExitStack()
        try:
            if spec.command:
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                params = StdioServerParameters(
                    command=spec.command, args=spec.args, env=spec.env or None
                )
                transport_ctx = stdio_client(params)
            else:
                from mcp.client.streamable_http import streamablehttp_client

                transport_ctx = streamablehttp_client(spec.url)

            streams = await stack.enter_async_context(transport_ctx)
            read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return session, stack
        except Exception:
            await stack.aclose()
            raise

    def _prefixed(self, server: str, tool: str) -> str:
        return f"ext_{server}_{tool}"

    def _unprefix(self, name: str) -> tuple[str | None, str | None]:
        """Resolve an ``ext_`` name back to (server, tool).

        Routing matches the longest configured server name prefix so server
        names containing ``_`` stay unambiguous.
        """
        if not name.startswith("ext_"):
            return None, None
        rest = name[4:]
        servers = sorted((s.name for s in self._specs), key=len, reverse=True)
        for server in servers:
            prefix = f"{server}_"
            if rest.startswith(prefix):
                tool = rest[len(prefix):]
                return server, tool if tool else None
        return None, None

    async def list_tools(self, allowed: set[str] | None = None) -> list[ToolSpec]:
        allowed_set = ext_allowlist() if allowed is None else allowed
        if not allowed_set:
            return []
        out: list[ToolSpec] = []
        for spec in self._specs:
            try:
                session = await self._connect(spec)
                result = await session.list_tools()
                for tool in getattr(result, "tools", []) or []:
                    name = self._prefixed(spec.name, tool.name)
                    if name not in allowed_set:
                        continue
                    out.append(ToolSpec(
                        name=name,
                        description=f"[ext {spec.name}] {getattr(tool, 'description', '') or ''}",
                        input_schema=dict(getattr(tool, "inputSchema", {}) or {}),
                    ))
            except Exception:  # noqa: BLE001 - sanitized; never leak internals
                # A failed server contributes nothing; the allowlist red line is
                # not weakened by an "unavailable" pseudo-tool.
                continue
        return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        allowed: set[str] | None = None,
    ) -> ToolResult:
        allowed_set = ext_allowlist() if allowed is None else allowed
        if name not in allowed_set:
            return ToolResult(text="", error=f"tool {name!r} is outside the external allowlist (red line)")
        server, tool = self._unprefix(name)
        if server is None or tool is None:
            return ToolResult(text="", error=f"unknown external tool {name!r}")
        spec = next((s for s in self._specs if s.name == server), None)
        if spec is None:
            return ToolResult(text="", error=f"unknown external server {server!r}")
        try:
            session = await self._connect(spec)
            result = await session.call_tool(tool, arguments)
            is_error = bool(getattr(result, "isError", False))
            text = _extract_tool_result_text(result)
            if is_error or text.startswith("Error:"):
                return ToolResult(text="", error=f"external tool {name!r} failed")
            return ToolResult(text=text)
        except Exception:  # noqa: BLE001 - sanitized for the orchestrator
            return ToolResult(text="", error=f"external tool {name!r} failed")

    async def close_all(self) -> None:
        for name in list(self._sessions):
            session, stack = self._sessions.pop(name)
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass


def _extract_tool_result_text(result: Any) -> str:
    """Extract and deterministically format text/JSON content from a CallToolResult."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False, sort_keys=True)
        return str(structured)
    return _content_to_text(getattr(result, "content", []))


def _content_to_text(content: Any) -> str:
    """Normalize MCP content blocks (list of TextContent / dicts / objects) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            elif isinstance(item, str):
                parts.append(item)
            else:
                text = getattr(item, "text", None)
                if text is not None:
                    parts.append(str(text))
                elif hasattr(item, "model_dump"):
                    parts.append(json.dumps(item.model_dump(), ensure_ascii=False, sort_keys=True))
                elif hasattr(item, "__dict__"):
                    parts.append(json.dumps(vars(item), ensure_ascii=False, sort_keys=True))
                else:
                    parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def parse_server_config(raw: str | None) -> list[McpClientSpec]:
    """Parse ``OPENMATRIX_MCP_SERVERS`` JSON into McpClientSpec list.

    Invalid entries are skipped (a broken spec must not block the others).
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    specs = []
    for entry in data:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        try:
            specs.append(McpClientSpec.from_dict(entry))
        except (ValueError, KeyError, TypeError):
            continue
    return specs


_default_registry: McpClientRegistry | None = None


def default_registry(specs: list[McpClientSpec] | None = None) -> McpClientRegistry:
    """Module-level default registry (lazy; from env when specs omitted)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = McpClientRegistry(
            specs if specs is not None else parse_server_config(os.environ.get("OPENMATRIX_MCP_SERVERS"))
        )
    return _default_registry
