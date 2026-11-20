# OpenMatrix MCP Capability Roadmap

> Evidence date: 2026-08-13
> Purpose: catalog additional capabilities that can be delivered through the MCP tool layer,
> aligned with the 52 merged MCP tools (see section 0) and the document->retrieval->evidence->conclusion pattern.
> Rule: planning only. No tool below is authorized for implementation until an Issue intake + supervisor gate.
> Patent posture: all analysis tools must operate on raw document retrieval -> evidence -> conclusion,
> NOT on `matrix_cells` secondary generation. Compare features use list/report form, not grid form.
> Key principle: all third-party keys are user-supplied (env / local config), never hard-coded or committed.
> Capability Manifest (AI Software Factory): before implementing anything, check the manifest below for an
> existing MCP / GitHub / API implementation first. Search priority: Capability Registry -> Skill -> MCP Server
> -> mature GitHub Repo -> API/SDK -> CLI/SaaS -> thin Adapter -> build from scratch.

## 0. Baseline (merged on main)

> **Refreshed 2026-08-22**: the original 7-tool table below was stale. Verified by counting
> `@mcp.tool` definitions in `mcp_server.py`: **52 tools are live on main**. Grouped inventory
> (authoritative; keep this table in sync when adding tools):

| Category | Tools | Count |
|---|---|---|
| Projects / document library | `project_create`, `project_list`, `project_delete`, `document_add`, `document_list`, `document_delete`, `document_update`, `document_versions`, `document_restore`, `project_search` | 10 |
| Agent task orchestration | `task_submit`, `task_status`, `task_cancel`, `task_result`, `task_list`, `decompose_analysis` | 6 |
| Data sources | `sec_search`, `sec_get_filing`, `sec_get_xbrl_facts`, `ak_stock_quote`, `ak_financials`, `macro_fred_lookup` | 6 |
| Security / governance | `apikey_create`, `apikey_list`, `apikey_revoke`, `apikey_verify`, `audit_list` | 5 |
| Ops / observability | `component_status`, `eval_retrieval`, `model_health`, `backup_data`, `restore_data` | 5 |
| Retrieval / citation | `retrieve_context`, `fetch_context`, `summarize_context`, `verify_quote` | 4 |
| Export / deliverables | `export_matrix_report`, `export_report`, `export_slides`, `locate_quote_bboxes` | 4 |
| Financial analysis | `compute_financials`, `extract_kpis`, `financial_model`, `analyze_documents` | 4 |
| Web intelligence | `web_research`, `web_crawl`, `web_extract` | 3 |
| Scheduled research (H7) | `schedule_research`, `list_schedules`, `cancel_schedule` | 3 |
| Contradiction detection | `find_contradictions`, `verify_claim_chain` | 2 |
| **Total** | | **52** |

Historical baseline of the first merge wave (kept for provenance):

| Tool | Source |
|---|---|
| `export_matrix_report` | PR #86 |
| `retrieve_context` | PR #88 |
| `verify_quote` | PR #91 (+ layering PR #99) |
| `fetch_context` | PR #94 |
| `summarize_context` | PR #97 |
| `export_report` (PDF/XLSX) | PR #101 |
| `analyze_documents` | PR #104 |

## 1. Analysis / Reasoning (pure MCP, reuse existing retrieval pipeline)

