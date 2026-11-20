# OpenMatrix × MCP 集成架构说明

> 本文档介绍 OpenMatrix 如何结合 Model Context Protocol (MCP) 生态实现可扩展的文档解析、检索、引用验证与多格式交付。

---

## 0. 核心架构：MCP 做能力原子，OpenMatrix 做编排与核验

| 能力域 | OpenMatrix 内置实现 | 可选外部 MCP / 扩展方向 | 许可证兼容性 |
|---|---|---|---|
| **MCP 运行时** | `mcp==1.29.0` (`FastMCP`) | 官方 `modelcontextprotocol/python-sdk` | MIT |
| **多格式导出** | `reportlab` (PDF)、`openpyxl` (XLSX)、`python-docx` (DOCX)、`python-pptx` (PPTX) | `carboneio/carbone-mcp` / 模板引擎 | Apache-2.0 / MIT |
| **引用核验分层** | `verify_quote`（精确 / 归一化 / 模糊）+ `assess_answer_support`（数值/实体一致性） | 可选 LLM 语义复核 (`OPENMATRIX_SUPPORT_JUDGE=llm`) | 自研核心 |
| **多文档研究编排** | 无网格 `analyze_documents` + `agent/` 有边界任务状态机 | 官方 `filesystem` MCP / 外部连接器 | MIT |
| **财务数据与宏观源** | SEC EDGAR (免 Key XBRL + 原文)、AkShare、FRED | 可扩展其它公开合规数据源 MCP | MIT / Apache-2.0 |
| **网页检索与抓取** | SSRF 加固 `fetch_context` + 可选 Web Research | SearXNG / Firecrawl MCP（仅作可选适配） | MIT / AGPL-free |

---

## 1. OpenMatrix MCP Server (`mcp_server.py`)

`mcp_server.py` 对外暴露 **52 个工具**，按职能分为 6 大类（完整清单见 [docs/MCP-ROADMAP.md](docs/MCP-ROADMAP.md)）：

1. **检索与解析**：`retrieve_context`、`fetch_context`（带 DNS 防重绑定与 IP 钉扎 SSRF 防护）、`summarize_context`
2. **引用与财务分析**：`verify_quote`、`extract_kpis`、`compute_financials`、`find_contradictions`、`verify_claim_chain`
3. **无网格多文档分析**：`analyze_documents`（推荐默认多文档工作流：文档列表 + 独立问答 + 汇总，不构建网格中间层）
4. **数据源连接器**：`sec_search`、`sec_get_filing`、`sec_get_xbrl_facts`、`ak_stock_quote`、`ak_financials`、`fred_series` 等
5. **项目库与后台任务**：文档库读写、异步任务队列、持久化定时任务管理
6. **多格式交付导出**：`export_report`（CSV/XLSX/PDF/DOCX）、`export_slides`（每页附原文引文脚注的 PPTX）

---

## 2. 外部 MCP 客户端与生态接入

OpenMatrix 支持作为 MCP Server 被 Claude Desktop、Cursor、OpenCode 等客户端调用，也支持通过外部 MCP Client Registry 安全挂载第三方能力。

- 详细配置与环境变量说明见 [docs/MCP-CLIENTS.md](docs/MCP-CLIENTS.md)。
- 选型时遵循许可证隔离原则：严禁将 AGPL-3.0 或 `CC-BY-NC` 依赖引入核心分发包（详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。
