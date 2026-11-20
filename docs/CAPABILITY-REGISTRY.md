# OpenMatrix Capability Registry

> Evidence date: 2026-08-16
> Status: **provisional planning data.** Scores and decisions are engineering estimates
> for planning and search only. They are NOT license grants, legal authorization, or
> Freedom-to-Operate opinions. Integration of any entry still requires an Issue intake,
> a supervisor gate, and (where noted) license/counsel review.
> Rule: registry inclusion is not automatic installation authorization.
> Verified production components live in `docs/COMPONENT-CATALOG.md` (source of truth).
> This registry links to the catalog; it does not replace or absorb it.

## Purpose

Replacing ad-hoc component notes with a scored, queryable registry so the
`capability-search` / `compatibility-judge` Agents can look up "what to build with".
Scores are planning heuristics; exact facts (license text, versions, security review)
must be re-verified from the cited source before production use.

## License posture (engineering policy, not legal advice)

- All statements below are **engineering flags for planning only**. They are not
  determinations of what is legally usable. Confirm each dependency's SPDX license from
  its source, and obtain license/counsel review before production use.
- Permissive licenses (MIT, Apache-2.0, BSD-family) and official key-free sources
  (e.g. SEC EDGAR, FRED, World Bank) are flagged as the most likely to be directly
  reusable - still subject to verification.
- MPL-2.0 and BSD-compatible variants are flagged "review with attribution"; treat as
  usable only after license review.
- AGPL-3.0, CC-BY-NC, and custom/restricted licenses are flagged red. "Wrap the library,
  do not link the server" is an **engineering mitigation for evaluation only** - it does
  not by itself resolve license obligations. Any actual use requires license or counsel
  review. CC-BY-NC entries are flagged "not for commercial use" pending review, not as a
  definitive exclusion.
- No entry in this registry is stated to be legally reusable or authorized.

## Compatibility Score (Direct Reuse / Adapter / Build)

Score each entry on the same heuristic scale. Decision mapping (with override rule):

| Score | Default decision |
|---|---|
| 0.8-1.0 | **Direct Reuse** |
| 0.5-0.79 | **Thin Adapter** |
| < 0.5 | **Build** (or reject) |

Override rule: an entry at the boundary (e.g. exactly 0.8) may be judged **Adapter** when
the Adapter-cost factor is high (e.g. needs an env-gated wrapper, ParserAdapter hook, or
runtime guardrail). The override is recorded in the Decision column so the mapping stays
auditable. Provisional entries (unverified license/evidence) are labeled `(provisional)`
and are not authorized for integration.

### Scoring rubric (0-1, heuristic)

| Factor | Weight | Notes |
|---|---|---|
| License compatibility | 0.35 | permissive=1.0; BSD=0.9; MPL=0.6; AGPL=0.2; CC-BY-NC=0.0 |
| Maintenance / activity | 0.20 | recent commits + issue responsiveness (qualitative) |
| Dependency / attack surface | 0.20 | fewer deps and narrower surface score higher |
| Adapter cost | 0.15 | matches an existing OpenMatrix adapter protocol? |
| Security posture | 0.10 | SSRF/large-file/privilege surface |

Scores are weighted estimates, not computed from a published dataset; factor-level
breakdowns are not stored per row. Treat numeric scores as planning signals, verify facts
from cited evidence before use.

## Registry