> Status: `compute_financials` / `extract_kpis` **implemented in P2 (PR #123),
> pending acceptance** on the `feat/p2-analysis` branch; not marked shipped until
> supervisor approval and merge.
> `find_contradictions` / `verify_claim_chain` were split OUT of P2 (they are
> experimental semantic heuristics needing deeper NLU; deferred to a later phase).
> Remaining rows stay planning-only until their own Issue intake + supervisor gate.

| Tool | Signature (implemented) | Notes |
|---|---|---|
| `extract_kpis` | `(pages, kpi_names: list[str], mode="bm25", limit_per_question=2)` | [P2 implemented, pending acceptance] Reuses `select_retrieved_context`; nearest-number extraction per KPI |
| `extract_tables` | `(pages, mode="bm25")` | Table-structure extraction, list form |
| `compare_documents` | `(documents: list[dict], question: str)` | Side-by-side list comparison, NOT grid |
| `find_contradictions` | `(documents: list[dict], topic: str, mode="bm25", limit_per_question=2)` | [Deferred out of P2] Cross-document conflicts (numeric / assertion-vs-negation), list form; EXPERIMENTAL heuristic requiring deeper NLU; not part of PR #123 |
| `compute_financials` | `(values: list[dict], operation: str)` | [P2 implemented, pending acceptance] Deterministic sum/average/ratio/percent/currency/unit conversion (no LLM) |
| `scan_risks` | `(pages, rules: list[str])` | Rule-template risk/clause scanning |
| `check_compliance` | `(pages, regulation: str)` | Checklist-style compliance scan |
| `decompose_question` | `(question, max_sub=5)` | Split a question into retrievable sub-questions (no grid cells) |
| `verify_claim_chain` | `(claim, pages, depth=1)` | [Deferred out of P2] Conclusion->citation->source text-match audit via `verify_quote` layering; EXPERIMENTAL heuristic requiring deeper NLU; not part of PR #123 |
| `sensitivity_analysis` | `(table: dict, scenario: dict)` | What-if computation on extracted table data |

## 2. Data Source / Input (connector MCP servers)

| Tool | Signature (proposal) | Notes |
|---|---|---|
| `sec_search` | `(ticker, form_type=None)` | US SEC filing search (official data.sec.gov, no key) |
| `sec_get_filing` | `(filing_url)` | Fetch and normalize a filing to pages |
| `sec_get_xbrl_facts` | `(ticker, fact_names: list[str])` | Structured XBRL financial facts (official XBRL API) |
| `ak_stock_quote` | `(code)` | China A-share quote via AkShare (MIT, no key) |
| `ak_financials` | `(code)` | China A-share financial statements via AkShare |
| `macro_fred_lookup` | `(series_id, start, end)` | FRED macro series (free key, user-supplied) |
| `folder_scan` | `(path)` | Batch ingest local directory (path allowlist required) |
| `folder_sync` | `(path, since)` | Incremental folder sync |
| `email_extract` | `(eml_path)` | EML attachment -> document text |
| `analyze_image` | `(image_path, question)` | Visual-model chart/image analysis (requires vision model) |
| `describe_chart` | `(image_path)` | Chart caption/description (vision model) |

## 2.5 Financial Data Source Catalog (free / open, commercial-license vetted)

> Selection rule: prefer official sources (SEC/FRED/central banks) and MIT/Apache-2.0 open libraries.
> "Free" does NOT mean "commercially usable" - each row records the license constraint.

### 2.5.1 Open-source libraries (MIT / Apache-2.0, commercial-safe)

| Component | License | Coverage | Notes |
|---|---|---|---|
| **AkShare** | MIT | China A-share/HK/funds/bonds/macro, 50+ sources (Eastmoney/Sina/central banks) | 1000+ endpoints, no key, pandas-ready; **core China data building block** |
| **Tushare Pro** | Partial free | China A-share/financials/macro | 220+ endpoints; free tier capped by credits, deep data needs points/paid |
| **yfinance** | Apache-2.0 | Global stocks/ETF/macro | No key, simplest; **scrapes unofficial Yahoo endpoints** - hobby use only, not production |
| **edgartools** | TBD (verify) | US SEC 10-K/Q/8-K, XBRL | AI-native parsing; verify license before bundling |
| **pandas-datareader** | BSD | FRED/Stooq/World Bank aggregation | Official sources via one reader |

### 2.5.2 Official free APIs (no auth, most stable, commercial-safe)

| Source | Coverage | Endpoint | Notes |
|---|---|---|---|
| **SEC EDGAR** (data.sec.gov) | US filings/XBRL/full-text | `submissions`, `companyfacts`, `xbrl/frames`, `efts.sec.gov` | **Government, key-free, commercial-safe**; preferred for SEC connector |
| **FRED** (St. Louis Fed) | Macro/rates/forex | `fred.stlouisfed.org` | Official, free key (user-supplied) |
| **World Bank / IMF** | Global macro | `api.worldbank.org` | Official, no key |
| Eastmoney / Sina public endpoints | China realtime/history | via AkShare | Free but unofficial, anti-scraping risk |

### 2.5.3 Free-tier APIs (limited quota or non-commercial [WARN])

| API | Free quota | Constraint |
|---|---|---|
| Finnhub | 60 req/min | [NO] free tier is non-commercial |
| Alpha Vantage | 25 req/day | [WARN] display/redistribution rights; real-time is paid |
| Twelve Data | 800/day | [WARN] EOD only; commercial rights to confirm |
| Polygon.io | 5 req/min | [WARN] EOD delayed |
| FMP | 250/day | [WARN] free plan is not a commercial license |
| Marketstack | 100/month | [WARN] too small for production |

### 2.5.4 Existing MCP data servers (reuse or wrap)

| MCP server | Data source | License |
|---|---|---|
| `stefanoamorelli/sec-edgar-mcp` | SEC EDGAR | AGPL [WARN] (wrap the library, do not link the server) |
| `mcp-eastmoney` (27dream) | Eastmoney A-share (15-min delay) | MIT [OK] |
| `akshare_mcp_server` (ttjslbz001) | AkShare full | open-source [OK] |
| `tushare-finance` skill | Tushare Pro | needs token |
| `a-share-mcp` | Eastmoney/Sina/Cninfo | open-source [OK] |

### 2.5.5 Data-source integration rules

- Prefer official sources (SEC/FRED/central banks): key-free, stable, commercially clean.
- AkShare is the China core: MIT, commercial-safe, broadest free A-share coverage.
- yfinance / Finnhub free tier never reach production (fragile / non-commercial).
- Keys are user-supplied (env or local config); SEC and AkShare need no key at all.
- AGPL entries are flagged red. "Wrap the library, never link the AGPL server" is an engineering mitigation for evaluation only; any actual use requires license or counsel review.

## 3. Delivery / Collaboration (MCP trigger + service support)

| Tool | Signature (proposal) | Notes |
|---|---|---|
| `report_docx` | `(run_id, output_dir=None)` | DOCX variant of `export_report` |
| `publish_webpage` | `(markdown: str, title: str)` | Render to HTML knowledge page |
| `draft_email` | `(research_id, tone="professional")` | Draft only; human-confirmed send |
| `artifact_share` | `(artifact_path)` | Signed download link (object storage) |
| `artifact_archive` | `(artifact_path, ttl_days=90)` | Archive + retention (object storage) |
| `schedule_research` | `(cron: str, objective: str)` | Requires task queue + scheduler |

## 4. Ops / Governance (MCP exposure of existing services)

| Tool | Signature (proposal) | Notes |
|---|---|---|
| `eval_retrieval` | `(mode="bm25")` | Reuse gold-corpus eval harness for agent self-check |
| `models_list` | `()` | List available models / routing |
| `model_health` | `()` | Provider health / fallback status |
| `task_status` | `(task_id)` | Reuse `/api/v1/tasks` backend |
| `task_cancel` | `(task_id)` | Reuse `/api/v1/tasks` backend |
| `index_rebuild` | `(collection: str)` | Rebuild vector index (vector store service) |
| `index_status` | `(collection: str)` | Index health (vector store service) |
| `audit_list` | `(cursor=None)` | Read-only audit query; server-side auth mandatory |

## 5. MCP-Ability Re-classification (2026-08-13 revision)

> Original framing "MCP cannot replace X" was too pessimistic. Core insight:
> **MCP exists precisely to expose information the model does not know** (internal data,
> private documents, live APIs, company knowledge). Every capability that can be
> executed server-side and returns data can be an MCP tool. What MCP cannot replace is
> the *infrastructure underneath* (database, object storage, GPU, identity provider) -
> but the *interface layer* exposed to the model can be 100% MCP.

### 5.1 What changed (2026 evidence)

1. **MCP Tasks API is now a standard** (spec 2025-11-25, RC 2026-07-28): tools can opt into
   `execution.taskSupport` and return an async task handle instead of blocking. Protocol-level
   `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/result`, `pollInterval`, and
   `input_required` replace every ad-hoc `start_*` + `get_*_status` pattern. Long-running
   document parsing, batch analysis, and rendering are now first-class MCP.
2. **Enterprise MCP gateways are mature** (Obot, Auth0, WorkOS, Keycloak, Stytch, Cloudflare):
   OAuth 2.1 + PKCE, dynamic client registration, token exchange (RFC 8693), RBAC/FGA
   fine-grained tool permissions, and audit logs run at the **gateway**, not in each server.
   Identity is verified by the gateway and passed to the server as a trusted principal -
   the server never trusts `user_id/is_admin` from tool arguments.
3. **Enterprise best practice confirms** internal knowledge bases / private data are the
   *canonical* MCP use case: "call internal corporate APIs in a controlled, authenticated way,
   retrieve knowledge directly from internal databases, enforce strict boundaries between
   model logic and system access." Expose with egress allowlist + URL sanitization + SSRF
   blocking + stdio transport where possible.

### 5.2 Revised verdict per previously-"impossible" capability

| Capability | Old verdict | New verdict | How it is MCP |
|---|---|---|---|
| Projects / datasets / document library | "Interface only" | [OK] **100% MCP tools** | `project_create/list/get`, `dataset_add_documents`, `document_upload/search/delete` as MCP tools; PostgreSQL + object storage + vector search stay underneath as infra, never exposed to the model |
| Chat / Research Agent | "Tool layer, not MCP" | [OK] **MCP-orchestrated** | Orchestrator can itself be a tool (`research_run`) that returns a Tasks API handle; planner/state machine run server-side, model just calls/waits via `tasks/get` + `input_required` |
| User / team / permission / audit | "Interface only" | [OK] **Gateway-layer, then MCP tools** | Identity/RBAC/audit enforced at MCP gateway (OAuth 2.1 + FGA + audit logs); tools then run as trusted principal. `audit_list`/`member_list` become valid MCP tools **after** gateway auth |
| Large-file parsing | "Trigger only" | [OK] **100% MCP via Tasks API** | `parse_document` returns task handle -> `tasks/get` polls -> `tasks/result`; async worker + Docling/OCR underneath |
| Connector auth | "connector_id only" | [OK] **MCP + gateway token exchange** | `connector_*` tools use gateway-issued scoped tokens (RFC 8693 exchange, never user token passthrough); secret manager holds long-lived creds |
| Deliverable rendering | "Trigger only" | [OK] **100% MCP via Tasks API** | `report_render` returns task handle; template store + office rendering underneath |

### 5.3 What genuinely cannot be MCP (narrow)

| Item | Why | What covers it |
|---|---|---|
| Identity / authentication itself | MCP is a *consumer* of identity, not a producer; the model cannot prove who it is | IdP (Keycloak/Zitadel/Entra) + MCP gateway OAuth 2.1 |
| Infrastructure (DB, object storage, GPU, broker) | MCP is an interface, not a storage/engine | PostgreSQL, S3/OSS, GPU runtime, task queue |
| Model weights / inference runtime | The LLM itself | NIM / local Qwen3 / GPU service |
| Hard legal/patent opinions | Engineering observation != FTO | Qualified counsel |

Everything else - any information the model lacks that a server can fetch/compute - is MCP-able.

## 5.5 Model & Agent Catalog (NVIDIA NIM / HuggingFace / agent frameworks)

> Selection rule: commercial-safe licenses (MIT/Apache-2.0) for self-hosted models.
> Existing adapters (RetrieverAdapter / RerankerAdapter / VectorStoreAdapter) are the insertion points - no architecture rewrite.

### 5.5.1 Retrieval layer

| Task | Model | License | Notes |
|---|---|---|---|
| Embedding (NIM API) | `nvidia/llama-3.2-nv-embedqa-1b-v2` | free endpoint | 26 languages, long-doc QA |
| Embedding (NIM API) | `nvidia/nemotron-3-embed-1b` | free endpoint | |
| Embedding (local) | **BGE-M3** | MIT | dense+sparse+multi-vector, 100+ languages, hybrid-retrieval default |
| Embedding (local) | Qwen3-Embedding-8B / -0.6B | Apache-2.0 | MTEB multilingual 70.58, 4096-dim Matryoshka |
| Rerank (NIM API) | `nvidia/llama-3.2-nv-rerankqa-1b-v2` | - | passage-level rerank |
| Rerank (local) | **BGE Reranker v2-m3** | Apache-2.0 | multilingual cross-encoder, 568M |
| Rerank (local) | Qwen3 Reranker / mxbai-rerank-large-v2 | Apache-2.0 | |
| OCR | olmOCR v2 | Apache-2.0 | PDF linearization/reading order; DeepSeek-OCR/PaddleOCR-VL for Chinese scans |

[WARN] License red flags: NV-Embed-v2 / NV-Embed-v1 (CC-BY-NC) and jina-embeddings-v3 (CC-BY-NC-4.0) are NOT usable for commercial RAG.

### 5.5.2 Generation layer

| Model | Notes | Hardware |
|---|---|---|
| `openai/gpt-oss-120b` (NVIDIA NIM) | current primary; tool-calling verified | hosted |
| **Qwen3-30B-A3B-Instruct** | 2026 RAG best overall: MoE 3B active, 262K ctx, RAGAS 0.91 | A10G |
| Qwen3-235B-A22B | multilingual strongest, Apache-2.0 | multi-GPU |
| GLM-5.1 / Kimi K2.6 | strong agentic workflows | large VRAM |
| Gemma 4 26B-A4B | best capability/hardware ratio | consumer GPU |

### 5.5.3 Agent frameworks (all MIT/Apache-2.0, all MCP-compatible)

| Framework | Best for | Notes |
|---|---|---|
| **LangGraph** | production complex workflows | state machines/HITL/parallel; used by Klarna/Uber; LangSmith observability |
| **LlamaIndex Workflows** | document-heavy pipelines | event-driven, closest to document->retrieval->evidence |
| **Haystack** | pure RAG pipelines | official NVIDIA components: NvidiaGenerator/NvidiaTextEmbedder/NvidiaRanker |
| **CrewAI** | fast role-based prototyping | research/report scenarios |
| **Smolagents** | lightweight single agent | code-first |

### 5.5.4 Integration pattern (no rewrite)

```text
LangGraph / Haystack / LlamaIndex (orchestration: planner + state machine + tools)
        |  MCP protocol
        v
existing mcp_server.py (7 tools + new data tools)   <- already agent-ready
        |
        v
existing adapters (swap implementations only)
  |-- DenseRetrieverAdapter -> BGE-M3 / Qwen3-Embedding (local) or NIM API
  |-- RerankerAdapter -> BGE Reranker v2-m3 / NIM rerank
  '-- LLM -> gpt-oss-120b (NIM) or Qwen3-30B (local)
```

The agent orchestrates via MCP and still follows document->retrieval->evidence->conclusion, never matrix_cells secondary generation.

## 5.6 UI Component Catalog (building blocks)

> Selection rule: MIT/Apache-2.0, commercial-safe, fit for matrix/table-first product.

| UI block | Choice | License | Notes |
|---|---|---|---|
| Data grid | **AG Grid Community** (current) | MIT | free tier sufficient; do NOT use Enterprise features |
| Data grid alt | TanStack Table + shadcn/ui | MIT | headless, full control |
| Editable grid | react-data-grid / Tabulator | MIT | Excel-like / simple free |
| Framework | **Ant Design (antd)** | MIT | enterprise admin, tables/forms/tree; best for Chinese team |
| AI chat UI | **Ant Design X** | MIT | chat bubbles/streaming/prompt input for Research Agent |
| Styling | shadcn/ui | MIT | copy-paste, full ownership |
| Charts | **ECharts** | Apache-2.0 | Chinese-team friendly; Recharts (MIT) alternative |
| PDF viewer | PDF.js (current) + react-pdf-highlighter | Apache-2.0 / MIT | citation highlight |
| Dashboard blocks | Tremor | MIT | KPI cards/charts, pairs with shadcn |

[NO] Avoid: Handsontable (commercial), MUI X Data Grid Pro (paid), AG Grid Enterprise features.

Recommended frontend path (SaaS phase): React 19 + Vite + TypeScript + TanStack Table/AG Grid Community + Ant Design (or shadcn/ui) + ECharts + PDF.js + Ant Design X. All MIT/Apache-2.0.

## 6. Suggested Priority (effort vs value)

> Theme: MCP is the isolation layer for information the model does not know.
> Each phase adds more of that private/live/internal information surface.

1. `compute_financials` + `extract_kpis` - pure MCP, reuse retrieval pipeline, fast value.
2. SEC connector (`sec_search` / `sec_get_filing` / `sec_get_xbrl_facts`) via official data.sec.gov - model cannot know this; first live-data isolation win.
3. AkShare China connector (`ak_stock_quote` / `ak_financials`) - MIT, no key, +2~3 coverage.
4. `report_docx` - lowest-cost extension of `export_report`.
5. `eval_retrieval` + `model_health` - ops self-check, precondition for agentification.
6. BGE-M3 + BGE Reranker swap (self-host or NIM) - retrieval quality jump.
7. LangGraph / Haystack orchestration - research-agent phase (server-side planner; agent calls MCP tools).
8. Document-library backend (PostgreSQL + object storage + vector search) exposed as MCP tools - the big private-knowledge surface. **[DONE core] SQLite+disk baseline (PR #138) + MCP tools (P4-2, PR #138); production storage abstraction - PostgreSQL (psycopg async) + S3/MinIO (boto3) ObjectStore with graceful fallback (P4.1, PR #159). Remaining: wire the production PG+S3 backend into the running service / deployment env (today it is env-selectable but SQLite remains the default), vector search via Qdrant, and MCP-ize dataset ingestion.**
9. React UI shell (Ant Design + ECharts + PDF.js + Ant Design X) - commercial-equivalence jump.
10. MCP Tasks API adoption (`execution.taskSupport`) for long-running parse/render/batch tools - replaces ad-hoc start/status/result.
11. MCP gateway (Obot/Auth0/Keycloak/WorkOS) - OAuth 2.1 + RBAC + audit at the edge; unlocks multi-user/enterprise deployment.

## 6.5 Coverage Projection (Enterprise Document Research Feature Surface)

> Estimate only, +/-2 points. Feature existence vs commercial equivalence are separate metrics.

| Stage | Feature existence | Commercial equivalence |
|---|---:|---:|
| Current baseline | 35% | 10-15% |
| + MCP 5-stage + existing components (docling-mcp / tavily-mcp) | 47-48% | 15-20% |
| + Financial data sources (SEC + AkShare + FRED) | +2~3 | +2~3 |
| + UI building blocks | ~50% | 30-35% |
| + Permission / document-library backend | ~55% | 40-45% |
| **+ Model layer + Agent framework** | **~60%** | **~50%** |

Remaining 40% gap = institutional features (25+ enterprise data sources, SOC2 compliance, thousands-document scale, finance-vertical model tuning, institutional trust) - a platform build, not a building-block problem.

## 7. Patent Guardrails

- Every analysis tool operates: project/documents -> retrieval -> evidence -> conclusion -> report.
- No tool may route: chat query -> select `matrix_cells` -> combine cells -> LLM secondary generation.
- No tool may compute attention scores -> cell scores -> matrix UI highlighting.
- Comparison/report outputs are list/card/report form; AG Grid remains for plain data tables only.
- All existing `patent-safe` language withdrawn (2026-08-13); engineering observations are not legal FTO opinions.

## 8. Capability Manifest (AI Software Factory - look up before you build)

> Rule: the first action for any new capability is to check this table, not write code.
> Search priority: Capability Registry -> Skill -> MCP Server -> mature GitHub Repo -> API/SDK -> CLI/SaaS -> thin Adapter -> build.

| Capability | What it does | Preferred existing implementation | Source / License |
|---|---|---|---|
| `web.search` | Live web search | **Tavily MCP** (official) | MCP / MIT; user key |
| `web.crawl` | Crawl a site | **Tavily MCP** (map/crawl) | MCP / MIT; user key |
| `web.extract` | Page -> clean text | **Tavily MCP** (extract) / existing `fetch_context` | MCP / MIT; own tool |
| `browser.control` | Drive a browser | **Playwright MCP** (Microsoft) | MCP / Apache-2.0 |
| `github.read_write` | Repo/issues/PRs | **GitHub MCP Server** (official, Go) | MCP / MIT |
| `database.query` | SQL access | **crystaldba/postgres-mcp** (self-host) | MCP / MIT |
| `document.parse` | PDF/DOCX/PPTX -> structure | **docling-mcp** (official) | MCP / MIT |
| `table.extract` | PDF tables | docling-mcp / Camelot (existing adapter) | MCP / MIT |
| `ocr` | Scanned pages | OCRmyPDF (existing) / olmOCR v2 (local) | Apache-2.0 |
| `embedding` | Vectorize chunks | **BGE-M3** (local) / NVIDIA NIM `llama-3.2-nv-embedqa-1b-v2` | MIT / NIM API |
| `rerank` | Re-rank candidates | **BGE Reranker v2-m3** / NVIDIA NIM rerank | Apache-2.0 |
| `sec.filings` | US SEC filings/XBRL | **SEC EDGAR official API** + edgartools (thin wrap) | official, key-free |
| `china.stock` | China A-share quotes/financials | **AkShare** (MIT) - wrap in MCP tool | MIT, no key |
| `macro.data` | Macro series | **FRED** / World Bank official APIs | official, user key |
| `chat.ui` | AI chat interface | **Ant Design X** | MIT |
| `data.grid` | Matrix/table UI | **AG Grid Community** (current) | MIT |
| `agent.orchestrate` | Planner/state machine | **LangGraph** or **LlamaIndex Workflows** | MIT |
| `connector.sharepoint` | MS365 files | `softeria/ms-365-mcp-server` (Graph, China 21Vianet) | verify license |

### Manifest maintenance rules
- Add a row when a new capability is planned; fill "Preferred existing implementation" before any design work.
- Update the row when an implementation is verified in production (move evidence to `docs/CAPABILITY-REGISTRY.md`).
- If no row exists and a mature option is found later, prefer it over the custom build already started.

## 9. Status

- [x] 2026-08-13: roadmap document created. Planning only; nothing implemented.
- [x] 2026-08-13: added financial data source catalog (section2.5), model/agent catalog (section5.5), UI component catalog (section5.6), score projection (section6.5).
- [x] 2026-08-13: revised section5 from "MCP cannot replace X" to "MCP-ability re-classification" - MCP Tasks API (standard) + enterprise MCP gateways (mature) make long-running tools, document library, research agent, permission/audit all MCP-able at the interface layer; only identity itself and infra stay non-MCP.
- [x] 2026-08-13: added Capability Manifest (section8) - look-up-first table per AI Software Factory.
- [ ] Issue intake + supervisor gate before any implementation.
- [x] 2026-08-14: P2 analysis tools implemented on feat/p2-analysis (PR #123 draft): compute_financials + extract_kpis only - pure analysis, reuse existing retrieval pipeline, no matrix_cells.
- [x] 2026-08-14: P2 split - find_contradictions / verify_claim_chain removed from PR #123 (experimental semantic heuristics deferred to a later phase needing deeper NLU); PR #123 now ships compute_financials + extract_kpis only.
- [x] 2026-08-15: P2-3/P2-4 landed as deterministic heuristics (PRs #154/#155) - find_contradictions (numeric-discrepancy v1) + verify_claim_chain (exact/fuzzy chain audit); MCP tools 15 -> 17.
- [x] 2026-08-15/16: P4.1 production storage (PR #159) - storage.py MetadataStore/ObjectStore Protocols + PostgreSQL/S3/MinIO optional backends with silent fallback; PG engine composes the injected ObjectStore (PG+S3 production path).