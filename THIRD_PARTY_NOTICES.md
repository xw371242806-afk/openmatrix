# Third-Party Notices

OpenMatrix uses the following third-party open-source packages. This inventory reflects `requirements.txt`, `requirements.lock`, and `ui/package.json` as of `v0.1.0` (2026-11-20). Always verify upstream `LICENSE` and `NOTICE` files before redistributing binaries or container images.

## Backend Python Dependencies (`requirements.txt` / `requirements.lock`)

| Package | Locked Version | License | Notes |
|---|---|---|---|
| `fastapi` | `0.143.0` | MIT | Web & REST API framework |
| `uvicorn` | `0.54.0` | BSD-3-Clause | ASGI server |
| `python-multipart` | `0.0.32` | Apache-2.0 | Multipart form upload handling |
| `openai` | `3.26.1` | Apache-2.0 | OpenAI-compatible LLM SDK |
| `httpx` | `0.28.1` | BSD-3-Clause | Async/sync HTTP client |
| `pypdf` | `6.19.0` | BSD-3-Clause | Default PDF text & physical-page extraction |
| `pdfplumber` | `0.11.10` | MIT | Coordinate/layout-aware PDF inspection (uses `pdfminer-six` [MIT] / `pypdfium2` [Apache-2.0 / BSD-3-Clause]) |
| `bm25s` | `0.3.13` | MIT | Fast lexical BM25 retrieval |
| `camelot-py` | `2.0.0` | MIT | Optional PDF table extraction |
| `tenacity` | `9.2.1` | Apache-2.0 | Retry policies for transient provider errors |
| `aiosqlite` | `0.22.1` | MIT | Default async SQLite persistence |
| `openpyxl` | `3.1.5` | MIT | XLSX workbook export |
| `reportlab` | `5.0.1` | BSD-3-Clause | PDF report export |
| `python-docx` | `1.2.0` | MIT | Word DOCX report export |
| `python-pptx` | `1.0.2` | MIT | PowerPoint PPTX citation-footnoted slide export |
| `mcp` | `1.29.0` | MIT | Official Model Context Protocol Python SDK (FastMCP) |
| `psycopg` (`psycopg[binary]`) | `3.3.6` | LGPL-3.0 | Optional PostgreSQL production storage backend |
| `boto3` | `1.43.110` | Apache-2.0 | Optional S3/MinIO object storage backend |

## Frontend UI Dependencies (`ui/package.json`)

| Package | Version Spec | License | Notes |
|---|---|---|---|
| `react` / `react-dom` | `^19.2.8` | MIT | UI framework |
| `antd` / `@ant-design/icons` / `@ant-design/x` | `^6.6.0` / `^6.3.2` / `^2.9.0` | MIT | Research Desk component library |
| `ag-grid-community` / `ag-grid-react` | `^36.1.0` | MIT | Data table community edition (no Enterprise features used) |
| `echarts` / `echarts-for-react` | `^6.1.0` / `^3.0.6` | Apache-2.0 / MIT | Quality & verification metrics charts |
| `i18next` / `react-i18next` | `^26.4.0` / `^17.0.12` | MIT | Bilingual (`en` / `zh`) localization |
| `pdfjs-dist` | `^6.2.108` | Apache-2.0 | Client-side PDF page rendering & text highlighting |

## Optional / Extra Packages (`requirements-kg.txt` & Optional Paths)

- `fastembed`, `qdrant-client`, `docling` / `docling-parse` (Apache-2.0 / MIT; optional retrieval/parser integrations)
- `OCRmyPDF` (MPL-2.0; optional OCR preprocessing)
- `LightRAG` (MIT; optional experimental evaluation path, disabled by default)

## Excluded Incompatible Components (Not Bundled)

The following components were evaluated and deliberately **excluded** from OpenMatrix due to AGPL copyleft, non-commercial (`CC-BY-NC`), or commercial-license restrictions:

- `PyMuPDF` (AGPL-3.0 / commercial)
- `Handsontable` (commercial)
- `sec-edgar-mcp` server layer (AGPL-3.0) / `OpenBB` (AGPL-3.0)
- `NV-Embed-v1/v2` & `jina-embeddings-v3` (CC-BY-NC)

## Attribution & Patent Note

- Retain upstream copyright and license notices in all source and binary redistributions per Apache-2.0, MIT, BSD-3-Clause, and LGPL-3.0 terms.
- Open-source software licenses do not grant licenses under third-party patents. See [PATENT_COMPLIANCE.md](PATENT_COMPLIANCE.md).
