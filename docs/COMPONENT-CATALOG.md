# OpenMatrix Verified Component Catalog

> Evidence date: 2026-08-12  
> Purpose: reuse mature GitHub components through narrow OpenMatrix adapters  
> Rule: catalog inclusion is not automatic installation authorization
> Note: scored entries with license/security/adapter-cost live in
> `docs/CAPABILITY-REGISTRY.md`; this file remains the detailed component comparison.

## Decision Summary

| Capability | Current / next choice | Later candidate | Rejected or gated |
|---|---|---|---|
| Text PDF parsing | Keep `pypdf >= 6.15.0` | `pdfplumber` for coordinates | PyMuPDF requires commercial-license approval |
| Table extraction | No P0 dependency | Camelot 2.x | Docling/MinerU deferred to complex-document stage |
| OCR | No P0 dependency | OCRmyPDF, then PaddleOCR for failure samples | Unstructured deferred; too broad and heavy |
| Chunking | OpenMatrix page-aware chunker | Chonkie benchmark | Framework-owned chunk models in P0 |
| Sparse retrieval | `bm25s` candidate | Qdrant sparse in P2 | `rank_bm25` as production choice |
| Dense retrieval | None in P0 | FastEmbed + BGE | Full FlagEmbedding stack in P0 |
| Vector storage | None in P0 | Qdrant after quality evidence | Chroma as long-term default; FAISS as document store |
| Rerank | Identity reranker | BGE reranker via lightweight runtime | Jina API for sensitive documents |
| Matrix grid | Keep current lightweight table until P0-B UI decision | AG Grid Community or TanStack Table + Virtual | Handsontable current commercial/non-commercial license |
| PDF viewer | No current dependency | PDF.js; optional react-pdf shell | react-pdf-highlighter as core dependency |
| Citation UI | Keep OpenMatrix domain model | Self-owned renderer; Vercel AI Elements as UI reference | Full chat framework for citation-only use |
| Concurrency | `asyncio` / AnyIO limiter | - | Celery/RQ/Dramatiq for P0-B |
| Retry | Explicit local logic today | Tenacity for transient provider errors | Retrying auth/schema/PDF errors |
| Queue persistence | In-process `TaskQueueAdapter` (Issue #77) | SQLite + aiosqlite single-writer adapter | Redis/broker stack before scale evidence |
| SSE | FastAPI native SSE | - | Extra SSE package without a demonstrated gap |
| Knowledge graph retrieval | Rejected for P0/P1 production (Issue #210) | `lightrag-hku`, `fast-graphrag` | LightRAG PoC proved unscalable for production ingestion: 120B model graph extraction takes >40 minutes per 10-K PDF (~68 chunks, 1-2 mins per 3 chunks). API token costs and ingestion latency block adoption. Fast-graphrag rejected due to similar LLM extraction costs and heavy transitive dependencies. Retain BM25/Dense as production standard. |

## 1. PDF, OCR, and Tables

| Component | Stars | License | Decision | Rationale |
|---|---:|---|---|---|
| [pypdf](https://github.com/py-pdf/pypdf) | 10,151 | BSD-3-Clause | Keep for P0 | Pure Python, Windows-friendly, lowest migration cost; pin at least 6.15.0 and apply input limits |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 10,650 | MIT | P1 coordinate candidate | Character/word/page objects and table cells; narrow adapter possible |
| [Camelot](https://github.com/camelot-dev/camelot) | 3,809 | MIT | P1 table candidate | Text-PDF table extraction and 2.x cross-page `stack_contiguous()` support |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | 34,413 | MPL-2.0 | Conditional P1 OCR preprocessor | Produces searchable PDF; requires Tesseract/external binaries and process isolation |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 87,444 | Apache-2.0 | P1/P2 OCR Spike | Strong Chinese/layout/table support; model/runtime weight requires isolated evaluation |
| [Docling](https://github.com/docling-project/docling) | 64,612 | MIT, model licenses vary | Evaluated P2 adapter (`OPENMATRIX_PARSER=docling`) | MIT permissive, reading-order & table layout; env-gated `DoclingParserAdapter` fallback to pypdf (Issue #78) |
| [MinerU](https://github.com/opendatalab/MinerU) | 77,369 | Custom Apache-derived | Evaluated; Deferred P2 | Evaluated in Issue #78 benchmark; state-of-the-art layout & VLM/OCR but deferred pending custom license review and heavy vLLM/GPU runtime requirements |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | 10,447 | AGPL-3.0 or commercial | Blocked without commercial license | Technically strong, but AGPL is incompatible with delegated automatic adoption |
| [Unstructured](https://github.com/Unstructured-IO/unstructured) | 15,297 | Apache-2.0 | Deferred | Dependency and attack surface are too broad for the current PDF-only product |

Recommended interfaces:

```text
ParserAdapter
TableExtractorAdapter
OcrPreprocessor
```

Do not expose third-party page/table objects through the product API. Normalize page numbers, text spans, coordinates, parser version, warnings, and table cells first.

## 2. Retrieval and Reranking

| Component | Stars | License | Decision | Rationale |
|---|---:|---|---|---|
| [bm25s](https://github.com/xhluca/bm25s) | 1,762 | MIT | Preferred sparse candidate | Small NumPy-only core, persistence/mmap support, appropriate for a narrow adapter |
| [FastEmbed](https://github.com/qdrant/fastembed) | 3,135 | Apache-2.0 | Integrated P2 adapter (Issue #76) | ONNX local path, singleton caching, cosine score filtering, env-gated mode |
| [Qdrant](https://github.com/qdrant/qdrant) | 33,921 | Apache-2.0 | Integrated P2 adapter (Issue #79) | `QdrantVectorStoreAdapter` + `InMemoryVectorStore` fallback behind `VectorStoreAdapter` Protocol |
| [sentence-transformers](https://github.com/huggingface/sentence-transformers) | 18,988 | Apache-2.0 | P2 quality baseline | Mature embeddings/CrossEncoder ecosystem, but heavy runtime |
| [FlagEmbedding](https://github.com/FlagOpen/FlagEmbedding) | 12,039 | MIT | Model reference, not default package | Good BGE models; full package dependency surface is too heavy |
| [Haystack](https://github.com/deepset-ai/haystack) | 26,176 | Apache-2.0 | P2 framework Spike | Strong hierarchical/auto-merge concepts; framework adoption is not required |
| [LlamaIndex](https://github.com/run-llama/llama_index) | 51,560 | MIT | Reference/Spike only | Broad retrieval patterns, but high object-model lock-in risk |
| [LangChain](https://github.com/langchain-ai/langchain) | 143,985 | MIT | Not shortlisted for core retrieval | Rapid package/API changes and broader agent focus |
| [Chroma](https://github.com/chroma-core/chroma) | 29,018 | Apache-2.0 | Prototype only | Easy local prototype but broad dependency surface and weaker long-term fit than Qdrant |
| [FAISS](https://github.com/facebookresearch/faiss) | 40,713 | MIT | ANN benchmark only | Mature index kernel, not a complete metadata/result store |

P0/P0-B preferred path:

```text
page-aware parent/child chunks
→ bm25s child retrieval
→ parent expansion
→ IdentityReranker
→ bounded context to the LLM
→ exact citation verification against original pages
```

Recommended interfaces:

```text
RetrieverAdapter
RerankerAdapter
ParentExpander
ReciprocalRankFusion
```

## 3. Grid, PDF Viewer, and Citation UI

| Component | Stars | License | Decision | Rationale |
|---|---:|---|---|---|
| [AG Grid](https://github.com/ag-grid/ag-grid) | 15,528 | Community MIT; Enterprise EULA | P0-B primary candidate | Mature virtualization, status renderers, keyboard support, CSV; use Community imports only |
| [TanStack Table](https://github.com/TanStack/table) | 28,311 | MIT | P0-B lightweight alternative | Small, headless, highly adaptable; requires more Grid engineering |
| [TanStack Virtual](https://github.com/TanStack/virtual) | 7,057 | MIT | Pair with TanStack Table | Mature virtualization primitive |
| [Glide Data Grid](https://github.com/glideapps/glide-data-grid) | 5,298 | MIT | P1 huge read-only matrix | Strong Canvas performance; weaker accessibility and complex-cell ergonomics |
| [MUI X](https://github.com/mui/mui-x) | 5,829 | Community MIT; paid tiers | Only with an existing MUI stack | Free feature boundary is tighter than AG Grid Community |
| [PDF.js](https://github.com/mozilla/pdf.js) | 53,719 | Apache-2.0 | P1 viewer standard | Mature viewer/text/annotation engine; isolate worker and normalize coordinates |
| [react-pdf](https://github.com/wojtekmaj/react-pdf) | 11,140 | MIT | Optional React shell | Useful if the full PDF.js viewer is unnecessary |
| [react-pdf-highlighter](https://github.com/agentcooper/react-pdf-highlighter) | 1,402 | MIT | Reference or bounded PoC | Maintenance and PDF.js-version lag make it unsuitable as the core layer |
| [Vercel AI Elements](https://github.com/vercel/ai-elements) | 2,313 | Apache-2.0 | Citation UI reference | Reuse selected UI concepts/source only behind OpenMatrix citation contracts |
| [Handsontable](https://github.com/handsontable/handsontable) | 22,014 | Proprietary/non-commercial boundary | Rejected | Current versions are not a free commercial Community Edition |

Recommended interfaces:

```text
GridAdapter
ExportAdapter
PdfViewerAdapter
PdfHighlightOverlay
CitationRendererAdapter
CitationNavigator
```

Grid and viewer vendor APIs must not enter matrix/citation domain models. CSV export must protect against formula injection. XLSX should remain independent from paid Grid features.

## 4. Queue, Retry, SSE, and Storage

| Component | Stars | License | Decision | Rationale |
|---|---:|---|---|---|
| [AnyIO](https://github.com/agronholm/anyio) | 2,523 | MIT | Preferred structured concurrency | Natural FastAPI/Starlette fit; use a limiter of 2-3 |
| [Tenacity](https://github.com/jd/tenacity) | 8,742 | Apache-2.0 | Preferred transient retry candidate | Async support and bounded retry policies |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | 1,570 | MIT | Preferred P0-B result/queue persistence | Minimal async SQLite adapter; keep one writer |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | 12,063 | MIT | Later persistence upgrade | Mature migration path to PostgreSQL; unnecessary for the first small schema |
| [ARQ](https://github.com/python-arq/arq) | 2,999 | MIT | First distributed-worker upgrade | Async-native and Redis-backed; too much infrastructure for P0-B |
| [RQ](https://github.com/rq/rq) | 10,669 | BSD | Deferred | Redis and sync worker model are unnecessary now |
| [Dramatiq](https://github.com/Bogdanp/dramatiq) | 5,307 | LGPL-3.0 | Deferred/gated | Good reliability/metrics but broker and LGPL obligations add complexity |
| [Celery](https://github.com/celery/celery) | 28,784 | BSD | Rejected for Windows P0-B | Official Windows support mismatch and excessive infrastructure |
| [Huey](https://github.com/coleifer/huey) | 6,000 | MIT | Not preferred | SQLite option is attractive, but at-least-once is not guaranteed and async fit is weak |

Preferred P0-B composition:

```text
FastAPI native SSE
+ AnyIO TaskGroup / CapacityLimiter(2-3)
+ asyncio.Queue for wake-up only
+ Tenacity for transient provider failures
+ aiosqlite single-writer persistence
+ SQLite tasks/results/events as the source of truth
```

Recommended interfaces:

```text
TaskQueueAdapter
ResultStoreAdapter
```

The durable SQLite write must occur before any in-memory wake-up signal. Do not store provider tokens in task payloads, SQLite, SSE, or logs.

## Adoption Rules

The delegated supervisor may approve MIT, Apache-2.0, BSD, ISC, and MPL-2.0 components within an approved Issue after recording the evidence. LGPL requires isolation and distribution-obligation review. AGPL, GPL, SSPL, BSL, non-commercial, source-available, custom, unknown, or conflicting licenses remain blocked.

Each adopted component requires:

1. Official source and license evidence.
2. Maintenance/release evidence.
3. A narrow OpenMatrix adapter contract.
4. Focused acceptance and regression tests.
5. Windows installation/runtime verification.
6. Security, privacy, resource, and dependency review.
7. A rollback path that restores the prior adapter.

## Proposed First Component Issues

These are candidates for queue approval, not automatic implementation approval:

1. **Browser provider-token storage hardening**: remove default persistent browser storage and add regression tests. No new component required.
2. **P0-B 3x3 matrix execution foundation**: reuse FastAPI/asyncio; evaluate AnyIO, Tenacity, and aiosqlite behind `TaskQueueAdapter` / `ResultStoreAdapter`.
3. **Page-aware retrieval and outbound-context reduction**: implement OpenMatrix chunk contracts and evaluate `bm25s` behind `RetrieverAdapter` without introducing a vector database.

## P2 Candidate Intakes (drafted, not approved)

Each candidate below requires explicit Issue scope, license verification, and acceptance tests before integration.

| Intake | Capability | Primary component | License | Risk |
|---|---|---|---|---|
| P2-1 | Dense retrieval on production default path | FastEmbed + TextCrossEncoder rerank | Apache-2.0 | Low; model download/offline fallback |
| P2-2 | Parser backend evaluation (Docling/MinerU) | Docling (MIT) / MinerU (custom Apache-derived) | MIT / custom | MinerU license must be approved; heavy deps |
| P2-3 | Vector storage + multi-doc batch analysis | Qdrant | Apache-2.0 | New service process; CI/ops complexity |
| P2-4 | Async task queue / multi-process orchestration | ARQ+Redis or asyncio queue | MIT / BSD | Redis dependency; queue semantics vs aiosqlite source of truth |

OCR, advanced PDF tables, AG Grid migration, PDF.js highlighting, and Qdrant remain researched candidates for later roadmap stages unless CHECKPOINT explicitly promotes one.