| Capability | Entry | License | Activity | Adapter cost | Security | **Score** | Decision | Evidence / Eval |
|---|---|---|---|---|---|---|---|---|
| Text PDF parsing | pypdf (see requirements.txt) | BSD-3-Clause | high | none (already default) | ok | **0.9** | Direct Reuse | P0 production; source: pypdf LICENSE |
| Table extraction | Camelot 2.x | MIT | medium | ParserAdapter hook | medium | **0.75** | Adapter | P0-B item 11 |
| OCR preprocessor | OCRmyPDF | MPL-2.0 | high | OcrPreprocessorAdapter | medium | **0.7** | Adapter | P0-B item 10 |
| Dense embedding | FastEmbed (BGE small) | Apache-2.0 | high | DenseRetrieverAdapter | ok | **0.85** | Direct Reuse | P2-1 |
| Vector store | Qdrant | Apache-2.0 | high | VectorStoreAdapter | ok | **0.85** | Direct Reuse | P2-3 |
| Docling parsing | docling (lib) | MIT | high | ParserAdapter (env-gated) | medium | **0.8** | Adapter (override: env-gated wrapper) | Issue #78 eval; source: docling LICENSE |
| MinerU parsing | mineru | custom Apache-derived | high | ParserAdapter | heavy | **0.5 (provisional)** | Gated P2 | license review pending; not authorized until confirmed |
| MCP SDK | mcp==1.29.0 | MIT | high | none | ok | **0.95** | Direct Reuse | merged, 204 tests; source: modelcontextprotocol LICENSE |
| Report PDF | reportlab | BSD-compatible | high | none | ok | **0.85** | Direct Reuse | PR #101 |
| SEC filings | SEC EDGAR official API | government source (key-free) | official | thin wrap tool | ok | **0.95** | Direct Reuse | no key, key-free; source: sec.gov; review data-license terms before redistribution |
| SEC parsing lib | edgartools | verify (provisional) | high | thin wrap tool | ok | **0.8 (provisional)** | Adapter | license unverified; confirm exact SPDX before integration |
| SEC MCP server | sec-edgar-mcp | AGPL-3.0 | high | **do not link server** | n/a | **0.2** | Reject (require license/counsel review for any use) | AGPL red flag |
| China A-share | AkShare | MIT | high | thin wrap tool | medium | **0.85** | Direct Reuse | no key; source: AkShare LICENSE |
| China A-share MCP | akshare_mcp_server | open-source (provisional) | medium | reuse directly | medium | **0.75 (provisional)** | Adapter | license/evidence unverified; confirm before reuse |
| Eastmoney MCP | mcp-eastmoney | MIT | low | reuse directly | medium | **0.7** | Adapter | 15-min delay; source: repo LICENSE |
| Web search | Tavily MCP (official) | MIT | high | none | ok | **0.9** | Direct Reuse | user key; source: tavily-mcp LICENSE |
| Browser control | Playwright MCP | Apache-2.0 | high | none | medium | **0.8** | Direct Reuse | Microsoft; source: microsoft/playwright-mcp |
| GitHub access | GitHub MCP Server | MIT | high | none | ok | **0.9** | Direct Reuse | official Go server; source: github/github-mcp-server |
| Postgres DB | postgres-mcp (crystaldba) | MIT | high | none | medium | **0.85** | Direct Reuse | self-host |
| MS365 connector | ms-365-mcp-server | verify (provisional) | medium | connector tool | medium | **0.7 (provisional)** | Adapter | license unverified; confirm exact SPDX before integration |
| Embedding (local) | BGE-M3 | MIT | high | DenseRetrieverAdapter | ok | **0.9** | Direct Reuse | hybrid default target; source: BAAI model card |
| Reranker (local) | BGE Reranker v2-m3 | Apache-2.0 | high | RerankerAdapter | ok | **0.85** | Direct Reuse | add to retrieval path |
| Embedding (NIM) | llama-3.2-nv-embedqa-1b-v2 | NIM API | official | DenseRetrieverAdapter | ok | **0.8** | Adapter (override: API adapter) | free endpoint; source: build.nvidia.com |
| Rerank (NIM) | llama-3.2-nv-rerankqa-1b-v2 | NIM API | official | RerankerAdapter | ok | **0.8** | Adapter (override: API adapter) | source: build.nvidia.com |
| OCR (local) | olmOCR v2 | Apache-2.0 | high | ParserAdapter | medium | **0.75** | Adapter | 16GB+; source: allenai model card |
| Agent orchestration | LangGraph | MIT | high | none | ok | **0.85** | Direct Reuse | research-agent phase |
| Agent orchestration | LlamaIndex Workflows | MIT | high | none | ok | **0.8** | Direct Reuse | doc-heavy |
| Agent orchestration | DeepSeek Harness (dsh) | TBD (verify against pinned commit) | TBD (verify against pinned commit) | TBD (verify MCP client plugin) | TBD | **0.5 (provisional)** | Watch, not adopt yet | candidate for P5; all facts (license, language, MCP client, preview status, version) must be verified against a pinned commit/tag before adoption |
| UI framework | Ant Design | MIT | high | none | ok | **0.9** | Direct Reuse | Chinese team; source: ant-design LICENSE |
| UI data grid | AG Grid Community | MIT | high | none | ok | **0.85** | Direct Reuse | current |
| AI chat UI | Ant Design X | MIT | medium | none | ok | **0.8** | Direct Reuse | |
| Charts | ECharts | Apache-2.0 | high | none | ok | **0.9** | Direct Reuse | |
| Postgres async driver | psycopg (async) | LGPL-3.0 / BSD-3 | high | MetadataStore (P4.1) | ok | **0.9** | Direct Reuse | PR #159 `PostgresMetadataStore`; source: psycopg project |
| Object store (S3/MinIO) | boto3 | Apache-2.0 | high | ObjectStore (P4.1) | ok | **0.9** | Direct Reuse | PR #159 `S3ObjectStore`; source: aws/boto3 |

