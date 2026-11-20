"""Tests for C: agent/mcp_clients.py (external MCP client registry)."""

import asyncio
import unittest
from unittest import mock

from agent.mcp_clients import (
    McpClientRegistry,
    McpClientSpec,
    parse_server_config,
)


def run_coro(coro):
    return asyncio.run(coro)


class FakeTool:
    """SDK-compatible tool object (has .name/.description/.inputSchema)."""

    def __init__(self, name, description="d", input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object"}


class FakeListResult:
    """SDK-compatible ListToolsResult (tools under .tools)."""

    def __init__(self, tools):
        self.tools = tools


class FakeText:
    """SDK-compatible TextContent block."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeCallResult:
    """SDK-compatible CallToolResult (content under .content, isError flag)."""

    def __init__(self, text, is_error=False):
        self.content = [FakeText(text)]
        self.isError = is_error
        self.structuredContent = None


class FakeSession:
    """Minimal ClientSession-like double using SDK result shapes."""

    def __init__(self, tools):
        self._tools = tools

    async def initialize(self):
        pass

    async def list_tools(self):
        return FakeListResult(self._tools)

    async def call_tool(self, name, arguments):
        return FakeCallResult(f"called {name} {arguments}")


class McpClientSpecTests(unittest.TestCase):
    def test_command_required_xor_url(self):
        self.assertIsNotNone(McpClientSpec("a", command="x"))
        self.assertIsNotNone(McpClientSpec("a", url="http://x"))
        with self.assertRaises(ValueError):
            McpClientSpec("a", command="x", url="http://x")
        with self.assertRaises(ValueError):
            McpClientSpec("a")

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError):
            McpClientSpec("", command="x")
        with self.assertRaises(ValueError):
            McpClientSpec("bad name!", command="x")
        with self.assertRaises(ValueError):
            McpClientSpec("x" * 60, command="x")

    def test_underscore_name_allowed(self):
        self.assertIsNotNone(McpClientSpec("my_git", command="npx"))

    def test_from_dict(self):
        spec = McpClientSpec.from_dict({"name": "g", "command": "npx", "args": ["-y", "x"], "env": {"K": "V"}})
        self.assertEqual("npx", spec.command)
        self.assertEqual(["-y", "x"], spec.args)
        self.assertEqual({"K": "V"}, spec.env)


class ParseServerConfigTests(unittest.TestCase):
    def test_parse_valid(self):
        specs = parse_server_config('[{"name":"g","command":"npx"},{"name":"t","url":"http://t"}]')
        self.assertEqual(2, len(specs))
        self.assertEqual("g", specs[0].name)
        self.assertEqual("http://t", specs[1].url)

    def test_parse_invalid_entries_skipped(self):
        specs = parse_server_config('[{"name":"g","command":"npx"},{"name":"bad"},{"foo":1}]')
        self.assertEqual(1, len(specs))

    def test_parse_empty(self):
        self.assertEqual([], parse_server_config(None))
        self.assertEqual([], parse_server_config("not json"))
        self.assertEqual([], parse_server_config("{}"))


class McpClientRegistryTests(unittest.TestCase):
    def test_prefix_unprefix_basic(self):
        reg = McpClientRegistry([McpClientSpec("git", command="x")])
        self.assertEqual("ext_git_list_issues", reg._prefixed("git", "list_issues"))
        self.assertEqual(("git", "list_issues"), reg._unprefix("ext_git_list_issues"))
        self.assertEqual((None, None), reg._unprefix("builtin_tool"))
        self.assertEqual((None, None), reg._unprefix("ext_"))

    def test_prefix_unprefix_server_with_underscore(self):
        # server name contains '_' -> longest-prefix match resolves it
        reg = McpClientRegistry([McpClientSpec("my_git", command="x")])
        self.assertEqual(("my_git", "search"), reg._unprefix("ext_my_git_search"))

    def test_list_tools_prefixed_and_filtered(self):
        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])
        fake_tool = FakeTool("search")

        async def drive():
            reg._sessions["demo"] = (FakeSession([fake_tool]), None)
            default_empty = await reg.list_tools()
            filtered = await reg.list_tools(allowed={"ext_demo_search"})
            disallowed = await reg.list_tools(allowed=set())
            return default_empty, filtered, disallowed

        default_empty, filtered, disallowed = run_coro(drive())
        self.assertEqual([], default_empty)
        self.assertEqual(1, len(filtered))
        self.assertEqual(["ext_demo_search"], [t.name for t in filtered])
        self.assertIn("[ext demo]", filtered[0].description)
        self.assertEqual(0, len(disallowed))  # red line: nothing when not allowed

    def test_call_tool_routes_by_prefix_when_allowed(self):
        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])
        allowed = {"ext_demo_search", "ext_nope_x", "builtin_tool"}

        async def drive():
            reg._sessions["demo"] = (FakeSession([]), None)
            ok = await reg.call_tool("ext_demo_search", {"q": "x"}, allowed=allowed)
            bad_prefix = await reg.call_tool("builtin_tool", {}, allowed=allowed)
            unknown_server = await reg.call_tool("ext_nope_x", {}, allowed=allowed)
            return ok, bad_prefix, unknown_server

        ok, bad_prefix, unknown_server = run_coro(drive())
        self.assertIn("called search", ok.text)
        self.assertIsNone(ok.error)
        self.assertTrue(bad_prefix.error)
        self.assertTrue(unknown_server.error)

    def test_call_tool_blocked_by_allowlist_red_line(self):
        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])
        reg._sessions["demo"] = (FakeSession([]), None)
        res = run_coro(reg.call_tool("ext_demo_search", {"q": "x"}, allowed=set()))
        self.assertTrue(res.error)
        self.assertIn("allowlist", res.error)

    def test_call_result_is_error_surfaces_failure(self):
        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])

        class ErrSession(FakeSession):
            async def call_tool(self, name, arguments):
                return FakeCallResult("Error: postgres://user:secret_pass@db.internal:5432/main", is_error=True)

        async def drive():
            reg._sessions["demo"] = (ErrSession([]), None)
            result = await reg.call_tool("ext_demo_search", {}, allowed={"ext_demo_search"})
            return result

        result = run_coro(drive())
        self.assertEqual("external tool 'ext_demo_search' failed", result.error)
        self.assertNotIn("secret_pass", result.error)
        self.assertEqual("", result.text)

    def test_structured_content_and_non_text_blocks_serialized(self):
        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])

        class StructuredSession(FakeSession):
            async def call_tool(self, name, arguments):
                res = FakeCallResult("")
                res.content = []
                res.structuredContent = {"status": "ok", "items": [1, 2]}
                return res

        class DictBlockSession(FakeSession):
            async def call_tool(self, name, arguments):
                res = FakeCallResult("")
                res.content = [{"type": "json", "data": {"a": 1}}]
                return res

        async def drive():
            reg._sessions["demo"] = (StructuredSession([]), None)
            res1 = await reg.call_tool("ext_demo_search", {}, allowed={"ext_demo_search"})
            reg._sessions["demo"] = (DictBlockSession([]), None)
            res2 = await reg.call_tool("ext_demo_search", {}, allowed={"ext_demo_search"})
            return res1, res2

        res1, res2 = run_coro(drive())
        self.assertEqual('{"items": [1, 2], "status": "ok"}', res1.text)
        self.assertIn('"data": {"a": 1}', res2.text)

    def test_http_transport_triplet_support(self):
        spec = McpClientSpec("http_demo", url="http://localhost:8000/sse")
        reg = McpClientRegistry([spec])

        class FakeHttpTransport:
            async def __aenter__(self):
                return (mock.AsyncMock(), mock.AsyncMock(), lambda: "session_123")

            async def __aexit__(self, *a):
                return False

        class FakeMcpSession:
            def __init__(self, r, w):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def initialize(self):
                pass

        with mock.patch("mcp.client.streamable_http.streamablehttp_client", return_value=FakeHttpTransport()), \
             mock.patch("mcp.ClientSession", side_effect=FakeMcpSession):
            session, stack = run_coro(reg._open(spec))
            self.assertIsNotNone(session)
            run_coro(stack.aclose())

    def test_connection_init_failure_unwinds_resources(self):
        spec = McpClientSpec("failing_demo", command="fake")
        reg = McpClientRegistry([spec])
        exited = []

        class FakeTransport:
            async def __aenter__(self):
                return (mock.AsyncMock(), mock.AsyncMock())

            async def __aexit__(self, *a):
                exited.append(True)
                return False

        class FailingSession:
            def __init__(self, r, w):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def initialize(self):
                raise RuntimeError("initialize failed")

        with mock.patch("mcp.client.stdio.stdio_client", return_value=FakeTransport()), \
             mock.patch("mcp.ClientSession", side_effect=FailingSession):
            with self.assertRaises(RuntimeError):
                run_coro(reg._open(spec))
            self.assertEqual([True], exited)

    def test_connection_failure_is_silent_and_sanitized(self):
        # A broken server contributes NO tools and leaks no internals
        reg = McpClientRegistry([McpClientSpec("broken", command="definitely-not-a-real-cmd-xyz")])
        tools = run_coro(reg.list_tools(allowed={"ext_broken_tool"}))
        self.assertEqual([], tools)

    def test_close_all_unwinds_transport(self):
        exited = []

        class FakeExitStack:
            async def aclose(self):
                exited.append(True)

        reg = McpClientRegistry([McpClientSpec("demo", command="fake")])
        reg._sessions["demo"] = (FakeSession([]), FakeExitStack())
        run_coro(reg.close_all())
        self.assertEqual([True], exited)
        self.assertEqual({}, reg._sessions)


class GatewayAggregationTests(unittest.TestCase):
    """McpToolGateway aggregates external tools and enforces ext allowlist."""

    def setUp(self):
        self._orig_servers = __import__("os").environ.get("OPENMATRIX_MCP_SERVERS")
        self._orig_ext = __import__("os").environ.get("OPENMATRIX_EXT_ALLOWLIST")
        import agent.mcp_clients as mc
        import agent.tools as at

        mc._default_registry = None
        at._default_gateway = None

    def tearDown(self):
        import os

        import agent.mcp_clients as mc

        if self._orig_servers is None:
            os.environ.pop("OPENMATRIX_MCP_SERVERS", None)
        else:
            os.environ["OPENMATRIX_MCP_SERVERS"] = self._orig_servers
        if self._orig_ext is None:
            os.environ.pop("OPENMATRIX_EXT_ALLOWLIST", None)
        else:
            os.environ["OPENMATRIX_EXT_ALLOWLIST"] = self._orig_ext
        mc._default_registry = None

    def test_gateway_aggregates_allowed_external_tool(self):
        import os

        os.environ["OPENMATRIX_MCP_SERVERS"] = '[{"name":"demo","command":"fake"}]'
        os.environ["OPENMATRIX_EXT_ALLOWLIST"] = "ext_demo_search"

        from agent.tools import McpToolGateway

        gateway = McpToolGateway()
        from agent.mcp_clients import default_registry

        default_registry()._sessions["demo"] = (FakeSession([FakeTool("search")]), None)

        specs = run_coro(gateway.list_tools())
        names = {s.name for s in specs}
        self.assertIn("ext_demo_search", names)
        self.assertIn("retrieve_context", names)

    def test_gateway_filters_external_tool_not_in_ext_allowlist(self):
        import os

        os.environ["OPENMATRIX_MCP_SERVERS"] = '[{"name":"demo","command":"fake"}]'

        from agent.tools import McpToolGateway

        gateway = McpToolGateway()
        from agent.mcp_clients import default_registry

        default_registry()._sessions["demo"] = (FakeSession([FakeTool("search")]), None)

        specs = run_coro(gateway.list_tools())
        names = {s.name for s in specs}
        self.assertNotIn("ext_demo_search", names)

    def test_gateway_call_external_tool_routes(self):
        import os

        os.environ["OPENMATRIX_MCP_SERVERS"] = '[{"name":"demo","command":"fake"}]'
        os.environ["OPENMATRIX_EXT_ALLOWLIST"] = "ext_demo_search"

        from agent.tools import McpToolGateway

        gateway = McpToolGateway()
        from agent.mcp_clients import default_registry

        default_registry()._sessions["demo"] = (FakeSession([]), None)

        result = run_coro(gateway.call_tool("ext_demo_search", {"q": "x"}))
        self.assertIn("called search", result.text)
        blocked = run_coro(gateway.call_tool("ext_demo_other", {}))
        self.assertTrue(blocked.error)
        self.assertIn("allowlist", blocked.error)


if __name__ == "__main__":
    unittest.main()
