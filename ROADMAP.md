# OpenMatrix Product Roadmap

OpenMatrix focuses on **evidence-first, auditable AI document research** — pairing transparent retrieval and verification with self-hosted deployment and open MCP interoperability.

---

## Shipped in `v0.1.0`

- **Research Desk UI (React 19 + TypeScript + Ant Design + AG Grid Community)**
  - Bilingual English/Chinese interface with automatic browser-language detection.
  - Projects library (PDF/DOCX/TXT ingestion), Analysis readiness checks, Results drawer with **Answer Support** verification badges (`supported | partial | unsupported`), PDF Evidence Viewer with jump-to-page & quote highlighting, Agent Console, SSE Execution Trace, Quality Dashboard, and Settings.
- **Verification & Financial Accuracy**
  - Physical page preservation across PDF parsing and retrieval.
  - Multi-layer quote matching (exact, normalized, fuzzy) plus deterministic numeric/entity `assess_answer_support`.
  - Financial KPI extraction (`extract_kpis`) with scale detection (`thousands` / `millions`), year-header rejection, and footnote superscript filtering.
- **52 MCP Tools & Bounded Agent Orchestration**
  - Grid-free multi-document analysis (`analyze_documents`), interruptible `research_run` state machine, deterministic rule planner, opt-in LLM planner, and SQLite-persisted schedules.
  - Built-in SEC EDGAR (annual FY XBRL + `www.sec.gov/Archives` filing chunks), AkShare, FRED, and SSRF-hardened web retrieval (`fetch_context`).
- **Multi-Format Deliverable Exports**
  - CSV, XLSX, PDF (ReportLab with long-table splitting), DOCX (`python-docx`), and citation-footnoted PPTX slides (`python-pptx`).
- **Production Security & Storage Readiness**
  - Automatic Bearer auth on non-loopback network bindings, strict CORS, credential redaction, loopback-bound Docker Compose defaults, and optional PostgreSQL + S3/MinIO backends.

---

## Planned for `v0.2.0`

1. **Content-Hashed Parsing Cache (Speedup)**
   - Optional user-isolated parsing cache keyed by `(file_sha256, parser_version, parser_options)` — controlled benchmarks on 154-page SEC 10-K filings observed ~75% end-to-end latency reduction on repeated document hits.
2. **Optional Semantic Support Verifier**
   - Opt-in LLM semantic judge (`OPENMATRIX_SUPPORT_JUDGE=llm`) for ambiguous wording, unit mismatches, or cross-period label swaps when deterministic checks flag `partial` or `needs_review`.
3. **Enhanced LLM Planner & Synthesis Controls**
   - Expose structured multi-step synthesis evidence links cleanly in the Research Desk Agent UI and expand planner benchmark suites.
4. **Community & Evaluation Datasets**
   - Public, reproducible multi-filing benchmark suite using SEC EDGAR 10-K/10-Q filings to track citation precision, answer-support accuracy, and latency across releases.

---

## Longer-Term (`v0.3.0+`)

- Pluggable OCR / complex layout parser presets (OCRmyPDF / Docling) behind strict resource/timeout guardrails.
- Multi-user workspace RBAC & OAuth/OIDC reverse-proxy deployment templates.
- Expanded open-data connectors (macroeconomic series, international filings, structured spreadsheets).
