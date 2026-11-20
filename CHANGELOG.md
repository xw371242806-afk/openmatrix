# Changelog

All notable changes to **OpenMatrix** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-20

Initial public open-source release of **OpenMatrix** (Apache-2.0) — a self-hosted, evidence-first AI research desk.

### Added
- **Evidence-First Research Desk (React 19 + Vite + TypeScript + Ant Design + AG Grid)**:
  - Overview, Projects library, Analysis readiness checks, Results drawer, Evidence PDF viewer with jump-to-page & quote highlighting, Quality review, Agent console, Execution Trace (SSE), and Settings.
  - Bilingual UI (`en` / `zh`) with browser-language auto-detection and manual override.
- **Verification & Citation Grounding**:
  - Exact, normalized, and fuzzy quote matching with physical page preservation (`pypdf` / `pdfplumber`).
  - Answer-support assessment (`supported | partial | unsupported | unchecked`) comparing numeric/entity claims in answers against cited quotes.
  - Raised PDF footnote handling and financial KPI extraction (`extract_kpis`, `compute_financials`) with scale normalization and year-header rejection.
- **52 MCP Tools & Agent Orchestration (`mcp_server.py` + `agent/`)**:
  - Grid-free multi-document analysis workflow (`analyze_documents`), bounded `research_run` state machine, deterministic rule-based planner, and opt-in LLM planner (`OPENMATRIX_LLM_PLANNER`).
  - Key-free SEC EDGAR (annual FY XBRL selection + `www.sec.gov/Archives` links), AkShare, FRED, and SSRF-hardened URL retrieval (`fetch_context`).
  - Export deliverables to CSV, XLSX, PDF (ReportLab), DOCX (`python-docx`), and citation-footnoted PPTX slides (`python-pptx`).
- **Deployment & Security Hardening**:
  - Auto-enabled Bearer authentication when binding beyond loopback (`OPENMATRIX_ENABLE_AUTH=auto`, auto-generated `master_key.txt`).
  - Strict CORS allowlists, credential redaction in logs/errors, streaming upload size caps, and loopback-bound Docker Compose defaults (`SQLite` + local disk default; optional `PostgreSQL` + `S3/MinIO`).
  - Portable Windows launcher (`start.bat` / `setup_windows.ps1`) and clean release packaging (`scripts/make_release.py`).