## Rejected / red-flag entries

| Entry | Reason | Engineering mitigation (requires license/counsel review for actual use) |
|---|---|---|
| `sec-edgar-mcp` (server) | AGPL-3.0 copyleft | prefer SEC EDGAR official API + edgartools library; do not link the AGPL server |
| OpenBB | AGPL-3.0 | use official data sources directly |
| NV-Embed-v2 / NV-Embed-v1 | CC-BY-NC | flagged not-for-commercial pending license/counsel review |
| jina-embeddings-v3 | CC-BY-NC-4.0 | use BGE/Qwen3 instead |
| Handsontable | commercial | use AG Grid Community / Tabulator |
| PyMuPDF | AGPL / commercial | use pypdf / Docling |
| Microsoft OneDrive MCP | proprietary + preview | use Graph API / ms-365 server |

## Note: DeepSeek Harness (DSH) evaluation

DeepSeek Harness (`deepseek-ai/deepseek-harness`) is an agent-harness candidate for the
P5 orchestration layer, flagged **provisional / watch, not adopt yet**. Rationale:

- **Unverified facts**: this registry does not yet assert its license, MCP-client
  capability, preview status, or version. Before any adoption decision, verify all of
  these against an immutable pinned commit/tag of the repository (LICENSE, README, and
  the MCP client package path at that revision). MCP interoperability with our
  `mcp_server.py` is **expected but not verified** until an integration test is run.
- **Order dependency**: P1 data-source tools (SEC/AkShare/FRED) do not exist yet; there
  is nothing meaningful to orchestrate until they land.
- **Stack note**: the project is reported to be TypeScript-based; this should be
  confirmed against a pinned commit. If so, integration with our Python backend would be
  as a separate orchestration service reached via MCP, not embedded.
- **Decision**: score **0.5 (provisional)**. LangGraph (0.85, production-ready, MIT)
  remains the current preferred orchestration choice. Re-evaluate DSH when its facts are
  pinned to a stable release and P1 tools are implemented.

## Maintenance

- This is provisional planning data; verify license text, versions, and security facts
  from cited sources before production integration.
- Re-score an entry when a new version or better alternative appears.
- Verified production components stay in `docs/COMPONENT-CATALOG.md` (source of truth).
  When an entry here becomes production-verified, add it to the catalog and link back
  from this registry; do not move/remove it from the catalog.
- `capability-search` / `compatibility-judge` Agents read this registry as their data source.
