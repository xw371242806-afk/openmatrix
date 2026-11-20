# OpenMatrix Intellectual Property & Patent Compliance Guide

> Technical engineering reference comparing OpenMatrix architecture against published U.S. patent claims (`US12393788`, `US12626067`, `US12271707`).
> **Disclaimer:** This document is an engineering risk-management note, **not** legal advice or a Freedom-to-Operate (FTO) opinion. Consult qualified patent counsel before commercializing or deploying in jurisdictions of concern.

---

## 0. Summary Matrix

| Capability / Architecture Path | Engineering Risk Assessment | Guidance |
|---|---|---|
| **Grid-free `analyze_documents` (Document List + Independent Q&A + Summary)** | Lower technical overlap | **Recommended default** multi-document workflow in OpenMatrix |
| **Token attention-score → `cell_score` → UI highlighting** | **Prohibited in OpenMatrix** | Strictly excluded (`US12626067`); OpenMatrix uses deterministic text/bounding-box matching only |
| **Row × Column Grid + free-text column prompts + per-cell LLM fill** | Elevated overlap risk (`US12393788`) | Retained in codebase for local evaluation only; consult counsel before US commercial SaaS exposure |
| **Chat query → select grid cells → subqueries → generate deliverable** | Elevated overlap risk (`US12271707`) | OpenMatrix exports/synthesis must consume retrieved document evidence directly, never intermediate grid-cell selection |
| **Document chunking, BM25/dense retrieval, quote string/fuzzy matching, multi-model routing** | General/standard techniques | Core OpenMatrix foundation |

---

## 1. Engineering Guardrails Enforced in OpenMatrix

1. **Zero Attention-Score Highlighting (`US12626067`)**:
   - OpenMatrix **never** accesses model token attention weights or computes attention-derived `cell_score` heatmaps.
   - Evidence highlighting in the PDF Viewer (`ui/src/PdfViewerPage.tsx` and `static/js/pdf-viewer.js`) uses exact, normalized, fuzzy text matching and PDF text bounding boxes (`pdfplumber` / `PDF.js`).
2. **Agent Tool Allowlist Isolation (`US12271707`)**:
   - `agent/tools.py` explicitly excludes matrix-cell construction tools from the autonomous agent allowlist.
   - `analyze_documents` executes **Document List → Independent Retrieval → Grounded Q&A → Summary Report** without constructing a row×column cell grid.
3. **Export Deliverables (`US12271707`)**:
   - `export_report` (PDF/XLSX/DOCX) and `export_slides` (PPTX with bottom citation footnotes) operate as pure serialization/formatting adapters over structured Q&A records rather than chat-selected grid-cell subqueries.
4. **Permissive Open-Source Dependency Hygiene**:
   - All core and optional dependencies are audited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (MIT / Apache-2.0 / BSD-3-Clause; optional `psycopg` LGPL-3.0). AGPL and non-commercial (`CC-BY-NC`) components are excluded.

---

## 2. Guidance for Contributors and Deployers

- **Do NOT** introduce token-level attention-score extraction or cell-level attention heatmaps.
- **Do NOT** wire the Research Agent or chat synthesis to route through "relevant grid cell selection + subquery generation over grid cells."
- **Do NOT** copy proprietary source code or assets from commercial vendors.
- If deploying OpenMatrix commercially (especially in the United States), consult qualified IP counsel regarding UI presentation choices (e.g., whether to keep list/card/report-only views vs. matrix table views).
