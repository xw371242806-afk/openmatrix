"""MCP tool gateway for the agent orchestration layer.

The orchestrator never imports ``mcp_server`` tool functions directly. Instead
it talks to the FastMCP server through the MCP protocol boundary
(``list_tools`` / ``call_tool``), exactly like an external MCP client would.
This keeps the agent layer transport-agnostic: the same ``ToolGateway`` protocol
can be satisfied by a live stdio client later, and tests use a mock gateway so
no external API is ever called.

Patent guardrail (red line): the agent allowlist only exposes
``document -> retrieval -> evidence -> conclusion`` tools. Matrix-run export
tools (``export_matrix_report`` / ``export_report``) operate on ``matrix_cells``
and are deliberately excluded so the agent can never trigger secondary cell
generation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """Static description of an MCP tool exposed to the agent."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Normalized outcome of a tool call.

    Tools return their payload as text (JSON or CSV). ``error`` carries a
    structured failure so the orchestrator can distinguish a successful call
    from a tool-level failure (e.g. missing run id).
    """

    text: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@runtime_checkable
class ToolGateway(Protocol):
    """Async boundary between the orchestrator and the MCP tool server."""

    async def list_tools(self) -> list[ToolSpec]:
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        ...


# The 13 analysis/data-source tools the agent is allowed to plan and call.
# Includes the P2 deterministic analysis tools (compute_financials / extract_kpis)
# which follow the document -> retrieval -> evidence -> conclusion pipeline and
# build no grid/cell structure. Explicitly excludes the two matrix-cell export tools.
AGENT_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    "retrieve_context",
    "fetch_context",
    "summarize_context",
    "analyze_documents",
    "verify_quote",
    "compute_financials",
    "extract_kpis",
    "sec_search",
    "sec_get_filing",
    "sec_get_xbrl_facts",
    "ak_stock_quote",
    "ak_financials",
    "macro_fred_lookup",
    "web_research",
    "web_crawl",
    "web_extract",
})

# Tools that touch matrix_cells; the agent must never call them.
MATRIX_CELL_TOOLS: frozenset[str] = frozenset({
    "export_matrix_report",
    "export_report",
})

_ALL_TOOLS = AGENT_TOOL_ALLOWLIST | MATRIX_CELL_TOOLS


def ext_allowlist() -> set[str]:
    """Externally allowed tool names from ``OPENMATRIX_EXT_ALLOWLIST``.

    Comma-separated ``ext_<server>_<tool>`` names the agent is permitted to
    plan/call from external MCP servers. The patent red line is enforced by
    only exposing tools listed here.
    """
    import os

    raw = os.environ.get("OPENMATRIX_EXT_ALLOWLIST", "")
    return {name.strip() for name in raw.split(",") if name.strip().startswith("ext_")}


def run_coro(coro):
    import asyncio

    return asyncio.run(coro)


def is_allowed_tool(name: str) -> bool:
    """True when ``name`` is a tool the agent is permitted to call.

    Includes external MCP tools explicitly listed in ``OPENMATRIX_EXT_ALLOWLIST``
    (``ext_<server>_<tool>`` names). The patent red line holds: only tools named
    there are exposed to the agent.
    """
    if name in AGENT_TOOL_ALLOWLIST:
        return True
    return name in ext_allowlist()


class McpToolGateway:
    """ToolGateway backed by the in-process FastMCP server (mcp_server.mcp).

    ``list_tools`` / ``call_tool`` delegate to the FastMCP server's protocol
    methods, which is the same surface an external MCP client speaks. Content
    blocks are normalized into plain text for the orchestrator.
    """

    def __init__(self, server: Any | None = None) -> None:
        if server is None:
            from mcp_server import mcp as server  # local import keeps the layer lazy
        self._server = server

    async def list_tools(self) -> list[ToolSpec]:
        tools = await self._server.list_tools()
        specs = [
            ToolSpec(
                name=tool.name,
                description=getattr(tool, "description", "") or "",
                input_schema=dict(getattr(tool, "inputSchema", {}) or {}),
            )
            for tool in tools
        ]
        # Aggregate external MCP servers (agent as an MCP client, C group).
        specs.extend(await self._external_list_tools())
        return specs

    async def _external_list_tools(self) -> list[ToolSpec]:
        try:
            from agent.mcp_clients import default_registry
        except Exception:  # noqa: BLE001 - external clients are optional
            return []
        registry = default_registry()
        if registry.is_empty():
            return []
        allowed = ext_allowlist()
        return await registry.list_tools(allowed=allowed)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not is_allowed_tool(name):
            return ToolResult(
                text="",
                error=f"tool {name!r} is outside the agent allowlist (red line)",
            )
        if name.startswith("ext_"):
            try:
                from agent.mcp_clients import default_registry

                return await default_registry().call_tool(name, arguments)
            except Exception:  # noqa: BLE001
                return ToolResult(text="", error="external tool call failed")
        try:
            content = await self._server.call_tool(name, arguments)
        except Exception as exc:  # ToolError and any underlying transport error
            return ToolResult(text="", error=f"tool call failed: {exc}")
        text = _content_to_text(content)
        # The MCP tools signal failures as "Error: ..." strings (e.g. missing
        # run id, empty pages). Normalize those to a structured failure so the
        # orchestrator can retry / degrade instead of treating them as success.
        if text.startswith("Error:"):
            return ToolResult(text="", error=text)
        return ToolResult(text=text)


def _content_to_text(content: Any) -> str:
    """Recursively extract plain text from MCP content blocks.

    The in-process FastMCP ``call_tool`` may return a bare string, a list of
    ``TextContent`` blocks, or a ``(content_blocks, structured)`` tuple. This
    normalizer flattens all of them deterministically.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            elif isinstance(item, (list, tuple)):
                nested = _content_to_text(item)
                if nested:
                    parts.append(nested)
            elif isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


_default_gateway: McpToolGateway | None = None


def default_gateway() -> McpToolGateway:
    """Return a lazily-created shared gateway to the in-process MCP server."""
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = McpToolGateway()
    return _default_gateway
