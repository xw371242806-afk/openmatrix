# OpenMatrix UI - Research Desk

React 19 + Vite + TypeScript research workspace served by the FastAPI application.

## Develop

```bash
cd ui
npm ci
npm run dev        # http://localhost:5173
```

## Build & serve from the FastAPI app

```bash
cd ui
npm run build      # outputs to ui/dist (gitignored)
```

The FastAPI app (`main.py`) serves the built shell at `http://localhost:<port>/ui/`
when `ui/dist/index.html` exists.

## Pages

- Overview: real run, project, schedule, and quality summaries
- Projects: document library, indexed-text ingestion, and retrieval evidence
- Analysis: batch and chained PDF analysis with confirmation and export
- Results: recent runs, status filters, AG Grid cell details, CSV/XLSX export
- Research Agent: real research-run status, steps, cancellation, and schedules
- Audit Trail: matrix SSE event ledger with hash deep links
- Quality Review: six real statistics rendered with ECharts
- Evidence Viewer: PDF.js rendering, quote matching, and bbox overlays
- Settings: language, API-key management, model routing, health, and audit

## Authentication

When `OPENMATRIX_ENABLE_AUTH` is enabled, use the key control in the top bar to
enter an OpenMatrix bearer token. The token is kept in JavaScript memory only:
it is not written to browser storage, logs, or the URL, and is cleared on page
refresh. Provider BYOK keys remain separate and are sent only with an analysis
request.

## Verify

```bash
cd ui
npm ci
npm test
npm run build
npm run lint
```

From the repository root, with the FastAPI service running:

```bash
venv\Scripts\python.exe scripts\ui_smoke_test.py
```

The authoritative lock file is `ui/package-lock.json`. Do not add a second
pnpm lock file unless the repository and CI are intentionally migrated.

See `../UI-HANDOFF.md` for design decisions, API boundaries, screenshots,
known limitations, and the complete verification record.
