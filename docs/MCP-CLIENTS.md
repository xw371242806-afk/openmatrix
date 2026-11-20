# External MCP Client Registry（C 组）配置接入指南

OpenMatrix 的 agent 层可以作为 **MCP client** 连接第三方 MCP server（Tavily、Playwright、
GitHub、postgres-mcp、docling-mcp 等），通过 `McpClientRegistry` 聚合到 `ToolGateway` 表面。
外部工具以 `ext_<server>_<tool>` 命名，且**必须显式加入 allowlist** 才能被 agent 规划/调用
（专利红线）。

## 1. 配置（`OPENMATRIX_MCP_SERVERS` env）

JSON 数组，每个条目二选一：

```json
[
  {"name": "tavily", "command": "npx", "args": ["-y", "@tavily/mcp-server"], "env": {"TAVILY_API_KEY": "..."}},
  {"name": "github", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}},
  {"name": "playwright", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
  {"name": "docling", "url": "http://127.0.0.1:8001/mcp"}
]
```

- `command` + `args`：stdio 连接（进程启动）。
- `url`：streamable HTTP 连接。
- `name`：仅 `[a-zA-Z0-9_-]`，1-48 字符。

## 2. Allowlist（`OPENMATRIX_EXT_ALLOWLIST` env）

只有显式列出的外部工具对 agent 可见/可调用：

```
OPENMATRIX_EXT_ALLOWLIST=ext_tavily_web_search,ext_github_create_issue,ext_github_list_issues
```

未列出的外部工具即使 server 已连接也不会出现在 `list_tools`，调用会返回 allowlist 拒绝。

## 3. 使用

- `task_submit("research_run", {...})` 启动 research run，planner 可规划 allowlist 内的
  `ext_*` 步骤（需在 planner 模板中引用，或通过上下文传入）。
- `McpToolGateway.list_tools()` 自动聚合外部工具（受 allowlist 过滤）。
- `McpToolGateway.call_tool("ext_<server>_<tool>", args)` 路由到对应 server。

## 4. 安全

- 外部工具名需显式 allowlist（专利红线双重强制：list + call）。
- 错误 sanitize：上游错误文本/异常类名不外泄。
- 连接懒加载；失败的 server 不影响其他工具。
- 传输生命周期由 `AsyncExitStack` 管理（含 HTTP session-id 回调、初始化失败回滚）。

## 5. 示例：启用 Tavily 外部 MCP（可选，等价于内置 web_research）

```powershell
$env:OPENMATRIX_MCP_SERVERS = '[{"name":"tavily","command":"npx","args":["-y","@tavily/mcp-server"],"env":{"TAVILY_API_KEY":"<KEY>"}}]'
$env:OPENMATRIX_EXT_ALLOWLIST = 'ext_tavily_web_search,ext_tavily_web_extract'
```

> 内置 `web_research`/`web_crawl`/`web_extract` 已覆盖 Tavily 搜索/爬取/提取，通常无需再接入
> 外部 Tavily MCP；此例仅为展示外部 server 接入方式。
