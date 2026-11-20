import os
import json
import logging
import asyncio
import hashlib
import math
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import numpy as np
import bm25s
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
import httpx
from contextlib import closing
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, Query, Body, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from core.model_provider import model_request_headers
from pypdf import PdfReader
from pypdf.errors import PdfReadError

if os.getenv("OPENMATRIX_USE_SYSTEM_PROXY", "").lower() not in {"1", "true", "yes", "on"}:
    for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
        os.environ.pop(k, None)

def _cors_origins():
    configured = os.environ.get("OPENMATRIX_CORS_ORIGINS")
    if configured is not None:
        return [value.strip().rstrip("/") for value in configured.split(",") if value.strip()]
    port = os.environ.get("OPENMATRIX_PORT", "8000")
    return list(dict.fromkeys([f"http://localhost:{port}", f"http://127.0.0.1:{port}",
                              "http://localhost:5173", "http://127.0.0.1:5173"]))



_S4_REDACTION_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,}\"']+"),
    re.compile(r"(?i)(\bbearer\s+)[^\s,}\"']+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|secret|password|token)\b\s*[:=]\s*[\"']?)[^,\s}\"']+"),
    re.compile(r"(?i)([?&](?:access_token|token|key)=)[^&#\s]+"),
)


def _redact_sensitive_text(value: object, *known_secrets: object) -> str:
    text = str(value)
    for secret in known_secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED_SECRET]")
    text = re.sub(r"(?i)([?&]api_key=)[^&#\s]+", r"\1***", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    for pattern in _S4_REDACTION_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
        text = pattern.sub(replacement, text)
    text = re.sub(r"(?i)([?&]api_key=)[^&#\s]+", r"\1***", text)
    return text


class SensitiveDataFilter(logging.Filter):
    """Redact query strings, bearer tokens, API keys, and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
                # AccessFormatter consumes this tuple separately from getMessage().
                record.args = tuple(_redact_sensitive_text(arg) if isinstance(arg, str) else arg for arg in record.args)
            else:
                record.msg = _redact_sensitive_text(record.getMessage())
                record.args = ()
            if record.exc_info:
                record.exc_text = _redact_sensitive_text(
                    record.exc_text or logging.Formatter().formatException(record.exc_info)
                )
                record.exc_info = None
        except Exception:
            record.msg = "[REDACTED_LOG_MESSAGE]"
            record.args = ()
        return True


def _install_sensitive_log_filter() -> None:
    loggers = [logging.getLogger(), logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access"), logging.getLogger("uvicorn.error")]
    for logger in loggers:
        if not any(isinstance(item, SensitiveDataFilter) for item in logger.filters):
            logger.addFilter(SensitiveDataFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
                handler.addFilter(SensitiveDataFilter())

app = FastAPI(title="OpenMatrix")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# ---- P6: React UI shell (Vite build in ui/dist) ----


def _mount_ui_shell(app, ui_index: Path | None = None) -> bool:
    """Mount the React UI shell under /ui when the built index.html exists.

    Returns True if mounted. The guard is the *file* ui/dist/index.html, not
    merely the dist directory, so an asset-only dist/ never exposes /ui.
    """
    index = ui_index if ui_index is not None else Path("ui") / "dist" / "index.html"
    if index.is_file():
        app.mount("/ui", StaticFiles(directory=str(index.parent), html=True), name="ui")
        return True
    return False


_mount_ui_shell(app)

# ---- B1/B2/B3: API-key REST + audit + bearer-auth middleware (master-key gated) ----
from apikeys import default_key_store, require_master_key, master_key_configured
from audit import default_audit_store
from fastapi import Depends, Header, HTTPException

# P0-A optimization: task-type LLM routing (disabled unless OPENMATRIX_ROUTER_ENABLED=true)
from model_router import TASK_BATCH, TASK_CHAIN, TASK_FAST, ModelRouter
from core.answer_support import assess_answer_support


def _keep_source_files() -> bool:
    return os.getenv("OPENMATRIX_KEEP_SOURCE_FILES", "true").strip().lower() not in {"0", "false", "no", "off"}


def _run_files_root() -> Path:
    data_dir = Path(os.getenv("OPENMATRIX_DATA_DIR", str(_DEFAULT_RUN_DB_DIR))).resolve()
    root = (data_dir / "run_files").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_run_file_path(run_id: str, document_index: int) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("invalid run_id")
    if not isinstance(document_index, int) or isinstance(document_index, bool) or document_index < 0:
        raise ValueError("document_index must be a non-negative integer")
    root = _run_files_root()
    path = (root / run_id / f"{document_index}.pdf").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("source path escapes run_files root") from exc
    return path


def _save_run_source(temp_path: str, run_id: str, document_index: int) -> Path | None:
    if not _keep_source_files():
        return None
    import shutil
    destination = _safe_run_file_path(run_id, document_index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".pdf.tmp")
    shutil.copyfile(temp_path, temporary)
    temporary.replace(destination)
    return destination
from core.evidence import build_browser_snapshot
from agent.tasks import default_task_manager
from core.adapters.kg_adapter import (
    GraphHit,
    LightRagKnowledgeGraphAdapter,
    SectionDocument,
    create_kg_adapter,
)


# P7-1 RBAC: these /api/v1/* paths require an admin role (or the master key).
_ADMIN_ONLY_PATHS = {
    "/api/v1/api-keys",
    "/api/v1/audit",
}


from core.access_auth import auth_enabled, get_master_key


def _auth_enabled():
    enabled=auth_enabled()
    if enabled: get_master_key(create=True)
    return enabled


@app.on_event("startup")
async def _initialize_auth():
    _install_sensitive_log_filter()
    _auth_enabled()
    from agent.scheduler import default_manager
    default_manager(_schedule_research_callback).start_ticker()


async def require_api_v1_auth_dependency(authorization: str | None = Header(default=None)):
    """Explicit /api/v1 route guard for OpenAPI/route-level visibility.

    The middleware below remains the authoritative protection for every
    /api/v1/* path. This dependency mirrors that behavior for v1 routes that
    also have legacy unversioned aliases, so the authenticated route is visible
    at the decorator level without breaking legacy compatibility.
    """
    if not _auth_enabled():
        return None
    auth = authorization or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    store = default_key_store()
    verified = None if require_master_key(token) else await store.verify_key(token)
    if verified is None and not require_master_key(token):
        raise HTTPException(status_code=401, detail="invalid API key")
    return verified or {"role": "master"}


@app.on_event("shutdown")
async def _stop_schedule_ticker():
    from agent import scheduler
    if scheduler._default_manager is not None:
        scheduler._default_manager.stop_ticker()


@app.middleware("http")
async def _api_key_auth_middleware(request, call_next):
    """B3: authenticate protected bearer requests with a server API key.

    When ``OPENMATRIX_ENABLE_AUTH`` is unset/off, pass through (dev mode).
    When enabled, protected /api/v1/* routes and matrix result cells require
    ``Authorization: Bearer <api key>`` and the key must verify against the
    ApiKeyStore. The master key is accepted as a management credential.
    """
    if not _auth_enabled():
        return await call_next(request)
    path = request.url.path
    protected_legacy_prefixes = (
        "/api/matrix/",
        "/api/matrix-runs/",
        "/api/export-csv/",
        "/api/export-xlsx/",
        "/api/runs/",
    )
    protected_legacy_paths = {
        "/api/analyze",
        "/api/models",
        "/api/analyze_chain",
        "/api/analyze_batch",
        "/api/analyze-batch",
    }
    protected = (
        path.startswith("/api/v1/")
        or path.startswith(protected_legacy_prefixes)
        or path in protected_legacy_paths
    )
    if not protected or request.method == "OPTIONS" or path in ("/api/v1/health",):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    store = default_key_store()
    verified = None if require_master_key(token) else await store.verify_key(token)
    if verified is None and not require_master_key(token):
        return JSONResponse({"error": "invalid API key"}, status_code=401)
    # P7-1 RBAC: admin-only paths require the caller to be the master key or an
    # admin role key. member/readonly keys are denied with 403.
    is_master = verified is None and require_master_key(token)
    role = (verified or {}).get("role", "member")
    if path in _ADMIN_ONLY_PATHS and not is_master and role != "admin":
        return JSONResponse({"error": "admin role required"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _origin_guard(request, call_next):
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD", "OPTIONS"} and origin not in _cors_origins():
        return JSONResponse({"error": "origin is not allowed"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _structured_log_middleware(request, call_next):
    """Structured request logging (observability)."""
    import json as _json

    start = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        print(_json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }, ensure_ascii=False))
        return response
    except Exception as exc:  # noqa: BLE001
        print(_json.dumps({
            "event": "request_error",
            "method": request.method,
            "path": request.url.path,
            "error": _redact_sensitive_text(exc, locals().get("api_key")),
        }, ensure_ascii=False))
        raise


@app.get("/api/v1/api-keys")
async def api_keys_list(authorization: str = Header(default="")):
    """B1: list API key metadata (master-key gated; never returns secrets)."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not require_master_key(token):
        raise HTTPException(status_code=401, detail="master key required")
    keys = await default_key_store().list_keys()
    return {"api_keys": keys}


@app.post("/api/v1/api-keys")
async def api_keys_create(name: str, authorization: str = Header(default="")):
    """B1: create an API key; returns the secret ONCE (master-key gated)."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not require_master_key(token):
        raise HTTPException(status_code=401, detail="master key required")
    try:
        rec = await default_key_store().create_key(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_redact_sensitive_text(exc, locals().get("api_key")))
    await default_audit_store().append("api_key_create", actor="master", detail=rec["id"])
    return rec


@app.delete("/api/v1/api-keys/{key_id}")
async def api_keys_revoke(key_id: str, authorization: str = Header(default="")):
    """B1: revoke an API key (master-key gated)."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not require_master_key(token):
        raise HTTPException(status_code=401, detail="master key required")
    removed = await default_key_store().revoke_key(key_id)
    if not removed:
        raise HTTPException(status_code=404, detail="key not found")
    await default_audit_store().append("api_key_revoke", actor="master", detail=key_id)
    return {"key_id": key_id, "revoked": True}


@app.get("/api/v1/audit")
async def api_audit_list(limit: int = 20, authorization: str = Header(default="")):
    """B2: recent audit log entries (master-key gated)."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not require_master_key(token):
        raise HTTPException(status_code=401, detail="master key required")
    entries = await default_audit_store().recent(limit=max(1, min(limit, 100)))
    return {"audit": entries}


_DEFAULT_RUN_DB_DIR = Path(os.environ.get("OPENMATRIX_DATA_DIR", Path(__file__).resolve().parent / ".openmatrix"))
MATRIX_DB_PATH = Path(os.environ.get("OPENMATRIX_RUN_DB", _DEFAULT_RUN_DB_DIR)) / "openmatrix_runs.sqlite3"

MAX_PDF_BYTES = int(os.environ.get("OPENMATRIX_MAX_PDF_BYTES", 25 * 1024 * 1024))
MAX_PDF_PAGES = int(os.environ.get("OPENMATRIX_MAX_PDF_PAGES", 500))
MAX_BATCH_FILES = int(os.environ.get("OPENMATRIX_MAX_BATCH_FILES", 50))
KG_CONTEXT_MAX_CHARS = int(os.environ.get("OPENMATRIX_KG_CONTEXT_MAX_CHARS", "12000"))
KG_RETRIEVE_TIMEOUT_SECONDS = float(
    os.environ.get("OPENMATRIX_KG_RETRIEVE_TIMEOUT_SECONDS", "10")
)
KG_MAX_INDEX_PAGES = int(os.environ.get("OPENMATRIX_KG_MAX_INDEX_PAGES", "20"))
KG_MAX_LLM_TOKENS = int(os.environ.get("OPENMATRIX_KG_MAX_LLM_TOKENS", "100000"))
KG_MAX_EMBEDDING_TOKENS = int(os.environ.get("OPENMATRIX_KG_MAX_EMBEDDING_TOKENS", "100000"))
KG_INDEX_LOCK_TTL_SECONDS = int(os.environ.get("OPENMATRIX_KG_INDEX_LOCK_TTL_SECONDS", "3600"))
_KG_READY_MARKER = ".openmatrix-kg-ready"
_KG_LOCK_MARKER = ".openmatrix-kg-indexing"
_kg_index_tasks: dict[str, str] = {}
_kg_task_owners: dict[str, str] = {}
_kg_index_tasks_lock = threading.Lock()


def _kg_working_dir(corpus_id: str) -> str:
    root = Path(os.environ.get("OPENMATRIX_KG_STORAGE_ROOT", "./kg_storage"))
    return str(root / corpus_id)


def _kg_corpus_id(tenant_id: str, pdf_sha256: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{pdf_sha256}".encode("utf-8")).hexdigest()


def _create_kg_adapter(corpus_id: str):
    return create_kg_adapter(_kg_working_dir(corpus_id))


def _kg_ready_path(corpus_id: str) -> Path:
    return Path(_kg_working_dir(corpus_id)) / _KG_READY_MARKER


def _kg_lock_path(corpus_id: str) -> Path:
    return Path(_kg_working_dir(corpus_id)) / _KG_LOCK_MARKER


def _kg_corpus_ready(corpus_id: str) -> bool:
    return _kg_ready_path(corpus_id).is_file()


def _kg_acquire_index_lock(corpus_id: str) -> bool:
    """Acquire a cross-process indexing lock via atomic mkdir.

    Returns True when this process now owns the indexing lock, False when
    another worker already holds it (or the corpus is already ready). A lock
    directory older than ``KG_INDEX_LOCK_TTL_SECONDS`` is treated as stale
    (e.g. a crashed worker) and reclaimed so indexing is not disabled forever.
    """
    if _kg_corpus_ready(corpus_id):
        return False
    lock = _kg_lock_path(corpus_id)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir(parents=False)
        return True
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            return False
        if age > KG_INDEX_LOCK_TTL_SECONDS:
            try:
                lock.rmdir()
                lock.mkdir(parents=False)
                return True
            except OSError:
                return False
        return False


def _kg_release_index_lock(corpus_id: str) -> None:
    lock = _kg_lock_path(corpus_id)
    try:
        lock.rmdir()
    except FileNotFoundError:
        pass


def _clean_kg_evidence(text: str) -> str:
    return (
        text.replace("</KNOWLEDGE_GRAPH_EVIDENCE>", "")
        .replace("<KNOWLEDGE_GRAPH_EVIDENCE>", "")
        .replace("</graph_evidence>", "")
        .replace("<graph_evidence", "&lt;graph_evidence")
        .replace("</DOCUMENT>", "")
        .replace("<DOCUMENT>", "")
    )


def _format_kg_context(
    hits: list[GraphHit], document_name: str, max_chars: int | None = None
) -> str:
    limit = KG_CONTEXT_MAX_CHARS if max_chars is None else max_chars
    if limit <= 0:
        return ""
    blocks: list[str] = []
    wrapper_size = len("<KNOWLEDGE_GRAPH_EVIDENCE>\n\n</KNOWLEDGE_GRAPH_EVIDENCE>")
    used = wrapper_size
    for hit in hits:
        if hit.doc_id != document_name or hit.page is None or hit.chunk_id is None:
            continue
        text = _clean_kg_evidence(hit.text).strip()
        if not text:
            continue
        block = (
            "<graph_evidence>\n"
            f"[Page {hit.page} | {_clean_kg_evidence(str(hit.chunk_id))}] {text}\n"
            "</graph_evidence>"
        )
        if used + len(block) > limit:
            break
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    return (
        "<KNOWLEDGE_GRAPH_EVIDENCE>\n"
        + "\n".join(blocks)
        + "\n</KNOWLEDGE_GRAPH_EVIDENCE>"
    )


async def _retrieve_kg_context(
    corpus_id: str, document_name: str, question: str
) -> str:
    if not _kg_corpus_ready(corpus_id):
        return ""
    adapter = _create_kg_adapter(corpus_id)
    if not adapter.is_available():
        return ""
    try:
        hits = await asyncio.wait_for(
            adapter.retrieve(question, mode="mix"),
            timeout=KG_RETRIEVE_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""
    return _format_kg_context(hits, document_name)


async def _index_kg_pages(
    corpus_id: str,
    document_name: str,
    pages: list[str],
    total_page_count: int | None = None,
) -> dict[str, Any]:
    adapter = _create_kg_adapter(corpus_id)
    if not adapter.is_available():
        raise RuntimeError("KG adapter unavailable")
    sections: list[SectionDocument] = []
    section_pages: list[int] = []
    for page_number, page_text in enumerate(pages[:KG_MAX_INDEX_PAGES], start=1):
        if not page_text.strip():
            continue
        source_id = LightRagKnowledgeGraphAdapter.page_source_id(document_name, page_number)
        sections.append(
            SectionDocument(
                section_id=f"page-{page_number}",
                text=f"[Section page-{page_number}]\n{page_text.strip()}",
                source_id=source_id,
            )
        )
        section_pages.append(page_number)
    if not sections:
        indexed_count = 0
        result = None
    else:
        result = await adapter.index_sections(document_name, sections)
        if not result.success:
            raise RuntimeError(result.error or "KG batch indexing failed")
        usage = getattr(adapter, "usage_snapshot", lambda: {})()
        llm_tokens = usage.get("llm_prompt_tokens", 0) + usage.get("llm_completion_tokens", 0)
        embedding_tokens = usage.get("embedding_tokens", 0)
        if usage.get("usage_missing_calls", 0):
            raise RuntimeError("KG provider omitted token usage")
        if llm_tokens > KG_MAX_LLM_TOKENS:
            raise RuntimeError(f"KG LLM token budget exceeded: {llm_tokens} > {KG_MAX_LLM_TOKENS}")
        if embedding_tokens > KG_MAX_EMBEDDING_TOKENS:
            raise RuntimeError(f"KG embedding token budget exceeded: {embedding_tokens} > {KG_MAX_EMBEDDING_TOKENS}")
        indexed_count = len(sections)
    results = [
        {
            "page": page_number,
            "success": result.success if result is not None else False,
            "chunks_indexed": result.chunks_indexed if result is not None else None,
            "entities_indexed": result.entities_indexed if result is not None else None,
            "elapsed_seconds": result.elapsed_seconds if result is not None else 0.0,
            "error": result.error if result is not None else None,
        }
        for page_number in section_pages
    ]
    declared_total = total_page_count if total_page_count is not None else len(pages)
    truncated = declared_total > indexed_count
    complete = (not truncated) and indexed_count >= 1 and indexed_count == declared_total
    return {
        "corpus_id": corpus_id,
        "document_name": document_name,
        "indexed_pages": indexed_count,
        "truncated": truncated,
        "complete": complete,
        "pages": results,
        "usage": getattr(adapter, "usage_snapshot", lambda: {})(),
    }


def _mark_kg_corpus_ready(corpus_id: str) -> None:
    marker = _kg_ready_path(corpus_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text("ready\n", encoding="ascii")
    temporary.replace(marker)


def _submit_kg_index_task(
    tenant_id: str, corpus_id: str, document_name: str, pages: list[str]
) -> str | None:
    manager = default_task_manager()
    with _kg_index_tasks_lock:
        existing_id = _kg_index_tasks.get(corpus_id)
        if existing_id:
            existing = manager.status(existing_id)
            if existing and existing.get("status") in ("queued", "running"):
                return existing_id

        if not _kg_acquire_index_lock(corpus_id):
            # Another worker is indexing this corpus, or it is already ready.
            return None

        pages_for_index = list(pages[:KG_MAX_INDEX_PAGES])
        total_page_count = len(pages)

        async def run_kg_index(_payload):
            try:
                result = await _index_kg_pages(
                    corpus_id,
                    document_name,
                    pages_for_index,
                    total_page_count=total_page_count,
                )
                if result.get("complete"):
                    _mark_kg_corpus_ready(corpus_id)
                return result
            finally:
                _kg_release_index_lock(corpus_id)

        task_id = manager.submit(
            "kg_index_document",
            {
                "corpus_id": corpus_id,
                "document_name": document_name,
                "page_count": len(pages_for_index),
            },
            run_kg_index,
        )
        _kg_index_tasks[corpus_id] = task_id
        _kg_task_owners[task_id] = tenant_id
        return task_id


def matrix_db_path(db_path: Path | str | None = None) -> Path | str:
    return MATRIX_DB_PATH if db_path is None else db_path


def init_matrix_store(db_path: Path | str | None = None) -> None:
    db_path = matrix_db_path(db_path)
    db_path_text = os.fspath(db_path)
    if db_path_text != ":memory:":
        Path(db_path_text).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matrix_cells (
                run_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                document_index INTEGER NOT NULL,
                document_name TEXT NOT NULL,
                question_index INTEGER NOT NULL,
                question TEXT NOT NULL,
                status TEXT NOT NULL,
                answer TEXT,
                quote TEXT,
                citation_status TEXT,
                support_status TEXT,
                support_reason TEXT,
                page INTEGER,
                error TEXT,
                PRIMARY KEY (run_id, cell_id)
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(matrix_cells)")}
        for column in ("support_status", "support_reason"):
            if column not in columns:
                conn.execute(f"ALTER TABLE matrix_cells ADD COLUMN {column} TEXT")
        conn.commit()


def persist_matrix_cell(run_id: str, cell: dict, db_path: Path | str | None = None) -> None:
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO matrix_cells (
                run_id, cell_id, document_index, document_name, question_index, question,
                status, answer, quote, citation_status, page, error, support_status, support_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                cell["cell_id"],
                cell["document_index"],
                cell["document_name"],
                cell["question_index"],
                cell["question"],
                cell["status"],
                cell.get("answer"),
                cell.get("quote"),
                cell.get("citation_status"),
                cell.get("page"),
                cell.get("error"),
                cell.get("support_status"),
                cell.get("support_reason"),
            ),
        )
        conn.commit()


def load_matrix_run(run_id: str, db_path: Path | str | None = None) -> dict | None:
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM matrix_cells
            WHERE run_id = ?
            ORDER BY document_index, question_index
            """,
            (run_id,),
        ).fetchall()

    if not rows:
        return None
    return {"run_id": run_id, "cells": [dict(row) for row in rows]}


class AiosqliteResultStore:
    """Async single-writer result store backed by aiosqlite (MIT).

    Serializes all writes through one background connection so SQLite remains a
    single source of truth without multi-writer contention. Mirrors the sync
    ``matrix_cells`` schema so async and sync stores stay in lockstep.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS matrix_cells (
            run_id TEXT NOT NULL,
            cell_id TEXT NOT NULL,
            document_index INTEGER NOT NULL,
            document_name TEXT NOT NULL,
            question_index INTEGER NOT NULL,
            question TEXT NOT NULL,
            status TEXT NOT NULL,
            answer TEXT,
            quote TEXT,
            citation_status TEXT,
            support_status TEXT,
            support_reason TEXT,
            page INTEGER,
            error TEXT,
            PRIMARY KEY (run_id, cell_id)
        )
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = matrix_db_path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._open_lock: asyncio.Lock | None = None

    async def _ensure_open(self) -> "aiosqlite.Connection":
        if self._conn is not None:
            return self._conn
        if self._open_lock is None:
            self._open_lock = asyncio.Lock()
        async with self._open_lock:
            if self._conn is None:
                import aiosqlite

                self._conn = await aiosqlite.connect(self._db_path)
                await self._conn.execute(self.SCHEMA)
                cursor = await self._conn.execute("PRAGMA table_info(matrix_cells)")
                columns = {row[1] for row in await cursor.fetchall()}
                for column in ("support_status", "support_reason"):
                    if column not in columns:
                        await self._conn.execute(f"ALTER TABLE matrix_cells ADD COLUMN {column} TEXT")
                await self._conn.commit()
        return self._conn

    async def persist_cell(self, run_id: str, cell: dict) -> None:
        conn = await self._ensure_open()
        await conn.execute(
            """
            INSERT OR REPLACE INTO matrix_cells (
                run_id, cell_id, document_index, document_name, question_index, question,
                status, answer, quote, citation_status, page, error, support_status, support_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                cell["cell_id"],
                cell["document_index"],
                cell["document_name"],
                cell["question_index"],
                cell["question"],
                cell["status"],
                cell.get("answer"),
                cell.get("quote"),
                cell.get("citation_status"),
                cell.get("page"),
                cell.get("error"),
                cell.get("support_status"),
                cell.get("support_reason"),
            ),
        )
        await conn.commit()

    async def load_run(self, run_id: str) -> dict | None:
        import aiosqlite

        conn = await self._ensure_open()
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT * FROM matrix_cells
            WHERE run_id = ?
            ORDER BY document_index, question_index
            """,
            (run_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        return {"run_id": run_id, "cells": [dict(row) for row in rows]}

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "AiosqliteResultStore":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


def persist_analyzed_run(
    document_name: str,
    questions: list[str],
    rows: list[dict],
    db_path: Path | str | None = None,
) -> str:
    run_id = uuid.uuid4().hex
    for question_index, row in enumerate(rows):
        status = "completed"
        if row.get("citation_status") == "not_found":
            status = "needs_review"
        elif row.get("citation_status") == "not_applicable":
            status = "not_disclosed"
        cell = {
            "cell_id": f"d0q{question_index}",
            "document_index": 0,
            "document_name": document_name or "document.pdf",
            "question_index": question_index,
            "question": row.get("question") or questions[question_index],
            "status": status,
            "answer": row.get("answer"),
            "quote": row.get("quote"),
            "citation_status": row.get("citation_status"),
            "support_status": row.get("support_status"),
            "support_reason": row.get("support_reason"),
            "page": row.get("page"),
            "error": None,
        }
        persist_matrix_cell(run_id, cell, db_path)
    return run_id


async def execute_matrix_run(
    documents: list[dict],
    questions: list[str],
    answer_cell,
    max_concurrency: int = 3,
    db_path: Path | str | None = None,
    run_id: str | None = None,
) -> dict:
    db_path = matrix_db_path(db_path)
    run_id = run_id or uuid.uuid4().hex
    from anyio import CapacityLimiter

    limiter = CapacityLimiter(max(1, max_concurrency))
    cells = []

    async with AiosqliteResultStore(db_path) as store:
        async def run_cell(document_index: int, document: dict, question_index: int, question: str) -> dict:
            cell = {
                "cell_id": f"d{document_index}q{question_index}",
                "document_index": document_index,
                "document_name": document["name"],
                "question_index": question_index,
                "question": question,
                "status": "running",
            }
            await store.persist_cell(run_id, cell)

            try:
                async with limiter:
                    row = await answer_cell(document, question)
                verified = verify_matrix_citations([row], document["pages"])[0]
                status = "completed"
                if verified.get("citation_status") == "not_applicable":
                    status = "not_disclosed"
                elif verified.get("citation_status") == "not_found":
                    status = "needs_review"

                cell.update(
                    {
                        "status": status,
                        "answer": verified.get("answer"),
                        "quote": verified.get("quote"),
                        "citation_status": verified.get("citation_status"),
                        "support_status": verified.get("support_status"),
                        "support_reason": verified.get("support_reason"),
                        "page": verified.get("page"),
                        "error": None,
                    }
                )
            except Exception as exc:
                cell.update({"status": "failed", "error": _redact_sensitive_text(exc, locals().get("api_key"))})

            await store.persist_cell(run_id, cell)
            return cell

        tasks = [
            run_cell(document_index, document, question_index, question)
            for document_index, document in enumerate(documents)
            for question_index, question in enumerate(questions)
        ]
        cells = await asyncio.gather(*tasks)
    return {"run_id": run_id, "status": "completed", "cells": cells}


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    page: int
    text: str
    source_text: str


@dataclass(frozen=True)
class RetrievalHit:
    chunk: Chunk
    score: float


@runtime_checkable
class RetrieverAdapter(Protocol):
    def retrieve(self, query: str, chunks: list[Chunk], limit: int = 3) -> list[RetrievalHit]:
        ...


@runtime_checkable
class ParserAdapter(Protocol):
    """PDF text extraction: a page list per PDF, honoring an optional page cap."""

    def __call__(self, pdf_path: str, max_pages: int = MAX_PDF_PAGES, ocr_backend=None) -> list[str]:
        ...


@runtime_checkable
class RerankerAdapter(Protocol):
    """Re-rank a retrieved hit list for a query, optionally capping the result."""

    def rerank(self, query: str, hits: list[RetrievalHit], limit: int | None = None) -> list[RetrievalHit]:
        ...


@runtime_checkable
class OcrPreprocessorAdapter(Protocol):
    """Optional OCR preprocessing with an explicit availability probe."""

    def is_available(self) -> bool:
        ...

    def preprocess(self, pdf_path: str, output_path: str | None = None) -> str | None:
        ...


@runtime_checkable
class TableExtractionAdapter(Protocol):
    """Extract normalized table rows (plain dicts) from a fixed-layout PDF."""

    def extract(self, pdf_path: str, pages: str = "1", flavor: str = "lattice") -> list[dict]:
        ...


@runtime_checkable
class ResultStoreAdapter(Protocol):
    """Async single-writer result store with a durable persist/load contract."""

    async def persist_cell(self, run_id: str, cell: dict) -> None:
        ...

    async def load_run(self, run_id: str) -> dict | None:
        ...

    async def close(self) -> None:
        ...


@runtime_checkable
class VectorStoreAdapter(Protocol):
    """Normalized vector storage adapter (e.g. Qdrant, in-memory, local)."""

    def is_available(self) -> bool:
        ...

    def upsert_chunks(self, collection_name: str, chunks: list[Chunk], vectors: list[list[float]], payload_meta: dict | None = None) -> int:
        ...

    def query_similar(self, collection_name: str, query_vector: list[float], limit: int = 3, min_score: float = 0.55) -> list[RetrievalHit]:
        ...


@runtime_checkable
class TaskQueueAdapter(Protocol):
    """Normalized async background task queue with durable status contract."""

    def is_available(self) -> bool:
        ...

    async def enqueue(self, task_type: str, payload: dict) -> str:
        ...

    async def status(self, task_id: str) -> str:
        ...

    async def cancel(self, task_id: str) -> bool:
        ...

    async def result(self, task_id: str) -> dict | None:
        ...


@dataclass(frozen=True)
class ProgressEvent:
    """Stable wire shape for a per-cell matrix progress event.

    The SSE endpoint emits exactly these fields so downstream consumers never
    depend on internal row objects.
    """

    type: str
    run_id: str
    cell_id: str
    document_index: int
    question_index: int
    question: str
    status: str


@dataclass(frozen=True)
class BoundedRunLimits:
    """Machine-readable bounded-run contract for autonomous runs.

    These are the invariants that gate a supervised run: the maximum number of
    pre-approved Issues, the maximum concurrent write tasks, the maximum
    substantive attempts per Issue, the maximum total wall-clock run, and the
    required terminal states. Encoding them here lets CI machine-check the
    checklist instead of relying on prose.
    """

    max_issues: int = 3
    max_concurrent_writes: int = 1
    max_attempts_per_issue: int = 2
    max_total_hours: int = 24
    terminal_states: frozenset[str] = frozenset({"PR_OPENED", "BLOCKED", "ABORTED"})

    def validate_plan(self, num_issues: int, attempts_per_issue: int = 1, max_attempts: int | None = None) -> list[str]:
        """Return a list of violated invariants for a candidate issue plan.

        An empty result means the plan is within the bounded-run contract.
        ``max_attempts`` defaults to ``max_attempts_per_issue`` so a single
        argument call still exercises the cap.
        """
        violations: list[str] = []
        if num_issues < 0:
            violations.append("num_issues must be non-negative")
        elif num_issues > self.max_issues:
            violations.append(f"num_issues={num_issues} exceeds max_issues={self.max_issues}")
        if max_attempts is None:
            max_attempts = self.max_attempts_per_issue
        if attempts_per_issue < 1:
            violations.append("attempts_per_issue must be >= 1")
        elif attempts_per_issue > max_attempts:
            violations.append(
                f"attempts_per_issue={attempts_per_issue} exceeds max_attempts_per_issue={max_attempts}"
            )
        return violations

    def is_valid_plan(self, num_issues: int, attempts_per_issue: int = 1) -> bool:
        return not self.validate_plan(num_issues, attempts_per_issue)

    def is_terminal_state(self, state: str) -> bool:
        return state in self.terminal_states


class IdentityReranker:
    def rerank(self, query: str, hits: list[RetrievalHit], limit: int | None = None) -> list[RetrievalHit]:
        return hits if limit is None else hits[:limit]

    def effective_backend(self) -> str:
        return "identity"


# Candidate pool retrieved before an optional reranker cuts it down to the final
# limit (top-30 -> top-K rerank per P3-2). The identity reranker preserves the
# original top-K, so default BM25 behavior is unchanged.
DEFAULT_RERANK_CANDIDATES = 30


def _rerank_candidate_pool(minimum: int = 1) -> int:
    """Resolve the rerank candidate-pool size from ``OPENMATRIX_RERANK_CANDIDATES``.

    A non-numeric value restores the default. A value below the requested
    ``minimum`` is raised to that minimum so the pool can never be smaller than
    the final top-K the retriever must satisfy (IdentityReranker preserves the
    original top-K only when the pool is at least that large).
    """
    raw = os.environ.get("OPENMATRIX_RERANK_CANDIDATES", str(DEFAULT_RERANK_CANDIDATES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_RERANK_CANDIDATES
    if value < 1:
        value = DEFAULT_RERANK_CANDIDATES
    return max(value, minimum)


_bge_reranker_models: dict[str, Any] = {}
_bge_reranker_lock = threading.Lock()


class BgeRerankerAdapter:
    """Optional local BGE Reranker v2-m3 (Apache-2.0) behind RerankerAdapter.

    Reranks a hit list with a cross-encoder over the ``BAAI/bge-reranker-v2-m3``
    model. Two local loaders are attempted in order:

    1. ``sentence_transformers.CrossEncoder`` (fast-tokenizer path, compatible
       with modern transformers releases).
    2. ``FlagEmbedding.FlagReranker`` (the BAAI canonical loader, used when
       sentence_transformers is unavailable).

    When no loader is importable, model init fails, or the rerank call errors,
    it degrades to identity ordering (original order preserved) so the retrieval
    pipeline is never interrupted. No model download happens at import time; the
    model is lazily cached behind a thread-safe lock.
    """

    MODEL_NAME = "BAAI/bge-reranker-v2-m3"

    def __init__(
        self,
        model_name: str | None = None,
        max_candidates: int | None = None,
        use_fp16: bool = False,
    ) -> None:
        self.model_name = model_name or os.environ.get(
            "OPENMATRIX_BGE_RERANKER_MODEL", self.MODEL_NAME
        )
        self.max_candidates = max_candidates
        self.use_fp16 = use_fp16
        self._last_backend: str | None = None
        self._last_status: str = "not_executed"
        self._last_error: str | None = None

    def _cross_encoder_available(self) -> bool:
        try:
            from sentence_transformers import CrossEncoder  # conditional dependency

            return True
        except Exception:
            return False

    def _flag_reranker_available(self) -> bool:
        try:
            from FlagEmbedding import FlagReranker  # conditional dependency

            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        return self._cross_encoder_available() or self._flag_reranker_available()

    def _get_model(self):
        global _bge_reranker_models
        with _bge_reranker_lock:
            for loader in ("cross-encoder", "flag-reranker"):
                key = (loader, self.model_name, bool(self.use_fp16))
                if key in _bge_reranker_models:
                    return _bge_reranker_models[key]
            model = None
            loader = None
            if self._cross_encoder_available():
                try:
                    from sentence_transformers import CrossEncoder

                    model = self._build_cross_encoder(CrossEncoder)
                    loader = "cross-encoder"
                except Exception:
                    model = None
            if model is None and self._flag_reranker_available():
                from FlagEmbedding import FlagReranker

                model = FlagReranker(self.model_name, use_fp16=self.use_fp16)
                loader = "flag-reranker"
            if model is None or loader is None:
                raise RuntimeError("no BGE reranker loader could initialize")
            key = (loader, self.model_name, bool(self.use_fp16))
            _bge_reranker_models[key] = model
            return _bge_reranker_models[key]

    def _build_cross_encoder(self, cross_encoder_cls):
        """Construct a CrossEncoder tolerating activation-parameter name drift.

        ``sentence_transformers.CrossEncoder`` has used ``activation_fn``,
        ``default_activation_function``, and ``default_activation_fn`` across
        releases. Probe the class signature and pass the supported name so a
        valid install is not misdetected as unavailable.
        """
        kwargs = {}
        try:
            import inspect

            params = inspect.signature(cross_encoder_cls.__init__).parameters
        except Exception:
            params = {}
        for name in ("activation_fn", "default_activation_function", "default_activation_fn"):
            if name in params:
                kwargs[name] = None
                break
        return cross_encoder_cls(self.model_name, **kwargs)

    def _score_pairs(self, model, pairs: list[list[str]]):
        """Return raw model scores without normalizing shape.

        A single-candidate pool may legitimately produce a bare float. The
        caller normalizes the raw value; converting here with ``list()`` would
        crash on a scalar and force a needless identity fallback.
        """
        try:
            return model.predict(pairs)
        except Exception:
            return model.compute_score(pairs)

    def effective_backend(self) -> str:
        """Which backend actually produced the last reranked output.

        ``bge-reranker-v2-m3`` when the model scored candidates, ``identity``
        when this call fell back to identity ordering (no hits, unavailable,
        model failure, or malformed scores), or ``"not-executed"`` before any
        ``rerank()`` call. Never inferred from importability or a global cache.
        """
        if self._last_backend is None:
            return "not-executed"
        return self._last_backend

    def rerank(self, query: str, hits: list[RetrievalHit], limit: int | None = None) -> list[RetrievalHit]:
        # Reset per-call outcome so effective_backend() never leaks a prior
        # call's state into the current one (including early-return paths).
        self._last_backend = "identity"
        self._last_status = "not_executed"
        self._last_error = None
        if not hits or not query.strip():
            return hits if limit is None else hits[:limit]
        if not self.is_available():
            self._last_status = "fallback_unavailable"
            return hits if limit is None else hits[:limit]
        try:
            model = self._get_model()
            pool = hits[: self.max_candidates] if self.max_candidates else hits
            pairs = [[query, hit.chunk.text] for hit in pool]
            scores = self._score_pairs(model, pairs)
            if isinstance(scores, (int, float)):
                scores = [scores]
            elif scores is None:
                self._last_status = "failed"
                self._last_error = "empty score output"
                return hits if limit is None else hits[:limit]
            else:
                try:
                    scores = list(scores)
                except TypeError:
                    self._last_status = "failed"
                    self._last_error = "non-iterable score output"
                    return hits if limit is None else hits[:limit]
            if len(scores) != len(pool):
                self._last_status = "failed"
                self._last_error = "score count mismatch"
                return hits if limit is None else hits[:limit]
            try:
                numeric = [float(score) for score in scores]
            except (TypeError, ValueError):
                self._last_status = "failed"
                self._last_error = "non-numeric scores"
                return hits if limit is None else hits[:limit]
            if not all(math.isfinite(score) for score in numeric):
                self._last_status = "failed"
                self._last_error = "non-finite scores"
                return hits if limit is None else hits[:limit]
            ordered = sorted(zip(pool, numeric), key=lambda pair: pair[1], reverse=True)
            reranked = [RetrievalHit(chunk=hit.chunk, score=score) for hit, score in ordered]
        except Exception as exc:
            self._last_status = "failed"
            self._last_error = _redact_sensitive_text(exc, locals().get("api_key"))
            return hits if limit is None else hits[:limit]
        self._last_backend = "bge-reranker-v2-m3"
        self._last_status = "success"
        return reranked if limit is None else reranked[:limit]


def get_reranker_adapter() -> IdentityReranker | BgeRerankerAdapter:
    """Return the configured RerankerAdapter.

    Prefers the local BGE Reranker v2-m3 when a cross-encoder loader is
    importable; otherwise falls back to ``IdentityReranker`` so no retrieval
    path changes behavior when the optional dependency is absent.
    """
    reranker = BgeRerankerAdapter()
    return reranker if reranker.is_available() else IdentityReranker()


def tokenize_for_retrieval(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def _retrieval_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def build_page_chunks(document_pages: list[str], max_chars: int | None = None,
                      overlap: int | None = None) -> list[Chunk]:
    size = max_chars if max_chars is not None else _retrieval_int("OPENMATRIX_CHUNK_CHARS", 1800)
    if size < 1:
        raise ValueError("max_chars must be positive")
    overlap = _retrieval_int("OPENMATRIX_CHUNK_OVERLAP", 200, 0) if overlap is None else overlap
    step = size - min(max(0, overlap), size // 2)
    chunks = []
    for page_number, page_text in enumerate(document_pages, start=1):
        clean_text = " ".join((page_text or "").split())
        for index, offset in enumerate(range(0, len(clean_text), step)):
            chunks.append(Chunk(chunk_id=f"p{page_number}:c{index}", page=page_number,
                                text=clean_text[offset:offset + size], source_text=page_text))
            if offset + size >= len(clean_text):
                break
    return chunks


def _financial_query(query: str) -> str:
    rules = {
        "revenue": "total revenue revenues net sales consolidated statements of income operations",
        "net income": "net income attributable",
        "same-store": "same-Shack comparable sales",
        "same-shack": "same-Shack comparable sales",
        "headcount": "employees workforce",
    }
    return query + " " + " ".join(value for key, value in rules.items() if key in query.lower())


def _context_from_hits(hits) -> str:
    # Keep the highest scores within budget, then restore physical page order.
    budget = _retrieval_int("OPENMATRIX_CONTEXT_MAX_CHARS", 48000)
    chosen = []
    used = 0
    for hit in sorted(hits, key=lambda h: (-h.score, h.chunk.page, h.chunk.chunk_id)):
        text = f"[Page {hit.chunk.page} | {hit.chunk.chunk_id}]\n{hit.chunk.text}"
        cost = len(text) + (2 if chosen else 0)
        if used + cost <= budget:
            chosen.append((hit, text))
            used += cost
    if not chosen and hits:
        best = max(hits, key=lambda h: h.score)
        return f"[Page {best.chunk.page} | {best.chunk.chunk_id}]\n{best.chunk.text}"[:budget]
    return "\n\n".join(text for hit, text in sorted(chosen, key=lambda item: (item[0].chunk.page, item[0].chunk.chunk_id)))


def select_batch_context(questions, pages):
    mode = os.environ.get("OPENMATRIX_BATCH_PER_QUESTION", "auto").lower()
    if mode == "true" or (mode == "auto" and len(pages) > 50):
        budget = _retrieval_int("OPENMATRIX_CONTEXT_MAX_CHARS", 48000)
        share = max(1, budget // max(1, len(questions)))
        sections, fallback = [], False
        for index, question in enumerate(questions, 1):
            context, used_fallback = select_retrieved_context([question], pages)
            header = f"Context for Q{index}: {question}\n"
            sections.append((header + context)[:max(1, share - 2)])
            fallback |= used_fallback
        return "\n\n".join(sections)[:budget], fallback
    return select_retrieved_context(questions, pages)


class SimpleSparseRetriever:
    def retrieve(self, query: str, chunks: list[Chunk], limit: int = 3) -> list[RetrievalHit]:
        query_terms = tokenize_for_retrieval(query)
        if not query_terms:
            return []

        hits = []
        for chunk in chunks:
            chunk_terms = tokenize_for_retrieval(chunk.text)
            if not chunk_terms:
                continue
            score = sum(chunk_terms.count(term) for term in query_terms)
            if score > 0:
                hits.append(RetrievalHit(chunk=chunk, score=float(score)))

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.page, hit.chunk.chunk_id))
        return hits[:limit]


class Bm25SRetriever:
    """Official BM25 retriever backed by the MIT-licensed `bm25s` component.

    bm25s returns result indices referencing the tokenized corpus order, which
    matches the input ``chunks`` order because each retrieval indexes only the
    chunk set passed for that call.
    """

    def retrieve(self, query: str, chunks: list[Chunk], limit: int = 3) -> list[RetrievalHit]:
        if not chunks:
            return []
        texts = [chunk.text for chunk in chunks]
        try:
            corpus_tokens = bm25s.tokenize(texts, show_progress=False)
            retriever = bm25s.BM25()
            retriever.index(corpus_tokens, show_progress=False)
            query_tokens = bm25s.tokenize([query], show_progress=False)
            k = min(max(1, limit), len(chunks))
            results, scores = retriever.retrieve(query_tokens, k=k, show_progress=False)
        except Exception:
            return []

        hits = []
        for result_index, score in zip(results[0].flatten().tolist(), scores[0].flatten().tolist()):
            if result_index < 0 or result_index >= len(chunks):
                continue
            if float(score) <= 0:
                continue
            hits.append(RetrievalHit(chunk=chunks[result_index], score=float(score)))
        return hits


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


import anyio

_dense_models: dict[Any, Any] = {}
_dense_model_lock = threading.Lock()
_corpus_embedding_cache: dict[tuple, list] = {}
_corpus_cache_lock = threading.Lock()


class _BgeM3BackendError(RuntimeError):
    """BGE-M3 backend failed at runtime (init, inference, or malformed output)."""


def _chunk_text_digest(chunks: list[Chunk]) -> str:
    """Stable content fingerprint of a chunk list.

    Chunk ids (``p1:c0`` etc.) restart for every document, so a cache key made
    only of ids + text lengths can collide across distinct documents. Include a
    content digest so two documents never share a corpus-embedding cache entry
    unless their chunk texts are byte-identical. Each chunk is length-prefixed
    so chunk boundaries are unambiguous (a bare separator byte would let
    ``["a\\x00b", "c"]`` and ``["a", "b\\x00c"]`` hash identically).
    """
    hasher = hashlib.sha256()
    for chunk in chunks:
        # surrogatepass keeps unpaired surrogates distinct (utf-8 with
        # errors="replace" would map "\ud800" and "?" to the same bytes).
        data = chunk.text.encode("utf-8", errors="surrogatepass")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


class DenseRetrieverAdapter:
    """Optional dense retriever behind RetrieverAdapter.

    Two local backends are supported and selected in this order when
    ``backend="auto"``:

    1. **BGE-M3** (``FlagEmbedding.BGEM3FlagModel``, MIT) - hybrid dense + sparse
       scoring. Query and chunks are embedded with dense vectors and lexical
       (sparse) weights; the final score blends cosine similarity with a
       normalized sparse-overlap term.
    2. **FastEmbed** (``fastembed.TextEmbedding``, Apache-2.0, local ONNX path)
       - the existing dense cosine path, kept as the graceful fallback.

    Both libraries are optional imports; when neither is importable (or model
    init/inference fails) retrieval returns an empty list so the sparse path
    continues uninterrupted. No vector database is involved and no model
    download happens at import time. Models are cached per backend/model name
    behind a thread-safe lock. Corpus chunk embeddings are cached per document
    chunk signature to avoid re-embedding on every question.
    """

    MIN_SCORE = 0.55
    BGE_M3_MODEL_NAME = "BAAI/bge-m3"
    BGE_M3_ALPHA = 0.7
    BGE_M3_MIN_SCORE = 0.30

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        min_score: float | None = None,
        backend: str = "auto",
        bge_m3_model: str | None = None,
        bge_m3_alpha: float = BGE_M3_ALPHA,
        bge_m3_min_score: float | None = None,
    ) -> None:
        self.model_name = model_name
        self.min_score = min_score if min_score is not None else self.MIN_SCORE
        self.backend = backend
        self.bge_m3_model = bge_m3_model or os.environ.get(
            "OPENMATRIX_BGE_M3_MODEL", self.BGE_M3_MODEL_NAME
        )
        self.bge_m3_alpha = bge_m3_alpha
        self.bge_m3_min_score = bge_m3_min_score if bge_m3_min_score is not None else self.BGE_M3_MIN_SCORE
        self._last_backend: str | None = None
        self._last_status: str = "not_executed"
        self._last_error: str | None = None

    def _fastembed_available(self) -> bool:
        try:
            from fastembed import TextEmbedding  # conditional dependency

            return True
        except Exception:
            return False

    def _bge_m3_available(self) -> bool:
        try:
            from FlagEmbedding import BGEM3FlagModel  # conditional dependency

            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        if self.backend == "fastembed":
            return self._fastembed_available()
        if self.backend == "bge-m3":
            return self._bge_m3_available()
        return self._bge_m3_available() or self._fastembed_available()

    def effective_backend(self) -> str | None:
        """Which backend ``retrieve()`` actually used, or None if not executed.

        Reflects the last completed retrieval only: ``bge-m3``, ``fastembed``,
        or ``None`` when no backend produced output (unavailable, backend
        failure with empty fallback, or empty/no-match result). It never infers
        a backend from module importability or a prior call's state.
        """
        return self._last_backend

    def _get_model(self):
        global _dense_models
        with _dense_model_lock:
            if self.model_name not in _dense_models:
                from fastembed import TextEmbedding

                _dense_models[self.model_name] = TextEmbedding(model_name=self.model_name)
            return _dense_models[self.model_name]

    def _get_bge_m3_model(self):
        global _dense_models
        with _dense_model_lock:
            key = ("bge-m3", self.bge_m3_model)
            if key not in _dense_models:
                from FlagEmbedding import BGEM3FlagModel

                _dense_models[key] = BGEM3FlagModel(self.bge_m3_model, use_fp16=False)
            return _dense_models[key]

    def _get_chunk_vectors(self, embedder, chunks: list[Chunk]) -> list:
        cache_key = (self.model_name, _chunk_text_digest(chunks))
        with _corpus_cache_lock:
            if cache_key in _corpus_embedding_cache:
                return _corpus_embedding_cache[cache_key]
        vectors = list(embedder.embed([chunk.text for chunk in chunks]))
        with _corpus_cache_lock:
            if len(_corpus_embedding_cache) > 64:
                _corpus_embedding_cache.clear()
            _corpus_embedding_cache[cache_key] = vectors
        return vectors

    def _get_bge_m3_chunk_outputs(self, model, chunks: list[Chunk]):
        cache_key = ("bge-m3", self.bge_m3_model, _chunk_text_digest(chunks))
        with _corpus_cache_lock:
            if cache_key in _corpus_embedding_cache:
                return _corpus_embedding_cache[cache_key]
        output = model.encode([chunk.text for chunk in chunks], return_dense=True, return_sparse=True)
        payload = (list(output.get("dense_vecs", [])), list(output.get("lexical_weights", [])))
        # Structural sanity check BEFORE caching: a transient malformed output
        # must never poison the cache so a later healthy call reuses bad vectors.
        if len(payload[0]) != len(chunks) or len(payload[1]) != len(chunks):
            raise _BgeM3BackendError("malformed dense/sparse output shape")
        with _corpus_cache_lock:
            if len(_corpus_embedding_cache) > 64:
                _corpus_embedding_cache.clear()
            _corpus_embedding_cache[cache_key] = payload
        return payload

    def _drop_bge_m3_cache_entry(self, chunks: list[Chunk]) -> None:
        """Remove the corpus-cache entry for these chunks (malformed output)."""
        cache_key = ("bge-m3", self.bge_m3_model, _chunk_text_digest(chunks))
        with _corpus_cache_lock:
            _corpus_embedding_cache.pop(cache_key, None)

    def _sparse_overlap_score(self, query_weights: dict, doc_weights: dict) -> float:
        if not query_weights or not doc_weights:
            return 0.0
        score = 0.0
        for term, q_weight in query_weights.items():
            d_weight = doc_weights.get(term)
            if d_weight is not None:
                score += q_weight * d_weight
        return score

    def _retrieve_bge_m3(self, query: str, chunks: list[Chunk], limit: int) -> list[RetrievalHit]:
        try:
            model = self._get_bge_m3_model()
            query_output = model.encode([query], return_dense=True, return_sparse=True)
            query_dense_vecs = query_output.get("dense_vecs")
            query_sparse_weights = query_output.get("lexical_weights")
            query_dense = list(query_dense_vecs[0]) if query_dense_vecs is not None else []
            query_sparse = query_sparse_weights[0] if query_sparse_weights is not None else {}
            chunk_dense, chunk_sparse = self._get_bge_m3_chunk_outputs(model, chunks)
        except _BgeM3BackendError:
            raise
        except Exception as exc:
            raise _BgeM3BackendError(_redact_sensitive_text(exc, locals().get("api_key"))) from exc

        # Validate and normalize all model output INSIDE the failure boundary.
        # Every shape/type/value violation becomes _BgeM3BackendError so
        # backend="auto" degrades to FastEmbed instead of letting an ordinary
        # TypeError/ValueError escape retrieve().
        try:
            query_dense = self._normalize_dense_vector(query_dense, "query")
            if not isinstance(query_sparse, dict):
                raise _BgeM3BackendError("malformed query sparse output")
            query_sparse = self._normalize_sparse_weights(query_sparse, "query")
            if len(chunk_dense) != len(chunks) or len(chunk_sparse) != len(chunks):
                raise _BgeM3BackendError("malformed dense/sparse output shape")

            chunk_dense_norm = []
            for vector in chunk_dense:
                chunk_dense_norm.append(self._normalize_dense_vector(vector, "document"))
            if any(len(vector) != len(query_dense) for vector in chunk_dense_norm):
                raise _BgeM3BackendError("malformed dense vector dimension")
            chunk_sparse_norm = []
            for weights in chunk_sparse:
                chunk_sparse_norm.append(self._normalize_sparse_weights(weights, "document"))
        except _BgeM3BackendError:
            # Never keep a malformed corpus entry cached; a later healthy call
            # must be able to re-encode this corpus.
            self._drop_bge_m3_cache_entry(chunks)
            raise

        # Sparse scores use the BGE-M3 canonical lexical matching score (a
        # normalized weighted-overlap). They are normalized by the query's own
        # self-match so a term appearing literally in one chunk cannot dominate
        # the hybrid blend the way a raw max-normalization would. The whole
        # scoring phase stays inside the backend-failure boundary so any
        # malformed chunk output degrades instead of escaping the caller.
        try:
            self_match = float(model.compute_lexical_matching_score(query_sparse, query_sparse)) if query_sparse else 0.0
        except Exception:
            self_match = self._sparse_overlap_score(query_sparse, query_sparse)
        if not math.isfinite(self_match):
            raise _BgeM3BackendError("non-finite sparse self-match")

        sparse_scores = []
        for doc_weights in chunk_sparse_norm:
            if self_match > 0 and doc_weights:
                try:
                    raw = float(model.compute_lexical_matching_score(query_sparse, doc_weights))
                except Exception:
                    raw = self._sparse_overlap_score(query_sparse, doc_weights)
                if not math.isfinite(raw):
                    raise _BgeM3BackendError("non-finite sparse score")
                sparse_scores.append(min(1.0, raw / self_match))
            else:
                sparse_scores.append(0.0)

        scores = []
        for index, vector in enumerate(chunk_dense_norm):
            dense_cos = max(0.0, _cosine_similarity(query_dense, vector))
            hybrid = self.bge_m3_alpha * dense_cos + (1.0 - self.bge_m3_alpha) * sparse_scores[index]
            if not math.isfinite(hybrid):
                hybrid = 0.0
            scores.append((index, hybrid))
        scores.sort(key=lambda item: (-item[1], item[0]))
        ranked = [hit for hit in scores if hit[1] >= self.bge_m3_min_score][:limit]
        return [RetrievalHit(chunk=chunks[index], score=score) for index, score in ranked]

    def _normalize_dense_vector(self, vector, source: str) -> list[float]:
        """Convert a model dense vector to a finite float list, or fail."""
        try:
            values = [float(value) for value in vector]
        except Exception as exc:
            raise _BgeM3BackendError("malformed {} dense vector".format(source)) from exc
        if not values:
            raise _BgeM3BackendError("empty {} dense vector".format(source))
        if not all(math.isfinite(value) for value in values):
            raise _BgeM3BackendError("non-finite {} dense vector".format(source))
        return values

    def _normalize_sparse_weights(self, weights, source: str) -> dict[str, float]:
        """Convert model lexical weights to a finite float mapping, or fail."""
        if weights is None:
            raise _BgeM3BackendError("malformed {} sparse output".format(source))
        if not isinstance(weights, dict):
            raise _BgeM3BackendError("malformed {} sparse output".format(source))
        normalized = {}
        for term, weight in weights.items():
            try:
                value = float(weight)
            except (TypeError, ValueError) as exc:
                raise _BgeM3BackendError("malformed {} sparse weight".format(source)) from exc
            if not math.isfinite(value):
                raise _BgeM3BackendError("non-finite {} sparse weight".format(source))
            normalized[term] = value
        return normalized

    def _drop_fastembed_cache_entry(self, chunks: list[Chunk]) -> None:
        """Remove the corpus-cache entry for these chunks (malformed output)."""
        cache_key = (self.model_name, _chunk_text_digest(chunks))
        with _corpus_cache_lock:
            _corpus_embedding_cache.pop(cache_key, None)

    def _retrieve_fastembed(self, query: str, chunks: list[Chunk], limit: int) -> list[RetrievalHit]:
        try:
            embedder = self._get_model()
        except Exception as exc:
            raise _BgeM3BackendError("fastembed model init failed: {}".format(exc)) from exc

        try:
            query_vector = next(iter(embedder.query_embed(query)), None)
            chunk_vectors = self._get_chunk_vectors(embedder, chunks)
        except Exception as exc:
            raise _BgeM3BackendError("fastembed inference failed: {}".format(exc)) from exc

        try:
            if query_vector is None:
                raise _BgeM3BackendError("empty query vector")
            try:
                query_list = list(query_vector)
            except Exception as exc:
                raise _BgeM3BackendError("malformed query vector") from exc
            query_vector_norm = self._normalize_dense_vector(query_list, "query")

            if len(chunk_vectors) != len(chunks):
                raise _BgeM3BackendError("malformed dense output shape")

            chunk_vectors_norm = []
            for vector in chunk_vectors:
                try:
                    vec_list = list(vector)
                except Exception as exc:
                    raise _BgeM3BackendError("malformed document vector") from exc
                chunk_vectors_norm.append(self._normalize_dense_vector(vec_list, "document"))

            if any(len(v) != len(query_vector_norm) for v in chunk_vectors_norm):
                raise _BgeM3BackendError("malformed dense vector dimension")
        except _BgeM3BackendError:
            self._drop_fastembed_cache_entry(chunks)
            raise

        scores = []
        for index, vector in enumerate(chunk_vectors_norm):
            scores.append((index, _cosine_similarity(query_vector_norm, vector)))
        scores.sort(key=lambda item: (-item[1], item[0]))
        ranked = [hit for hit in scores if hit[1] >= self.min_score][:limit]
        return [RetrievalHit(chunk=chunks[index], score=score) for index, score in ranked]

    def _retrieve_fastembed_guarded(self, query: str, chunks: list[Chunk], limit: int) -> list[RetrievalHit]:
        try:
            return self._retrieve_fastembed(query, chunks, limit)
        except _BgeM3BackendError:
            return []

    def retrieve(self, query: str, chunks: list[Chunk], limit: int = 3) -> list[RetrievalHit]:
        # Reset per-call outcome so effective_backend() never leaks a prior
        # call's backend into the current one (including early-return paths).
        self._last_backend = None
        self._last_status = "not_executed"
        self._last_error = None
        if not chunks or not query.strip() or not self.is_available():
            return []

        if self.backend == "fastembed":
            try:
                hits = self._retrieve_fastembed(query, chunks, limit)
                self._last_backend = "fastembed"
                self._last_status = "success" if hits else "no_hits"
                return hits
            except _BgeM3BackendError as exc:
                self._last_backend = None
                self._last_status = "failed"
                self._last_error = _redact_sensitive_text(exc, locals().get("api_key"))
                return []
        if self.backend == "bge-m3":
            try:
                hits = self._retrieve_bge_m3(query, chunks, limit)
                self._last_backend = "bge-m3"
                self._last_status = "success" if hits else "no_hits"
                return hits
            except _BgeM3BackendError as exc:
                self._last_backend = None
                self._last_status = "failed"
                self._last_error = _redact_sensitive_text(exc, locals().get("api_key"))
                return []

        # backend == "auto": prefer BGE-M3, fall back to FastEmbed when the
        # BGE-M3 backend fails at runtime (init, inference, or malformed output).
        if self._bge_m3_available():
            try:
                hits = self._retrieve_bge_m3(query, chunks, limit)
                self._last_backend = "bge-m3"
                self._last_status = "success" if hits else "no_hits"
                return hits
            except _BgeM3BackendError as exc:
                self._last_error = _redact_sensitive_text(exc, locals().get("api_key"))
        try:
            hits = self._retrieve_fastembed(query, chunks, limit)
        except _BgeM3BackendError as exc:
            self._last_backend = None
            self._last_status = "failed"
            self._last_error = _redact_sensitive_text(exc, locals().get("api_key"))
            return []
        self._last_backend = "fastembed"
        self._last_status = "success" if hits else "no_hits"
        return hits


class InMemoryVectorStore:
    """Fallback in-memory vector storage adhering to VectorStoreAdapter."""

    def __init__(self) -> None:
        self._collections: dict[str, list[dict]] = {}

    def is_available(self) -> bool:
        return True

    def upsert_chunks(self, collection_name: str, chunks: list[Chunk], vectors: list[list[float]], payload_meta: dict | None = None) -> int:
        if collection_name not in self._collections:
            self._collections[collection_name] = []
        meta = payload_meta or {}
        for chunk, vector in zip(chunks, vectors):
            self._collections[collection_name].append({
                "chunk": chunk,
                "vector": list(vector),
                "meta": meta,
            })
        return len(chunks)

    def query_similar(self, collection_name: str, query_vector: list[float], limit: int = 3, min_score: float = 0.55) -> list[RetrievalHit]:
        points = self._collections.get(collection_name, [])
        if not points or not query_vector:
            return []
        scores = []
        for p in points:
            sim = _cosine_similarity(query_vector, p["vector"])
            if sim >= min_score:
                scores.append((p["chunk"], sim))
        scores.sort(key=lambda item: -item[1])
        return [RetrievalHit(chunk=c, score=s) for c, s in scores[:limit]]


class QdrantVectorStoreAdapter:
    """Optional Qdrant vector store adapter (Apache-2.0).

    Supports in-memory (``:memory:``), local path, or remote URL (e.g. ``http://localhost:6333``).
    Falls back gracefully to InMemoryVectorStore when qdrant-client is absent.
    """

    def __init__(self, location: str = ":memory:", vector_size: int = 384) -> None:
        self.location = location
        self.vector_size = vector_size
        self._client = None
        self._fallback = InMemoryVectorStore()

    def is_available(self) -> bool:
        try:
            import qdrant_client
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is None and self.is_available():
            from qdrant_client import QdrantClient
            if self.location == ":memory:":
                self._client = QdrantClient(location=":memory:")
            elif self.location.startswith("http://") or self.location.startswith("https://"):
                self._client = QdrantClient(url=self.location)
            else:
                self._client = QdrantClient(path=self.location)
        return self._client

    def upsert_chunks(self, collection_name: str, chunks: list[Chunk], vectors: list[list[float]], payload_meta: dict | None = None) -> int:
        if not self.is_available():
            return self._fallback.upsert_chunks(collection_name, chunks, vectors, payload_meta)
        try:
            from qdrant_client import models
            client = self._get_client()
            if not client.collection_exists(collection_name):
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(size=len(vectors[0]) if vectors else self.vector_size, distance=models.Distance.COSINE),
                )
            points = []
            meta = payload_meta or {}
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
                # Qdrant requires uint or uuid id
                point_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection_name}_{chunk.chunk_id}").hex
                points.append(
                    models.PointStruct(
                        id=point_id,
                        vector=list(vector),
                        payload={
                            "chunk_id": chunk.chunk_id,
                            "page": chunk.page,
                            "text": chunk.text,
                            "source_text": chunk.source_text,
                            **meta,
                        },
                    )
                )
            client.upsert(collection_name=collection_name, points=points)
            return len(points)
        except Exception:
            return self._fallback.upsert_chunks(collection_name, chunks, vectors, payload_meta)

    def query_similar(self, collection_name: str, query_vector: list[float], limit: int = 3, min_score: float = 0.55) -> list[RetrievalHit]:
        if not self.is_available():
            return self._fallback.query_similar(collection_name, query_vector, limit, min_score)
        try:
            client = self._get_client()
            if not client.collection_exists(collection_name):
                return []
            res = client.query_points(
                collection_name=collection_name,
                query=list(query_vector),
                limit=limit,
                score_threshold=min_score,
            ).points
            hits = []
            for pt in res:
                p = pt.payload or {}
                chunk = Chunk(
                    chunk_id=str(p.get("chunk_id", "")),
                    page=int(p.get("page", 1)),
                    text=str(p.get("text", "")),
                    source_text=str(p.get("source_text", "")),
                )
                hits.append(RetrievalHit(chunk=chunk, score=float(pt.score)))
            return hits
        except Exception:
            return self._fallback.query_similar(collection_name, query_vector, limit, min_score)


def get_vector_store_adapter(location: str | None = None) -> VectorStoreAdapter:
    """Return configured VectorStoreAdapter (defaults to memory Qdrant/InMemory)."""
    loc = location or os.environ.get("OPENMATRIX_VECTOR_STORE", ":memory:")
    return QdrantVectorStoreAdapter(location=loc)


class InProcessTaskQueue:
    """Fallback in-process async task queue adhering to TaskQueueAdapter.

    Uses an in-memory dict for task status and result storage. Never requires a
    broker; this is the zero-dependency default and is not durable across restarts.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def is_available(self) -> bool:
        return True

    async def enqueue(self, task_type: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex
        async with self._lock:
            self._tasks[task_id] = {"task_type": task_type, "payload": payload, "status": "queued"}
        return task_id

    async def status(self, task_id: str) -> str:
        async with self._lock:
            task = self._tasks.get(task_id)
        return task["status"] if task else "unknown"

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] in ("completed", "failed", "cancelled"):
                return False
            task["status"] = "cancelled"
            return True

    async def result(self, task_id: str) -> dict | None:
        async with self._lock:
            task = self._tasks.get(task_id)
        return task.get("payload") if task else None

    async def mark(self, task_id: str, status: str, payload: dict | None = None) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task["status"] = status
                if payload is not None:
                    task["payload"] = payload


def get_task_queue_adapter() -> TaskQueueAdapter:
    """Return configured TaskQueueAdapter (defaults to in-process)."""
    return InProcessTaskQueue()


def precision_at_k(hits: list[RetrievalHit], relevant_ids: set[str], k: int | None = None) -> float:
    """Fraction of the top-k retrieved chunks that are relevant (precision@k)."""
    if k is not None:
        hits = hits[:k]
    if not hits:
        return 0.0
    relevant = sum(1 for hit in hits if hit.chunk.chunk_id in relevant_ids)
    return relevant / len(hits)


def reciprocal_rank(hits: list[RetrievalHit], relevant_ids: set[str]) -> float:
    """Inverse of the rank of the first relevant hit (1 for first, 0 if none)."""
    for rank, hit in enumerate(hits, start=1):
        if hit.chunk.chunk_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(all_hits: list[list[RetrievalHit]], relevant: list[set[str]]) -> float:
    """Average reciprocal rank across queries (MRR)."""
    if not all_hits:
        return 0.0
    total = 0.0
    for hits, relevant_ids in zip(all_hits, relevant):
        total += reciprocal_rank(hits, relevant_ids)
    return total / len(all_hits)


def recall_at_k(hits: list[RetrievalHit], relevant_ids: set[str], k: int | None = None) -> float:
    """Fraction of relevant chunks retrieved within the top-k.

    Returns 0.0 when there are no relevant chunks (undefined recall treated as
    a hard miss so distractor queries score consistently).
    """
    if not relevant_ids:
        return 0.0
    if k is not None:
        hits = hits[:k]
    retrieved = {hit.chunk.chunk_id for hit in hits}
    return len(retrieved & relevant_ids) / len(relevant_ids)


def ndcg_at_k(hits: list[RetrievalHit], relevant_ids: set[str], k: int | None = None) -> float:
    """Normalized discounted cumulative gain at the top-k rank cutoff.

    Relies on the standard ``log2(i + 2)`` discount and a binary gain of 1 for
    relevant chunks, so scores stay in [0, 1] and rank ordering matters.
    """
    if k is not None:
        hits = hits[:k]
    if not hits or not relevant_ids:
        return 0.0

    def dcg(ranked: list[RetrievalHit]) -> float:
        total = 0.0
        for index, hit in enumerate(ranked, start=1):
            gain = 1.0 if hit.chunk.chunk_id in relevant_ids else 0.0
            total += gain / math.log2(index + 1)
        return total

    ideal = dcg(sorted(hits, key=lambda hit: hit.chunk.chunk_id in relevant_ids, reverse=True))
    if ideal == 0.0:
        return 0.0
    return dcg(hits) / ideal


DEFAULT_GOLD_CORPUS: list[dict] = [
    {
        "page": 1,
        "text": "Quarterly revenue reached 1.2 billion with gross margin of 48%. "
        "Operating income grew 14% year over year.",
    },
    {
        "page": 2,
        "text": "The company guided next quarter revenue between 1.25 and 1.3 billion. "
        "Cloud segment grew 31% and remains the largest business unit.",
    },
    {
        "page": 3,
        "text": "Research and development expense increased to 180 million, focused on "
        "generative AI and the new database engine.",
    },
    {
        "page": 4,
        "text": "Primary competitors include the legacy database vendors. The company "
        "believes its vector support is a durable competitive advantage.",
    },
    {
        "page": 5,
        "text": "Forward-looking risks include tariffs, supply chain disruption, and "
        "server parts availability in the current fiscal year.",
    },
    {
        "page": 6,
        "text": "Free cash flow reached 340 million while capital expenditures "
        "increased due to data center expansion.",
    },
    {
        "page": 7,
        "text": "Non-GAAP adjusted earnings per share came in at 0.94, above the "
        "consensus estimate, while diluted share count edged higher. "
        "Foreign exchange headwinds trimmed reported growth.",
    },
    {
        "page": 8,
        "text": "The database market opportunity exceeds 60 billion. 市场机遇 (market "
        "opportunity) remains large and the company targets enterprise "
        "adoption across banking, retail, and manufacturing verticals.",
    },
    {
        "page": 9,
        "text": "Segment revenue table (in millions): Cloud 1800, Software 950, "
        "Hardware 320, Services 610. Cloud grew 31% while Hardware declined 4%.",
    },
]


DEFAULT_GOLD_QUERIES: list[dict] = [
    {"query": "quarterly gross margin revenue", "relevant_pages": {1}},
    {"query": "next quarter revenue guidance", "relevant_pages": {2}},
    {"query": "research and development spending", "relevant_pages": {3}},
    {"query": "main competitors competitive advantage", "relevant_pages": {4}},
    {"query": "tariff supply chain risks", "relevant_pages": {5}},
    {"query": "free cash flow capital expenditure", "relevant_pages": {6}},
    {"query": "adjusted earnings per share consensus", "relevant_pages": {7}},
    {"query": "每股收益 adjusted earnings per share", "relevant_pages": {7}},
    {"query": "数据库市场 database market opportunity", "relevant_pages": {8}},
    {"query": "future business headwinds and trade barrier concerns", "relevant_pages": {5}},
    {"query": "segment revenue by hardware", "relevant_pages": {9}},
    {"query": "segment revenue by cloud", "relevant_pages": {2, 9}},
    {"query": "non relevant keyword entirely absent", "relevant_pages": set()},
]


def relevant_chunk_ids(chunks: list[Chunk], relevant_pages: set[int]) -> set[str]:
    """Chunk ids on any page considered relevant for a query."""
    return {chunk.chunk_id for chunk in chunks if chunk.page in relevant_pages}


def evaluate_retrievers(
    corpus: list[dict] | None = None,
    queries: list[dict] | None = None,
    limit: int = 3,
    include_dense: bool = True,
    include_rerank: bool = True,
) -> dict:
    """Deterministic precision@k, recall@k, nDCG@k, and MRR over the gold corpus.

    Both bm25s and the simple sparse retriever are scored on the same chunks,
    so results are directly comparable and stable across runs. When FastEmbed is
    importable (and ``include_dense`` is true), a ``dense`` retriever and a
    ``hybrid`` (dense + sparse via RRF) retriever are added so the semantic gain
    can be measured. When a real reranker is available (and ``include_rerank``
    is true), a ``bm25_reranked`` row applies the reranker to a BM25 candidate
    pool. The default retriever is never changed.
    """
    corpus = corpus if corpus is not None else DEFAULT_GOLD_CORPUS
    queries = queries if queries is not None else DEFAULT_GOLD_QUERIES
    chunks = build_page_chunks([entry["text"] for entry in corpus])
    retrievers = {"bm25": Bm25SRetriever(), "simple_sparse": SimpleSparseRetriever()}

    reranker = get_reranker_adapter() if include_rerank else IdentityReranker()
    rerank_available = not isinstance(reranker, IdentityReranker)

    dense = DenseRetrieverAdapter()
    dense_available = include_dense and dense.is_available()
    if dense_available:
        retrievers["dense"] = dense
        retrievers["hybrid"] = None  # placeholder for RRF-fused path below
    if rerank_available:
        retrievers["bm25_reranked"] = None  # placeholder for rerank path below

    results: dict[str, dict] = {}
    dense_outcomes: list[tuple[str | None, str]] = []
    rerank_outcomes: list[tuple[str | None, str]] = []
    dense_hits_by_query: list[list[RetrievalHit]] = []
    if dense_available:
        for entry in queries:
            hits = dense.retrieve(entry["query"], chunks, limit=limit)
            dense_hits_by_query.append(hits)
            if dense._last_status != "not_executed":
                dense_outcomes.append((dense.effective_backend(), dense._last_status))

    for name, retriever in retrievers.items():
        all_hits: list[list[RetrievalHit]] = []
        relevant: list[set[str]] = []
        for query_index, entry in enumerate(queries):
            if name == "hybrid":
                sparse_hits = Bm25SRetriever().retrieve(entry["query"], chunks, limit=limit)
                dense_hits = dense_hits_by_query[query_index]
                hits = ReciprocalRankFusion().fuse(sparse_hits, dense_hits)[:limit]
            elif name == "bm25_reranked":
                pool = min(_rerank_candidate_pool(minimum=limit), len(chunks))
                candidate_hits = Bm25SRetriever().retrieve(entry["query"], chunks, limit=pool)
                hits = reranker.rerank(entry["query"], candidate_hits, limit)
                if reranker._last_status != "not_executed":
                    rerank_outcomes.append((reranker.effective_backend(), reranker._last_status))
            else:
                if name == "dense":
                    hits = dense_hits_by_query[query_index]
                else:
                    hits = retriever.retrieve(entry["query"], chunks, limit=limit)
            all_hits.append(hits)
            relevant.append(relevant_chunk_ids(chunks, entry.get("relevant_pages", set())))
        results[name] = {
            "precision@{}".format(limit): [
                precision_at_k(hits, rel, k=limit) for hits, rel in zip(all_hits, relevant)
            ],
            "recall@{}".format(limit): [
                recall_at_k(hits, rel, k=limit) for hits, rel in zip(all_hits, relevant)
            ],
            "ndcg@{}".format(limit): [
                ndcg_at_k(hits, rel, k=limit) for hits, rel in zip(all_hits, relevant)
            ],
        }
        results[name]["avg_precision@{}".format(limit)] = _average(results[name]["precision@{}".format(limit)])
        results[name]["avg_recall@{}".format(limit)] = _average(results[name]["recall@{}".format(limit)])
        results[name]["avg_ndcg@{}".format(limit)] = _average(results[name]["ndcg@{}".format(limit)])
        results[name]["mrr"] = mean_reciprocal_rank(all_hits, relevant)
    return {
        "results": results,
        "num_queries": len(queries),
        "num_chunks": len(chunks),
        "backends": {
            "dense_backend": _aggregate_backend_outcomes(dense_outcomes) if include_dense else None,
            "reranker": _aggregate_backend_outcomes(rerank_outcomes) if include_rerank else None,
        },
    }


def _aggregate_backend_outcomes(outcomes: list[tuple[str | None, str]]) -> str | list[str] | dict | None:
    """Collapse per-query (backend, status) outcomes into one metadata value.

    Uniform success with a single distinct backend -> that backend string (so
    the common evaluation report stays compact and comparable). Mixed backends,
    failures, or no-hit executions -> a sorted list of distinct backends when
    the run is clean, or a dict ``{"backends": {...}, "statuses": {...}}`` with
    counts when statuses diverge (failed / no_hits present). No executed query
    -> None.
    """
    if not outcomes:
        return None
    backend_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for backend, status in outcomes:
        key = backend if backend is not None else "failed"
        backend_counts[key] = backend_counts.get(key, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
    if "failed" not in status_counts and len(backend_counts) == 1:
        return next(iter(backend_counts))
    if set(status_counts) <= {"success", "no_hits"} and len(backend_counts) == 1:
        return next(iter(backend_counts))
    return {"backends": backend_counts, "statuses": status_counts}


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _run_retrieval_eval_cli() -> int:
    import json as _json

    report = evaluate_retrievers()
    print(_json.dumps(report, indent=2))
    return 0


class ParentExpander:
    """Maps child chunk hits back to their parent page/chunk for context assembly.

    Chunk ids encode their source page and offset (e.g. ``p2:c0``). This expander
    returns the largest unit that still fits within ``max_parent_chars`` and shares
    the parent page prefix, so downstream context assembly can surface full parent
    sections instead of narrow child hits.
    """

    def __init__(self, chunks: list[Chunk], max_parent_chars: int = 2400) -> None:
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self._max_parent_chars = max_parent_chars

    def parent_for(self, child: Chunk) -> Chunk | None:
        if child.chunk_id in self._chunks_by_id:
            return self._chunks_by_id[child.chunk_id]
        if child.source_text and child.text:
            return Chunk(
                chunk_id=child.chunk_id,
                page=child.page,
                text=child.source_text,
                source_text=child.source_text,
            )
        return None

    def expand(self, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        expanded: dict[str, RetrievalHit] = {}
        for hit in hits:
            parent = self.parent_for(hit.chunk)
            if parent is None:
                continue
            parent_text = " ".join(parent.text.split())
            if len(parent_text) > self._max_parent_chars:
                parent_text = parent_text[: self._max_parent_chars]
            expanded[parent.chunk_id] = RetrievalHit(
                chunk=Chunk(
                    chunk_id=parent.chunk_id,
                    page=parent.page,
                    text=parent_text,
                    source_text=parent.source_text,
                ),
                score=hit.score,
            )
        ordered = sorted(expanded.values(), key=lambda hit: (-hit.score, hit.chunk.page, hit.chunk.chunk_id))
        return ordered


class ReciprocalRankFusion:
    """Deterministic RRF blend of multiple ranked hit lists.

    Uses ``1 / (k + rank)`` scoring with ``k=60`` (standard RRF) so scores are
    bounded in (0, 1] and independent of per-ranker magnitude, mitigating score
    normalization drift across sparse and dense rankers.
    """

    def __init__(self, k: float = 60.0) -> None:
        self.k = k

    def fuse(self, *ranked_lists: list[RetrievalHit]) -> list[RetrievalHit]:
        fused: dict[str, RetrievalHit] = {}
        for ranked in ranked_lists:
            for rank, hit in enumerate(ranked, start=1):
                score = 1.0 / (self.k + rank)
                if hit.chunk.chunk_id in fused:
                    previous = fused[hit.chunk.chunk_id]
                    fused[hit.chunk.chunk_id] = RetrievalHit(
                        chunk=hit.chunk,
                        score=previous.score + score,
                    )
                else:
                    fused[hit.chunk.chunk_id] = RetrievalHit(chunk=hit.chunk, score=score)
        ordered = sorted(
            fused.values(),
            key=lambda hit: (-round(hit.score, 9), hit.chunk.page, hit.chunk.chunk_id),
        )
        return ordered



def select_retrieved_context(
    question_list: list[str],
    document_pages: list[str],
    retriever: RetrieverAdapter | None = None,
    reranker: RerankerAdapter | None = None,
    limit_per_question: int | None = None,
    mode: str | None = None,
) -> tuple[str, bool]:
    """Select per-question retrieved context for prompt assembly.

    Defaults to the BM25 sparse path (``mode="bm25"``). Set ``mode="fusion"``
    to enable the ParentExpander + ReciprocalRankFusion path, which blends BM25
    sparse hits with FastEmbed dense hits (when available) and expands child
    chunk hits to parent page context. Set ``mode="dense"`` to use dense-only
    retrieval. Controlled via the ``OPENMATRIX_RETRIEVAL_MODE`` env var.
    """
    limit_per_question = limit_per_question or _retrieval_int("OPENMATRIX_CHUNKS_PER_QUESTION", 6)
    raw_mode = mode if mode is not None else os.environ.get("OPENMATRIX_RETRIEVAL_MODE", "bm25")
    mode = str(raw_mode or "bm25").strip().lower()
    if mode not in {"bm25", "dense", "fusion"}:
        mode = "bm25"
    chunks = build_page_chunks(document_pages)

    if mode == "bm25":
        return _select_sparse_context(
            question_list,
            chunks,
            retriever=retriever,
            reranker=reranker,
            limit_per_question=limit_per_question,
        )

    dense = DenseRetrieverAdapter()
    if mode == "dense":
        if not dense.is_available():
            return _select_sparse_context(
                question_list, chunks, retriever=retriever, reranker=reranker, limit_per_question=limit_per_question
            )
        selected: dict[str, RetrievalHit] = {}
        for question in question_list:
            hits = dense.retrieve(_financial_query(question), chunks, limit=limit_per_question)
            for hit in hits:
                selected[hit.chunk.chunk_id] = hit
        if not selected:
            # When dense retrieval yields zero hits (e.g. model offline/init failure
            # or low-scoring query), fall back to BM25 sparse context first before
            # falling back to the entire document.
            return _select_sparse_context(
                question_list, chunks, retriever=retriever, reranker=reranker, limit_per_question=limit_per_question
            )
        ordered_hits = sorted(selected.values(), key=lambda hit: (hit.chunk.page, hit.chunk.chunk_id))
        context_parts = [f"[Page {hit.chunk.page} | {hit.chunk.chunk_id}]\n{hit.chunk.text}" for hit in ordered_hits]
        return _context_from_hits(ordered_hits), False

    expander = ParentExpander(chunks)
    selected: dict[str, RetrievalHit] = {}

    for question in question_list:
        sparse_hits = (retriever or Bm25SRetriever()).retrieve(_financial_query(question), chunks, limit=limit_per_question)
        ranked_lists = [sparse_hits]
        if dense.is_available():
            ranked_lists.append(dense.retrieve(_financial_query(question), chunks, limit=limit_per_question))
        fused = ReciprocalRankFusion().fuse(*ranked_lists)
        expanded = expander.expand(fused)
        for hit in expanded[:limit_per_question]:
            selected[hit.chunk.chunk_id] = hit

    if not selected:
        return "\n".join(document_pages)[:_retrieval_int("OPENMATRIX_CONTEXT_MAX_CHARS", 48000)], True

    ordered_hits = sorted(selected.values(), key=lambda hit: (hit.chunk.page, hit.chunk.chunk_id))
    context_parts = [f"[Page {hit.chunk.page} | {hit.chunk.chunk_id}]\n{hit.chunk.text}" for hit in ordered_hits]
    return _context_from_hits(ordered_hits), False


def _select_sparse_context(
    question_list: list[str],
    chunks: list[Chunk],
    retriever: RetrieverAdapter | None = None,
    reranker: RerankerAdapter | None = None,
    limit_per_question: int | None = None,
) -> tuple[str, bool]:
    limit_per_question = limit_per_question or _retrieval_int("OPENMATRIX_CHUNKS_PER_QUESTION", 6)
    retriever = retriever or Bm25SRetriever()
    reranker = reranker or get_reranker_adapter()
    selected: dict[str, RetrievalHit] = {}

    for question in question_list:
        candidate_pool = min(_rerank_candidate_pool(minimum=limit_per_question), len(chunks))
        candidates = retriever.retrieve(_financial_query(question), chunks, limit=candidate_pool)
        if re.search(r"what was|how much|how many|%", question, re.I):
            boosted = []
            for hit in candidates:
                bonus = 0.0
                if len(re.findall(r"\d[\d,]*(?:\.\d+)?", hit.chunk.text)) >= 2:
                    bonus += 2.0
                    if "total revenue" in question.lower() and re.search(r"total revenue\s+\$?\s*[\d,]+", hit.chunk.text, re.I):
                        bonus += 8.0
                boosted.append(RetrievalHit(chunk=hit.chunk, score=hit.score + bonus))
            candidates = boosted
            candidates.sort(key=lambda hit: -hit.score)
        hits = reranker.rerank(question, candidates, limit_per_question)
        for hit in hits:
            selected[hit.chunk.chunk_id] = hit

    if not selected:
        # Deduplicate fallback by original page to avoid amplification when a page
        # is split into many chunks. Preserve original page order using the first
        # occurrence of each page number.
        seen_pages = set()
        page_texts = []
        for chunk in chunks:
            if chunk.page not in seen_pages:
                seen_pages.add(chunk.page)
                page_texts.append(chunk.source_text)
        return "\n".join(page_texts)[:_retrieval_int("OPENMATRIX_CONTEXT_MAX_CHARS", 48000)], True

    ordered_hits = sorted(selected.values(), key=lambda hit: (hit.chunk.page, hit.chunk.chunk_id))
    context_parts = [f"[Page {hit.chunk.page} | {hit.chunk.chunk_id}]\n{hit.chunk.text}" for hit in ordered_hits]
    return _context_from_hits(ordered_hits), False


class TenacityRetryAdapter:
    """Narrow retry adapter for transient provider/network failures.

    Uses Tenacity (Apache-2.0) to retry transient failures with bounded attempts
    and exponential backoff. Non-transient errors (auth, schema, PDF) fail fast.
    """

    TRANSIENT = (TimeoutError, httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)

    def __init__(self, max_attempts: int = 3, min_wait_seconds: float = 0.5, max_wait_seconds: float = 4.0) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self.min_wait_seconds = min_wait_seconds
        self.max_wait_seconds = max_wait_seconds

    def is_transient(self, exc: BaseException) -> bool:
        if isinstance(exc, self.TRANSIENT):
            return True
        status_code = getattr(exc, "status_code", None)
        if status_code in (429, 502, 503):
            return True
        message = (getattr(exc, "message", None) or _redact_sensitive_text(exc, locals().get("api_key"))).lower()
        transient_markers = (
            "timeout",
            "timed out",
            "connection refused",
            "connection reset",
            "network",
            "temporarily unavailable",
            "503",
            "502",
            "429",
            "rate limit",
            "service unavailable",
        )
        return any(marker in message for marker in transient_markers)

    def decorate(self, coro):
        """Return a retrying wrapper around a coroutine function."""
        import tenacity
        from tenacity import AsyncRetrying

        async def wrapped(*args, **kwargs):
            async for attempt in AsyncRetrying(
                reraise=True,
                stop=tenacity.stop_after_attempt(self.max_attempts),
                wait=tenacity.wait_exponential(
                    multiplier=self.min_wait_seconds,
                    max=self.max_wait_seconds,
                ),
                retry=tenacity.retry_if_exception(self.is_transient),
            ):
                with attempt:
                    return await coro(*args, **kwargs)

        return wrapped



class PypdfParserAdapter:
    """Default baseline PDF text extractor backed by pypdf (BSD-3-Clause)."""

    def __call__(self, pdf_path: str, max_pages: int = MAX_PDF_PAGES, ocr_backend=None, ocr_preprocessor=None) -> list[str]:
        return extract_pages_from_pdf(
            pdf_path, ocr_backend=ocr_backend, max_pages=max_pages, ocr_preprocessor=ocr_preprocessor
        )


class DoclingParserAdapter:
    """Optional structured PDF parser adapter backed by Docling (MIT, IBM/LF AI).

    Extracts markdown/text pages preserving reading order and table layout.
    Falls back gracefully to pypdf when docling is not installed or raises an error.
    """

    def __init__(self) -> None:
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                from docling.document_converter import DocumentConverter  # conditional dependency (MIT)

                self._available = True
            except Exception:
                self._available = False
        return self._available

    def __call__(self, pdf_path: str, max_pages: int = MAX_PDF_PAGES, ocr_backend=None, ocr_preprocessor=None) -> list[str]:
        if not self.is_available():
            return PypdfParserAdapter()(pdf_path, max_pages=max_pages, ocr_backend=ocr_backend, ocr_preprocessor=ocr_preprocessor)
        try:
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()
            result = converter.convert(pdf_path)
            # Docling exports structured markdown per document; split by page breaks if available,
            # or wrap into normalized page strings.
            md = result.document.export_to_markdown()
            pages = [p.strip() for p in md.split("\n\n---\n\n") if p.strip()] or [md]
            if len(pages) > max_pages:
                raise PdfTooManyPagesError(f"PDF exceeds {max_pages} pages limit.")
            return pages
        except PdfTooManyPagesError:
            raise
        except Exception:
            return PypdfParserAdapter()(pdf_path, max_pages=max_pages, ocr_backend=ocr_backend, ocr_preprocessor=ocr_preprocessor)


def get_parser_adapter(name: str | None = None) -> ParserAdapter:
    """Return configured ParserAdapter instance (defaults to pypdf)."""
    name = (name or os.environ.get("OPENMATRIX_PARSER", "pypdf")).strip().lower()
    if name == "docling":
        return DoclingParserAdapter()
    return PypdfParserAdapter()


def extract_pages_from_pdf(pdf_path: str, ocr_backend=None, max_pages: int = MAX_PDF_PAGES, ocr_preprocessor=None) -> list[str]:
    reader = PdfReader(pdf_path)
    if len(reader.pages) > max_pages:
        raise PdfTooManyPagesError(
            f"PDF has {len(reader.pages)} pages, exceeding the {max_pages}-page limit."
        )
    pages = [page.extract_text() or "" for page in reader.pages]

    # Scanned-PDF verification: only preprocess when no page yields extractable
    # text and the preprocessor is explicitly enabled. This keeps the optional
    # OCRmyPDF path off for text PDFs and off unless configured on.
    if (
        not any(text.strip() for text in pages)
        and ocr_preprocessor is not None
        and getattr(ocr_preprocessor, "enabled", False)
    ):
        preprocessed = ocr_preprocessor.preprocess(pdf_path)
        if preprocessed:
            try:
                ocr_reader = PdfReader(preprocessed)
                ocr_pages = [page.extract_text() or "" for page in ocr_reader.pages]
                if any(text.strip() for text in ocr_pages):
                    pages = ocr_pages
            except Exception:
                pass
            finally:
                if preprocessed.endswith(".ocr.pdf"):
                    os.remove(preprocessed)

    if any(text.strip() for text in pages) or ocr_backend is None:
        return pages

    if callable(ocr_backend) and ocr_backend is not True:
        return [ocr_backend(pdf_path, index) or "" for index in range(len(pages))]

    try:
        from paddleocr import PaddleOCR  # optional dependency (Apache-2.0)
        paddle = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    except Exception:
        return pages
    ocr = paddle.ocr

    ocr_pages = []
    for page_number in range(len(pages)):
        image_path = render_page_to_image(pdf_path, page_number)
        if image_path is None:
            ocr_pages.append("")
            continue
        try:
            result = ocr(image_path)
            ocr_pages.append(extract_ocr_text(result))
        except Exception:
            ocr_pages.append("")
    return ocr_pages


def render_page_to_image(pdf_path: str, page_number: int) -> str | None:
    try:
        from pdf2image import convert_from_path  # optional dependency
    except ImportError:
        return None
    try:
        images = convert_from_path(pdf_path, first_page=page_number + 1, last_page=page_number + 1)
    except Exception:
        return None
    if not images:
        return None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        images[0].save(tmp.name)
        return tmp.name


def extract_ocr_text(result) -> str:
    lines = []
    for page_result in result or []:
        if not page_result:
            continue
        for item in page_result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = item[1]
                if isinstance(text, (list, tuple)) and text:
                    text = text[0]
                if text:
                    lines.append(str(text))
    return "\n".join(lines)


class OcrPreprocessor:
    """Optional OCRmyPDF (MPL-2.0) preprocessor for scanned PDFs.

    When explicitly enabled and OCRmyPDF is importable, it converts a scanned PDF
    into a searchable PDF before the existing parse chain. When disabled or
    unavailable it is a no-op passthrough so current behavior is unchanged.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import ocrmypdf  # conditional dependency (MPL-2.0)

                self._available = True
            except Exception:
                self._available = False
        return self._available

    def preprocess(self, pdf_path: str, output_path: str | None = None) -> str | None:
        """OCR a scanned PDF into a searchable PDF.

        Returns the path of the OCR'd PDF, or ``None`` when disabled/unavailable
        or the underlying OCR binary fails (callers keep using the original file).
        """
        if not self.enabled or not self.is_available():
            return None
        import ocrmypdf

        if output_path is None:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ocr.pdf") as tmp:
                output_path = tmp.name
        try:
            ocrmypdf.ocr(pdf_path, output_path, skip_text=True)
        except Exception:
            if output_path.endswith(".ocr.pdf"):
                os.remove(output_path)
            return None
        return output_path


class PdfPlumberCoordinateAdapter:
    """Extract normalized word coordinates from text-based PDFs (pdfplumber, MIT).

    The adapter isolates product code from third-party page/word objects: callers
    receive plain dictionaries with page index and x0/y0/x1/y1 bounding boxes.
    pdfplumber is an optional dependency; when unavailable extraction falls back
    to an empty structure so the pypdf page-text pipeline continues to work.
    """

    def extract(self, pdf_path: str) -> list[dict]:
        try:
            import pdfplumber  # optional dependency (MIT)
        except ImportError:
            return []

        coordinates = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page_number, page in enumerate(pdf.pages):
                    for word in page.extract_words() or []:
                        coordinates.append(
                            {
                                "page": page_number,
                                "x0": float(word.get("x0") or 0),
                                "y0": float(word.get("top") or 0),
                                "x1": float(word.get("x1") or 0),
                                "y1": float(word.get("bottom") or 0),
                                "text": str(word.get("text") or ""),
                            }
                        )
        except Exception:
            return []

        return coordinates


class TableExtractorAdapter:
    """Extract normalized tables from fixed-layout PDFs using Camelot (MIT).

    The adapter isolates product code from the third-party page/table objects:
    callers receive plain lists of row dictionaries only.
    """

    def extract(self, pdf_path: str, pages: str = "1", flavor: str = "lattice") -> list[dict]:
        try:
            import camelot
        except ImportError:
            return []

        try:
            tables = camelot.read_pdf(pdf_path, pages=pages, flavor=flavor)
        except Exception:
            return []

        extracted = []
        for table_index, table in enumerate(tables):
            headers = None
            rows = []
            for row_index, row in enumerate(table.df.values.tolist()):
                cells = [str(cell).strip() if cell is not None else "" for cell in row]
                if row_index == 0:
                    headers = cells
                    continue
                if any(cells):
                    rows.append(cells)
            if not rows:
                continue
            table_page = getattr(table, "page", None)
            if headers:
                for cells in rows:
                    extracted.append(
                        {
                            "table_index": table_index,
                            "page": table_page,
                            "headers": headers,
                            "cells": cells,
                            "row": dict(zip(headers, cells)),
                        }
                    )
            else:
                for cells in rows:
                    extracted.append(
                        {
                            "table_index": table_index,
                            "page": table_page,
                            "headers": headers,
                            "cells": cells,
                            "row": {str(index): value for index, value in enumerate(cells)},
                        }
                    )
        return extracted


def table_rows_to_text(rows: list[dict]) -> str:
    """Render normalized table rows into plain, vendor-free text lines.

    Each row becomes ``Header: value | Header: value`` so the rendered text can
    feed both retrieval context and citation verification without exposing any
    third-party table object.
    """
    lines = []
    for row in rows:
        headers = row.get("headers") or []
        cells = row.get("cells") or []
        parts = []
        for index, value in enumerate(cells):
            label = headers[index] if index < len(headers) else str(index)
            parts.append(f"{label}: {value}")
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def enrich_pages_with_tables(pages: list[str], rows: list[dict]) -> list[str]:
    """Append normalized table text to the page it was extracted from.

    Citation verification runs ``quote in page_text``; appending the rendered
    table lines to the matching page lets exact quotes inside table cells verify
    against the same page while leaving page count unchanged.
    """
    enriched = [page or "" for page in pages]
    for row in rows:
        page = row.get("page")
        index = int(page) - 1 if isinstance(page, (int, float)) and page >= 1 else None
        if index is None or index >= len(enriched):
            index = 0
        line = table_rows_to_text([row])
        if line:
            enriched[index] = (enriched[index] + "\n" + line) if enriched[index] else line
    return enriched


def extract_tables_from_pdf(
    pdf_path: str,
    pages: str = "1",
    flavor: str = "lattice",
    adapter: TableExtractorAdapter | None = None,
) -> list[dict]:
    adapter = adapter or TableExtractorAdapter()
    return adapter.extract(pdf_path, pages=pages, flavor=flavor)


def health_status() -> dict:
    """Return a minimal application health projection (placeholder #18)."""
    return {"status": "ok", "service": "openmatrix"}


def matrix_run_summary(run_id: str, db_path: Path | str | None = None) -> dict | None:
    """Return a compact status summary for a persisted matrix run (placeholder #19)."""
    run = load_matrix_run(run_id, db_path=db_path)
    if run is None:
        return None
    cells = run["cells"]
    statuses = [cell.get("status") for cell in cells]
    return {
        "run_id": run_id,
        "total": len(cells),
        "completed": statuses.count("completed"),
        "failed": statuses.count("failed"),
        "needs_review": statuses.count("needs_review"),
    }


def list_recent_runs(limit: int = 10, db_path: Path | str | None = None) -> list[str]:
    """Return the most recent persisted matrix run ids (placeholder #20)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    limit = max(1, min(int(limit), 100))
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT run_id FROM matrix_cells
            GROUP BY run_id
            ORDER BY MAX(rowid) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row[0] for row in rows]


def run_cell_errors(run_id: str, db_path: Path | str | None = None) -> list[dict]:
    """Return question and error pairs for failed cells in a run (placeholder #21)."""
    run = load_matrix_run(run_id, db_path=db_path)
    if run is None:
        return []
    return [
        {"question": cell.get("question"), "error": cell.get("error")}
        for cell in run["cells"]
        if cell.get("status") == "failed"
    ]


def run_status_counts(db_path: Path | str | None = None) -> dict:
    """Return counts of persisted cells grouped by terminal status (placeholder #22)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count FROM matrix_cells
            GROUP BY status
            """
        ).fetchall()
    counts: dict = {}
    for status, count in rows:
        counts[status] = int(count)
    return counts


def citation_status_counts(db_path: Path | str | None = None) -> dict:
    """Return counts of persisted cells grouped by citation status (placeholder #23)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT citation_status, COUNT(*) AS count FROM matrix_cells
            GROUP BY citation_status
            """
        ).fetchall()
    counts: dict = {}
    for citation_status, count in rows:
        counts[citation_status or "unknown"] = int(count)
    return counts


def average_answer_length_by_status(db_path: Path | str | None = None) -> dict:
    """Return average answer length per cell status (placeholder #24)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT status, AVG(LENGTH(answer)) AS avg_len FROM matrix_cells
            GROUP BY status
            """
        ).fetchall()
    averages: dict = {}
    for status, avg_len in rows:
        averages[status] = float(avg_len or 0.0)
    return averages


def total_cell_count(db_path: Path | str | None = None) -> int:
    """Return total count of persisted cells (placeholder #25)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM matrix_cells
            """
        ).fetchone()
    return int(row[0])


def most_failed_questions(
    db_path: Path | str | None = None, limit: int = 10
) -> list[dict]:
    """Return most frequently failed questions across runs (placeholder #26)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT question, COUNT(*) AS fail_count FROM matrix_cells
            WHERE status = 'failed'
            GROUP BY question
            ORDER BY fail_count DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [
        {"question": question, "fail_count": int(fail_count)}
        for question, fail_count in rows
    ]


def citation_verification_rate(db_path: Path | str | None = None) -> float:
    """Return citation verification success rate over completed cells (placeholder #27)."""
    db_path = matrix_db_path(db_path)
    init_matrix_store(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN citation_status IN ('verified_exact', 'verified_normalized', 'verified_fuzzy') THEN 1 ELSE 0 END) AS verified
            FROM matrix_cells
            WHERE status = 'completed'
            """
        ).fetchone()
    if row is None or not row[0]:
        return 0.0
    return float(row[1] or 0) / float(row[0])


def _fuzzy_partial_ratio(needle: str, haystack: str) -> float:
    """Calculate partial ratio string similarity (0.0 to 100.0)."""
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 100.0
    try:
        from rapidfuzz import fuzz
        return float(fuzz.partial_ratio(needle, haystack))
    except ImportError:
        import difflib
        len_needle = len(needle)
        len_haystack = len(haystack)
        if len_haystack <= len_needle:
            return difflib.SequenceMatcher(None, needle, haystack).ratio() * 100.0
        best = 0.0
        step = max(1, len_needle // 4)
        for i in range(0, len_haystack - len_needle + 1, step):
            sub = haystack[i : i + len_needle]
            r = difflib.SequenceMatcher(None, needle, sub).ratio() * 100.0
            if r > best:
                best = r
            if best >= 95.0:
                break
        return best


def verify_matrix_citations(
    rows: list[dict],
    document_pages: list[str],
    exact_only: bool = False,
) -> list[dict]:
    """Four-layer citation verification funnel (H4 / Session Checkpoint).

    Funnel layers:
    1. Exact string match (citation_status='verified_exact', match_score=1.0)
    2. Whitespace & newline normalized match (citation_status='verified_normalized', match_score=0.95)
    3. Sliding window fuzzy match (citation_status='verified_fuzzy', match_score>=0.82)
    4. Not found / Not applicable (citation_status='not_found' | 'not_applicable', match_score=0.0)
    """
    verified_rows = []
    for row in rows:
        verified = dict(row)
        quote = verified.get("quote")
        verified["page"] = None
        verified["match_score"] = 0.0

        if not isinstance(quote, str) or not quote.strip():
            verified["citation_status"] = "not_applicable"
            verified.update(assess_answer_support(verified.get("question", ""), verified.get("answer"), quote,
                document_pages[verified["page"] - 1] if verified.get("page") else ""))
            verified_rows.append(verified)
            continue

        verified["citation_status"] = "not_found"

        for page_number, page_text in enumerate(document_pages, start=1):
            # 1. Exact verbatim match (1.0)
            if quote in page_text:
                verified["citation_status"] = "verified_exact"
                verified["page"] = page_number
                verified["match_score"] = 1.0
                break

        if verified["citation_status"] != "not_found" or exact_only:
            verified.update(assess_answer_support(verified.get("question", ""), verified.get("answer"), quote,
                document_pages[verified["page"] - 1] if verified.get("page") else ""))
            verified_rows.append(verified)
            continue

        norm_quote = " ".join(quote.split())
        for page_number, page_text in enumerate(document_pages, start=1):
            # 2. Whitespace & newline normalized match (0.95)
            norm_page = " ".join((page_text or "").split())
            if norm_quote in norm_page:
                verified["citation_status"] = "verified_normalized"
                verified["page"] = page_number
                verified["match_score"] = 0.95
                break

        if verified["citation_status"] != "not_found":
            verified.update(assess_answer_support(verified.get("question", ""), verified.get("answer"), quote,
                document_pages[verified["page"] - 1] if verified.get("page") else ""))
            verified_rows.append(verified)
            continue

        for page_number, page_text in enumerate(document_pages, start=1):
            # 3. Sliding window fuzzy matching (similarity >= 82%)
            norm_page = " ".join((page_text or "").split())
            sim = _fuzzy_partial_ratio(norm_quote, norm_page)
            if sim >= 82.0:
                verified["citation_status"] = "verified_fuzzy"
                verified["page"] = page_number
                verified["match_score"] = round(sim / 100.0, 2)
                break

        verified.update(assess_answer_support(verified.get("question", ""), verified.get("answer"), quote,
            document_pages[verified["page"] - 1] if verified.get("page") else ""))
        verified_rows.append(verified)

    return verified_rows

def answer_support_rate(db_path=None):
    init_matrix_store(db_path)
    with closing(sqlite3.connect(matrix_db_path(db_path))) as conn:
        supported, checked = conn.execute("""SELECT
            COALESCE(SUM(support_status = 'supported'), 0),
            COALESCE(SUM(support_status IN ('supported', 'partial', 'unsupported')), 0)
            FROM matrix_cells""").fetchone()
    return supported / checked if checked else 0.0


@app.get("/api/v1/stats/answer-support-rate")
async def api_answer_support_rate():
    return {"rate": answer_support_rate()}


class PdfInputError(ValueError):
    """Stable error taxonomy for PDF input path failures."""




async def _stream_upload_to_temp(upload_file: UploadFile, max_bytes: int, digest=None) -> str:
    """Stream an upload in 1 MiB chunks and remove partial files on rejection."""
    temp_path = ""
    total = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_path = temp_file.name
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PdfTooLargeError(f"PDF exceeds the {max_bytes // (1024 * 1024)} MB size limit.")
                temp_file.write(chunk)
                if digest is not None:
                    digest.update(chunk)
        return temp_path
    except Exception:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
        raise

class PdfTooLargeError(PdfInputError):
    pass


class PdfTooManyPagesError(PdfInputError):
    pass


class PdfUnreadableError(PdfInputError):
    pass


def pdf_input_error_message(exc: PdfInputError) -> str:
    return _redact_sensitive_text(exc, locals().get("api_key"))


@app.get("/api/v1/model-router")
async def get_model_router_config():
    """Show the current task-type model routing configuration (P0-A)."""
    return ModelRouter("default").describe()


@app.get("/api/v1/schedules")
async def list_schedules_rest():
    """List registered scheduled research runs (H7 REST exposure)."""
    from agent.scheduler import default_manager
    return {"schedules": default_manager().list(), "persistent": default_manager().persistent}


def _schedule_research_callback(schedule_id: str, entry) -> None:
    """Fire a scheduled research run via research_run (runs in ticker thread).

    Mirrors mcp_server's callback: both construction and waiting run inside
    ``asyncio.run`` so research_run always has a running event loop.
    """
    del schedule_id
    import asyncio
    from agent.research_run import research_run

    async def _run() -> None:
        controller = research_run(entry.research_id, entry.objective, entry.context)
        await controller.wait()

    asyncio.run(_run())


@app.post("/api/v1/schedules")
async def create_schedule_rest(payload: dict = Body(...)):
    """Register a scheduled research run (H7 REST exposure).

    Body: {"schedule_id"?, "cron", "objective", "research_id", "context"?}
    schedule_id defaults to a generated hex id when omitted.
    """
    from agent.scheduler import default_manager

    cron = payload.get("cron")
    objective = payload.get("objective")
    research_id = payload.get("research_id")
    if not isinstance(cron, str) or not cron.strip():
        return JSONResponse({"error": "cron must be a non-empty string"}, status_code=400)
    if not isinstance(objective, str) or not objective.strip():
        return JSONResponse({"error": "objective must be a non-empty string"}, status_code=400)
    if not isinstance(research_id, str) or not research_id.strip():
        return JSONResponse({"error": "research_id must be a non-empty string"}, status_code=400)
    schedule_id = payload.get("schedule_id") or uuid.uuid4().hex
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return JSONResponse({"error": "schedule_id must be a non-empty string"}, status_code=400)
    context = payload.get("context")
    if context is not None and not isinstance(context, dict):
        return JSONResponse({"error": "context must be an object"}, status_code=400)

    manager = default_manager(_schedule_research_callback)
    entry = manager.register(schedule_id, cron, objective, research_id, context)
    if entry is None:
        dup = any(s["schedule_id"] == schedule_id for s in manager.list())
        detail = "schedule already exists" if dup else "invalid cron expression"
        return JSONResponse({"error": detail}, status_code=400)
    manager.start_ticker()
    return {
        "schedule_id": entry.schedule_id,
        "cron": entry.cron,
        "objective": entry.objective,
        "research_id": entry.research_id,
        "status": "registered",
        "persistent": manager.persistent,
    }


@app.post("/api/v1/schedules/{schedule_id}/pause")
async def pause_schedule_rest(schedule_id: str):
    from agent.scheduler import default_manager
    if default_manager().pause(schedule_id):
        return {"schedule_id": schedule_id, "status": "paused", "persistent": default_manager().persistent}
    return JSONResponse({"error": "schedule not found"}, status_code=404)


@app.post("/api/v1/schedules/{schedule_id}/resume")
async def resume_schedule_rest(schedule_id: str):
    from agent.scheduler import default_manager
    if default_manager().resume(schedule_id):
        return {"schedule_id": schedule_id, "status": "active", "persistent": default_manager().persistent}
    return JSONResponse({"error": "schedule not found"}, status_code=404)


@app.delete("/api/v1/schedules/{schedule_id}")
async def delete_schedule_rest(schedule_id: str):
    """Cancel a scheduled research run (H7 REST exposure)."""
    from agent.scheduler import default_manager
    if default_manager().unregister(schedule_id):
        return {"schedule_id": schedule_id, "status": "cancelled"}
    return JSONResponse({"error": "schedule not found"}, status_code=404)


@app.post("/api/models")
async def get_models(payload: dict = Body(...)):
    """Fetch available models from the proxy using a JSON POST body."""
    base_url = str(payload.get("base_url", ""))
    api_key = str(payload.get("api_key", ""))
    if not base_url or not api_key:
        return JSONResponse({"error": "JSON body must include base_url and api_key"}, status_code=422)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as http_client:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=model_request_headers(base_url),
                http_client=http_client,
            )
            retry_adapter = TenacityRetryAdapter()

            async def list_models():
                response = await client.models.list()
                return [m.id for m in response.data]

            models = await retry_adapter.decorate(list_models)()
            return {"status": "success", "models": models}
    except Exception as exc:
        detail = _redact_sensitive_text(exc, api_key, base_url)
        return JSONResponse({"error": detail}, status_code=500)


@app.get("/api/models")
async def get_models_get():
    return JSONResponse(
        {
            "error": "GET /api/models is not supported; use POST /api/models with JSON body {'base_url': '...', 'api_key': '...'}."
        },
        status_code=405,
        headers={"Allow": "POST"},
    )


@app.get("/api/matrix-runs/{run_id}")
async def get_matrix_run(run_id: str):
    run = load_matrix_run(run_id)
    if run is None:
        return JSONResponse({"error": "Matrix run not found"}, status_code=404)
    return run


@app.get("/api/v1/health")
async def api_health():
    return health_status()


@app.get("/api/v1/health/detailed")
async def api_health_detailed():
    """Health with per-component status (observability)."""
    base = health_status()
    components: dict = {}

    def _probe(name: str, fn) -> None:
        try:
            components[name] = "ok" if fn() else "unavailable"
        except Exception:  # noqa: BLE001
            components[name] = "error"

    from storage import effective_storage_engines, get_metadata_store, get_object_store

    components.update(effective_storage_engines())
    _probe("metadata_store", lambda: (get_metadata_store(), True)[1])
    _probe("object_store", lambda: (get_object_store(), True)[1])
    _probe("dense_retriever", lambda: DenseRetrieverAdapter().is_available())
    _probe("vector_store", lambda: get_vector_store_adapter().is_available())
    return {"status": base.get("status"), "service": base.get("service"), "components": components}


@app.get("/api/v1/runs")
async def api_list_recent_runs(limit: int = Query(10, ge=1, le=100)):
    return {"runs": list_recent_runs(limit=limit)}


@app.get("/api/v1/runs/{run_id}/documents/{document_index}/file", dependencies=[Depends(require_api_v1_auth_dependency)])
async def api_run_source_file(run_id: str, document_index: int):
    try:
        source = _safe_run_file_path(run_id, document_index)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not source.is_file():
        return JSONResponse({"error": "source file not found"}, status_code=404)
    return FileResponse(str(source), media_type="application/pdf", filename=source.name)


class QuoteBboxRequest(BaseModel):
    quote: str | None = None
    page: int | None = None


@app.post("/api/v1/runs/{run_id}/cells/{cell_id}/bboxes", dependencies=[Depends(require_api_v1_auth_dependency)])
async def api_run_cell_bboxes(run_id: str, cell_id: str, payload: QuoteBboxRequest):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cell_id or ""):
        return JSONResponse({"error": "invalid cell_id"}, status_code=400)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id or ""):
        return JSONResponse({"error": "invalid run_id"}, status_code=400)
    run = load_matrix_run(run_id)
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    cell = next((item for item in run.get("cells", []) if item.get("cell_id") == cell_id), None)
    if cell is None:
        return JSONResponse({"error": "cell not found"}, status_code=404)
    quote = (payload.quote or cell.get("quote") or "").strip()
    if not quote:
        return JSONResponse({"error": "quote is required"}, status_code=400)
    page = payload.page if payload.page is not None else cell.get("page")
    if page is not None and (not isinstance(page, int) or page < 1):
        return JSONResponse({"error": "page must be a positive integer"}, status_code=400)
    try:
        source = _safe_run_file_path(run_id, int(cell.get("document_index", 0)))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not source.is_file():
        return JSONResponse({"error": "source file not found"}, status_code=404)
    from mcp_server import _locate_quote_bboxes
    result = await asyncio.to_thread(
        _locate_quote_bboxes,
        str(source.relative_to(_run_files_root())),
        quote,
        page=page,
        root=_run_files_root(),
    )
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            parsed.setdefault("quote", quote)
        return parsed
    except (TypeError, json.JSONDecodeError):
        return JSONResponse({"error": str(result)}, status_code=422)


@app.get("/api/v1/runs/{run_id}")
async def api_run_summary(run_id: str):
    summary = matrix_run_summary(run_id)
    if summary is None:
        return JSONResponse({"error": "Matrix run not found"}, status_code=404)
    return summary


@app.get("/api/v1/runs/{run_id}/errors")
async def api_run_cell_errors(run_id: str):
    return {"errors": run_cell_errors(run_id)}


@app.get("/api/v1/stats/status-counts")
async def api_run_status_counts():
    return run_status_counts()


@app.get("/api/v1/stats/citation-status-counts")
async def api_citation_status_counts():
    return citation_status_counts()


@app.get("/api/v1/stats/average-answer-length")
async def api_average_answer_length_by_status():
    return average_answer_length_by_status()


@app.get("/api/v1/stats/total-cells")
async def api_total_cell_count():
    return {"total": total_cell_count()}


@app.get("/api/v1/stats/most-failed-questions")
async def api_most_failed_questions(limit: int = Query(10, ge=1, le=100)):
    return {"questions": most_failed_questions(limit=limit)}


@app.get("/api/v1/stats/citation-verification-rate")
async def api_citation_verification_rate():
    return {"rate": citation_verification_rate()}

def safe_cell(value):
    value = str(value or "")
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@app.get("/api/v1/export-csv/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.get("/api/export-csv/{run_id}")
def export_csv(run_id: str):
    run = load_matrix_run(run_id)
    if run is None:
        return JSONResponse({"error": "Matrix run not found"}, status_code=404)

    import csv
    import io

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["document_name", "question", "answer", "quote", "citation_status", "page", "support_status", "support_reason"])
        for cell in run["cells"]:
            writer.writerow([
                safe_cell(cell.get("document_name", "")),
                safe_cell(cell.get("question", "")),
                safe_cell(cell.get("answer", "")),
                safe_cell(cell.get("quote", "")),
                safe_cell(cell.get("citation_status", "")),
                safe_cell(cell.get("page", "")),
                safe_cell(cell.get("support_status", "")),
                safe_cell(cell.get("support_reason", "")),
            ])
            buffer.seek(0)
            data = buffer.read()
            buffer.seek(0)
            buffer.truncate(0)
            yield data
    headers = {"Content-Disposition": f'attachment; filename="matrix_{run_id}.csv"'}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


@app.get("/api/v1/export-xlsx/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.get("/api/export-xlsx/{run_id}")
def export_xlsx(run_id: str):
    """Export a matrix run as a real .xlsx workbook via openpyxl (MIT).

    Applies the same formula-injection neutralization as the CSV export so cells
    starting with ``= + - @`` cannot execute formulas on re-import.
    """
    run = load_matrix_run(run_id)
    if run is None:
        return JSONResponse({"error": "Matrix run not found"}, status_code=404)

    try:
        import io

        from openpyxl import Workbook
    except Exception:
        return JSONResponse({"error": "XLSX export unavailable (openpyxl missing)"}, status_code=500)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Matrix Results"
    headers = ["document_name", "question", "answer", "quote", "citation_status", "page", "support_status", "support_reason"]
    sheet.append(headers)
    for cell in run["cells"]:
        sheet.append([
            safe_cell(cell.get("document_name", "")),
            safe_cell(cell.get("question", "")),
            safe_cell(cell.get("answer", "")),
            safe_cell(cell.get("quote", "")),
            safe_cell(cell.get("citation_status", "")),
            safe_cell(cell.get("page", "")),
            safe_cell(cell.get("support_status", "")),
            safe_cell(cell.get("support_reason", "")),
        ])

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    headers_map = {"Content-Disposition": f'attachment; filename="matrix_{run_id}.xlsx"'}
    return StreamingResponse(buffer, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ), headers=headers_map)


def _rest_export(run_id: str, format: str):
    """Export only into the server-fixed export root; never trust a returned path."""
    import json as _json
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        return JSONResponse({"error": "invalid run id"}, status_code=400)
    run = load_matrix_run(run_id)
    if run is None:
        return JSONResponse({"error": "Matrix run not found"}, status_code=404)
    try:
        import mcp_server
        result = mcp_server.export_report(run_id, format=format, output_dir=f"rest/{run_id}")
        if isinstance(result, str) and result.startswith("Error"):
            return JSONResponse({"error": result}, status_code=500)
        payload = _json.loads(result)
        path = Path(payload.get("path", "")).resolve()
        root = mcp_server._export_root().resolve()
        path.relative_to(root)
    except Exception:
        return JSONResponse({"error": "export failed"}, status_code=500)
    if not path.is_file() or path.stat().st_size == 0:
        return JSONResponse({"error": "export artifact missing"}, status_code=404)
    media_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    return FileResponse(path, media_type=media_types[format], filename=f"matrix_{run_id}.{format}")


@app.get("/api/v1/export-pdf/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.get("/api/export-pdf/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
def export_pdf(run_id: str):
    return _rest_export(run_id, "pdf")


@app.get("/api/v1/export-docx/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.get("/api/export-docx/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
def export_docx(run_id: str):
    return _rest_export(run_id, "docx")


@app.get("/api/v1/export-pptx/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.get("/api/export-pptx/{run_id}", dependencies=[Depends(require_api_v1_auth_dependency)])
def export_pptx(run_id: str):
    return _rest_export(run_id, "pptx")


TERMINAL_CELL_STATUSES = {"completed", "failed", "needs_review", "not_disclosed"}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    """FastAPI-native SSE stream of per-cell progress events for a matrix run.

    Emits a ``cell`` event for every terminal cell as it appears, then a final
    ``run_complete`` event once all cells have reached a terminal state. Uses the
    persisted run as the single source of truth, so no third-party SSE package or
    event replay is needed.
    """

    async def generate():
        seen: set[str] = set()
        not_found_waits = 0
        while True:
            run = load_matrix_run(run_id)
            if run is None:
                not_found_waits += 1
                if not_found_waits >= 20:
                    yield 'event: error\ndata: {"detail": "run not found"}\n\n'
                    return
                await asyncio.sleep(0.1)
                continue
            cells = run["cells"]
            for cell in cells:
                cell_id = cell["cell_id"]
                if cell_id in seen:
                    continue
                status = cell.get("status")
                if status in TERMINAL_CELL_STATUSES:
                    seen.add(cell_id)
                    payload = {
                        "type": "cell",
                        "run_id": run_id,
                        "cell_id": cell_id,
                        "document_index": cell.get("document_index"),
                        "question_index": cell.get("question_index"),
                        "question": cell.get("question"),
                        "status": status,
                    }
                    yield f'event: cell\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
            if all(cell.get("status") in TERMINAL_CELL_STATUSES for cell in cells):
                yield f'event: run_complete\ndata: {json.dumps({"type": "run_complete", "run_id": run_id}, ensure_ascii=False)}\n\n'
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_tasks: TaskQueueAdapter | None = None


def _task_queue() -> TaskQueueAdapter:
    global _tasks
    if _tasks is None:
        _tasks = get_task_queue_adapter()
    return _tasks


@app.post("/api/v1/tasks")
async def create_task(task_type: str = Form(...), payload: str = Form("{}")):
    """Enqueue a background task and return its task id (Queue integration)."""
    try:
        data = json.loads(payload) if payload else {}
        task_id = await _task_queue().enqueue(task_type, data)
        return {"task_id": task_id, "status": "queued", "type": task_type}
    except Exception as e:
        return JSONResponse({"error": f"Enqueue failed: {_redact_sensitive_text(e, locals().get("api_key"))}"}, status_code=500)


@app.get("/api/v1/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request):
    """Return current status and result of a background task."""
    if task_id.startswith("t-"):
        identity = getattr(request.state, "openmatrix_identity", {"id": "dev"})
        if _kg_task_owners.get(task_id) != str(identity.get("id") or "dev"):
            return JSONResponse({"error": "Task not found"}, status_code=404)
        result = default_task_manager().result(task_id)
        if result is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return result
    queue = _task_queue()
    status = await queue.status(task_id)
    if status == "unknown":
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return {"task_id": task_id, "status": status, "result": await queue.result(task_id)}


@app.delete("/api/v1/tasks/{task_id}")
async def cancel_task(task_id: str, request: Request):
    """Cancel a pending/queued background task."""
    if task_id.startswith("t-"):
        identity = getattr(request.state, "openmatrix_identity", {"id": "dev"})
        if _kg_task_owners.get(task_id) != str(identity.get("id") or "dev"):
            return JSONResponse({"error": "Task not found"}, status_code=404)
        cancelled = default_task_manager().cancel(task_id)
        if not cancelled:
            return JSONResponse(
                {"error": "Task not cancellable or not found"}, status_code=400
            )
        return {"task_id": task_id, "status": "cancelled"}
    cancelled = await _task_queue().cancel(task_id)
    if not cancelled:
        return JSONResponse({"error": "Task not cancellable or not found"}, status_code=400)
    return {"task_id": task_id, "status": "cancelled"}


# ---- P6-3: Document-library REST API (wraps library.default_library_store) ----
def _strict_int(value, default, *, min_value=None, max_value=None, name="value"):
    """Return a validated JSON integer, or None when invalid.

    ``default`` is returned only when ``value`` is the sentinel MISSING
    (key absent); explicit null / bool / float / string / container all fail.
    """
    if value is _MISSING:
        return default
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if min_value is not None and value < min_value:
        return None
    if max_value is not None and value > max_value:
        return None
    return value


_MISSING = object()


def _strict_bool(value, default):
    """Return a validated JSON boolean, or None when invalid."""
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        return None
    return value


def _strict_str(value, name="value"):
    """Return a non-empty stripped string, or None when invalid.

    Rejects bools, numbers, containers, and null; empty/whitespace fails.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def _require_dict_body(payload) -> dict | None:
    """Return the payload when it is a JSON object, else None (400)."""
    if isinstance(payload, dict):
        return payload
    return None


def _library_store():
    from library import default_library_store

    return default_library_store()


@app.get("/api/v1/projects")
async def api_projects_list():
    """List all document-library projects (P6-3)."""
    try:
        projects = await _library_store().list_projects()
        return {"projects": projects}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"list projects failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.post("/api/v1/projects")
async def api_projects_create(payload: Any = Body(None)):
    """Create a document-library project.

    JSON body: {"project_id": "...", "name": "...", "description": "..."}
    """
    body = _require_dict_body(payload)
    if body is None:
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    pid = _strict_str(body.get("project_id"), "project_id")
    name = _strict_str(body.get("name"), "name")
    if pid is None or name is None:
        return JSONResponse({"error": "project_id and name must be non-empty strings"}, status_code=400)
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        return JSONResponse({"error": "description must be a string"}, status_code=400)
    try:
        project = await _library_store().create_project(pid, name, description)
        return {"project": project}
    except ValueError as e:
        return JSONResponse({"error": _redact_sensitive_text(e, locals().get("api_key"))}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"create project failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.delete("/api/v1/projects/{project_id}")
async def api_projects_delete(project_id: str):
    """Delete a project and its documents (P6-3)."""
    try:
        removed = await _library_store().delete_project(project_id)
        if not removed:
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
        return {"deleted": project_id}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"delete project failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.get("/api/v1/projects/{project_id}/documents")
async def api_projects_documents(project_id: str):
    """List documents inside a project (P6-3). Unknown project -> 404."""
    store = _library_store()
    try:
        if await store.get_project(project_id) is None:
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
        docs = await store.list_documents(project_id)
        return {"documents": docs}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"list documents failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.post("/api/v1/projects/{project_id}/upload")
async def api_projects_upload(project_id: str, file: UploadFile = File(...), indexed: bool = Form(False)):
    """Upload PDF/DOCX/TXT into a project library."""
    temp_path = None
    try:
        store = _library_store()
        if await store.get_project(project_id) is None:
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
        filename, suffix = file.filename or "document", Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md", ".text"}:
            return JSONResponse({"error": f"unsupported file type {suffix!r}"}, status_code=400)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise HTTPException(status_code=400, detail="File exceeds maximum upload size")
                tmp.write(chunk)
        if suffix == ".pdf":
            pages = await asyncio.to_thread(get_parser_adapter(),temp_path)
            content, page_count = "\f".join(pages) + "\f", len(pages)
        elif suffix == ".docx":
            from docx import Document
            try:
                source = Document(temp_path)
            except Exception as exc:
                raise ValueError("Could not read DOCX document") from exc
            parts = [p.text.strip() for p in source.paragraphs if p.text and p.text.strip()]
            for table in source.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text and cell.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            content, page_count = "\n".join(parts), 0
        else:
            content, page_count = Path(temp_path).read_text(encoding="utf-8", errors="replace"), 0
        if not content.strip():
            return JSONResponse({"error": "no extractable text in file"}, status_code=400)
        doc = await store.add_document("doc_" + uuid.uuid4().hex, project_id, filename, content, page_count=page_count)
        if indexed:
            await store.update_document_status(doc["id"], "indexed")
            doc["status"] = "indexed"
        return {"document": doc}
    except HTTPException:
        raise
    except (ValueError, PdfReadError, PdfInputError) as exc:
        return JSONResponse({"error": _redact_sensitive_text(exc, locals().get("api_key"))}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"upload failed: {_redact_sensitive_text(exc)}"}, status_code=500)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        await file.close()


@app.post("/api/v1/projects/{project_id}/documents")
async def api_projects_documents_add(project_id: str, payload: Any = Body(None)):
    """Add pasted text or explicit physical pages to a project."""
    body = _require_dict_body(payload)
    if body is None:
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    doc_id, filename = _strict_str(body.get("doc_id")), _strict_str(body.get("filename"))
    pages = body.get("pages", _MISSING)
    if pages is not _MISSING:
        if not isinstance(pages, list) or not pages or not all(isinstance(p, str) for p in pages) or not any(p.strip() for p in pages):
            return JSONResponse({"error": "pages must be a non-empty list of text strings"}, status_code=400)
        content, default_count = "\f".join(p.strip() for p in pages) + "\f", len(pages)
    else:
        raw_content = body.get("content")
        content = raw_content if isinstance(raw_content,str) and raw_content.strip() else None
        chunks = content.split("\f") if content is not None and "\f" in content else []
        if chunks and chunks[-1] == "":
            chunks.pop()
        default_count = len(chunks)
    if doc_id is None or filename is None or content is None:
        return JSONResponse({"error": "doc_id, filename and content/pages are required"}, status_code=400)
    page_count = _strict_int(body.get("page_count", _MISSING), default_count, min_value=0)
    indexed = _strict_bool(body.get("indexed", _MISSING), False)
    if page_count is None or indexed is None:
        return JSONResponse({"error": "invalid page_count or indexed"}, status_code=400)
    store = _library_store()
    try:
        if await store.get_project(project_id) is None:
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
        doc = await store.add_document(doc_id, project_id, filename, content, page_count=page_count)
        if indexed:
            await store.update_document_status(doc_id, "indexed")
            doc["status"] = "indexed"
        return {"document": doc}
    except ValueError as exc:
        return JSONResponse({"error": _redact_sensitive_text(exc, locals().get("api_key"))}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"add document failed: {_redact_sensitive_text(exc)}"}, status_code=500)


@app.post("/api/v1/projects/{project_id}/evidence-snapshots")
async def api_projects_evidence_snapshot_add(
    project_id: str,
    payload: Any = Body(None),
    caller: dict | None = Depends(require_api_v1_auth_dependency),
):
    """Persist a user-authorized current-browser-page evidence snapshot.

    The server validates only the supplied snapshot. It never fetches the
    submitted URL, so browser collection cannot expand the SSRF surface. The
    URL is client-asserted metadata, not server-verified provenance.

    Projects do not currently carry an owner/membership model. Therefore
    authenticated deployments intentionally restrict this write operation to
    admin/master callers until project ownership is implemented and reviewed.
    """
    # Projects do not have an owner model yet. In auth-enabled deployments,
    # only an admin/master can attach browser evidence until ownership exists.
    if caller is not None and caller.get("role") not in {"admin", "master"}:
        return JSONResponse({"error": "admin role required for evidence capture"}, status_code=403)
    store = _library_store()
    try:
        if await store.get_project(project_id) is None:
            return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
        snapshot = build_browser_snapshot(payload)
        indexed = _strict_bool(
            payload["indexed"] if isinstance(payload, dict) and "indexed" in payload else _MISSING,
            False,
        )
        if indexed is None:
            return JSONResponse({"error": "indexed must be a boolean"}, status_code=400)
        doc_id = "ev_" + uuid.uuid4().hex
        doc = await store.add_document(
            doc_id,
            project_id,
            snapshot.title + ".txt",
            snapshot.content,
            page_count=len(snapshot.sections),
            provenance=snapshot.provenance,
        )
        if indexed:
            await store.update_document_status(doc_id, "indexed")
            doc["status"] = "indexed"
        return {
            "evidence_snapshot": {
                "id": doc_id,
                "project_id": project_id,
                "source_kind": snapshot.source_kind,
                "provenance_basis": "client_asserted",
                "title": snapshot.title,
                "source_url": snapshot.source_url,
                "canonical_url": snapshot.canonical_url,
                "captured_at": snapshot.captured_at,
                "content_sha256": snapshot.content_sha256,
                "section_count": len(snapshot.sections),
                "status": doc["status"],
            }
        }
    except ValueError as e:
        return JSONResponse({"error": _redact_sensitive_text(e, locals().get("api_key"))}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"add evidence snapshot failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.delete("/api/v1/documents/{doc_id}")
async def api_documents_delete(doc_id: str):
    """Delete a single document (P6-3)."""
    try:
        removed = await _library_store().delete_document(doc_id)
        if not removed:
            return JSONResponse({"error": f"document {doc_id!r} not found"}, status_code=404)
        return {"deleted": doc_id}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"delete document failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.post("/api/v1/projects/{project_id}/search")
async def api_projects_search(project_id: str, payload: Any = Body(None)):
    """Retrieve evidence from a project's indexed documents (P6-3).

    JSON body: {"question": "...", "limit": 3}
    Returns a flat list of hits (never a grid) per the patent guardrail.
    Unknown project -> 404; strict field validation -> 400.
    """
    body = _require_dict_body(payload)
    if body is None:
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    question = _strict_str(body.get("question"), "question")
    if question is None:
        return JSONResponse({"error": "question must be a non-empty string"}, status_code=400)
    limit = _strict_int(
        body["limit"] if "limit" in body else _MISSING,
        3, min_value=1, max_value=100, name="limit")
    if limit is None:
        return JSONResponse({"error": "limit must be an integer between 1 and 100"}, status_code=400)
    mode = body.get("mode", "bm25")
    if mode not in ("bm25", "dense", "fusion"):
        return JSONResponse({"error": "mode must be one of 'bm25', 'dense', 'fusion'"}, status_code=400)
    store = _library_store()
    if await store.get_project(project_id) is None:
        return JSONResponse({"error": f"project {project_id!r} not found"}, status_code=404)
    from mcp_server import _search_project

    try:
        hits = await _search_project(project_id, question, limit, mode=mode)
        return {"hits": hits}
    except ValueError as e:
        return JSONResponse({"error": _redact_sensitive_text(e, locals().get("api_key"))}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"search failed: {_redact_sensitive_text(e)}"}, status_code=500)


def _run_manager():
    """Return the shared in-process ResearchRunManager singleton (same as mcp_server)."""
    import importlib

    _rr_mod = importlib.import_module("agent.research_run")
    if _rr_mod._default_manager is None:  # noqa: SLF001
        _rr_mod._default_manager = _rr_mod.ResearchRunManager()
    return _rr_mod._default_manager


@app.post("/api/v1/research")
async def api_research_start(payload: Any = Body(None)):
    """Start a research run from the chat UI (P6-6).

    JSON body: {"research_id": "...", "objective": "...", "context": {...}?, "owner": "..."?}
    Returns the run's task handle. Flat report object - never a grid.
    """
    body = _require_dict_body(payload)
    if body is None:
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    research_id = _strict_str(body.get("research_id"), "research_id")
    objective = _strict_str(body.get("objective"), "objective")
    if research_id is None or objective is None:
        return JSONResponse({"error": "research_id and objective must be non-empty strings"}, status_code=400)
    if research_id.startswith("t-"):
        return JSONResponse({"error": "research_id must not use the reserved 't-' prefix"}, status_code=400)
    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        return JSONResponse({"error": "context must be an object"}, status_code=400)
    owner = body.get("owner")
    if owner is not None:
        owner = _strict_str(owner, "owner")
        if owner is None:
            return JSONResponse({"error": "owner must be a non-empty string"}, status_code=400)
    try:
        controller = _run_manager().start(
            research_id,
            objective,
            context,
            owner=owner if owner else None,
        )
        return {"task_id": research_id, "operation": "research_run",
                "phase": controller.status()["phase"]}
    except ValueError as e:
        return JSONResponse({"error": _redact_sensitive_text(e, locals().get("api_key"))}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"start research failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.get("/api/v1/research/{research_id}")
async def api_research_status(research_id: str, owner: str | None = None):
    """Get a research run's status snapshot (phase, steps) for the chat UI."""
    try:
        status = _run_manager().status(research_id, owner=owner)
        if status is None:
            return JSONResponse({"error": f"research run {research_id!r} not found"}, status_code=404)
        return status
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"research status failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.delete("/api/v1/research/{research_id}")
async def api_research_cancel(research_id: str, owner: str | None = None):
    """Cancel a running research run."""
    try:
        cancelled = _run_manager().cancel(research_id, owner=owner)
        if not cancelled:
            return JSONResponse({"error": f"research run {research_id!r} not cancellable or not found"}, status_code=404)
        return {"research_id": research_id, "cancelled": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"research cancel failed: {_redact_sensitive_text(e)}"}, status_code=500)


@app.get("/api/v1/research")
async def api_research_list(owner: str | None = None):
    """List research runs visible to the caller (optionally owner-scoped)."""
    try:
        runs = _run_manager().list(owner=owner)
        return {"research": runs}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"research list failed: {_redact_sensitive_text(e)}"}, status_code=500)



@app.post("/api/analyze")
async def analyze_document(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    base_url: str = Form(...),
    model_name: str = Form(...),
    questions: str = Form(...),
    max_concurrency: int = Form(3, ge=1, le=8),
):
    """Analyze a single PDF against a list of questions through the matrix runner.

    Routes through :func:`execute_matrix_run` so bounded AnyIO concurrency,
    per-cell isolation, persistence, and the SSE event stream share one
    production execution path.
    """
    try:
        question_list = json.loads(questions)
        # Validate that we received a bounded list of string questions.
        # Enforce type, count, and length limits to avoid unbounded model fan‑out.
        MAX_QUESTIONS = 20
        MAX_QUESTION_LENGTH = 500
        MAX_TOTAL_CHARS = 5000
        if not isinstance(question_list, list) or not all(isinstance(q, str) for q in question_list):
            return JSONResponse({"error": "`questions` must be a JSON array of strings"}, status_code=400)
        if len(question_list) == 0:
            return JSONResponse({"error": "No questions provided"}, status_code=400)
        if len(question_list) > MAX_QUESTIONS:
            return JSONResponse({"error": f"Too many questions (max {MAX_QUESTIONS})"}, status_code=400)
        total_chars = 0
        for q in question_list:
            if len(q) > MAX_QUESTION_LENGTH:
                return JSONResponse({"error": f"Question too long (max {MAX_QUESTION_LENGTH} characters)"}, status_code=400)
            total_chars += len(q)
        if total_chars > MAX_TOTAL_CHARS:
            return JSONResponse({"error": f"Total question characters exceed limit ({MAX_TOTAL_CHARS})"}, status_code=400)

        try:
            temp_pdf_path = await _stream_upload_to_temp(file, MAX_PDF_BYTES)
        except PdfTooLargeError as pdf_err:
            return JSONResponse({"error": pdf_input_error_message(pdf_err)}, status_code=400)

        try:

            print(f"Extracting text from {file.filename}...")
            try:
                ocr_enabled = os.environ.get("OPENMATRIX_OCR_PREPROCESS", "").strip().lower() in ("1", "true", "yes", "on")
                ocr_preprocessor = OcrPreprocessor(enabled=ocr_enabled) if ocr_enabled else None
                document_pages = extract_pages_from_pdf(
                    temp_pdf_path,
                    ocr_backend=True,
                    ocr_preprocessor=ocr_preprocessor,
                )
            except PdfReadError as pdf_err:
                return JSONResponse({"error": f"Invalid PDF: {str(pdf_err)}"}, status_code=400)
            except PdfInputError as pdf_err:
                return JSONResponse({"error": pdf_input_error_message(pdf_err)}, status_code=400)
            except Exception as parse_err:
                return JSONResponse(
                    {"error": f"PDF parse error: {str(parse_err) or type(parse_err).__name__}"},
                    status_code=400,
                )

            table_context_enabled = os.environ.get("OPENMATRIX_TABLE_CONTEXT", "").strip().lower() in ("1", "true", "yes", "on")
            if table_context_enabled:
                try:
                    table_rows = extract_tables_from_pdf(temp_pdf_path)
                    document_pages = enrich_pages_with_tables(document_pages, table_rows)
                except Exception:
                    pass

            full_document_text = "\n".join(document_pages)

            if not full_document_text.strip():
                return JSONResponse({"error": "Could not extract text from the PDF."}, status_code=400)

            print(f"Connecting to proxy at {base_url}...")
            model_name = ModelRouter(model_name).resolve(TASK_FAST)
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=model_request_headers(base_url),
                http_client=httpx.AsyncClient(trust_env=False, timeout=120.0) # Increased timeout for huge docs
            )
            retry_adapter = TenacityRetryAdapter()

            fallback_enabled = os.getenv("FALLBACK_TO_LOCAL", "false").lower() == "true"
            async def call_model(prompt: str) -> str:
                try:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                        temperature=0.1
                    )
                    return response.choices[0].message.content
                except Exception as primary_err:
                    if fallback_enabled:
                        fallback_base = os.getenv("LOCAL_OPENAI_BASE", "http://localhost:11434/v1")
                        fallback_model = os.getenv("LOCAL_MODEL", "llama")
                        print("Primary OpenAI call failed, falling back to local LLM at", fallback_base)
                        fallback_client = AsyncOpenAI(
                            api_key="dummy",
                            base_url=fallback_base,
                            http_client=httpx.AsyncClient(trust_env=False, timeout=120.0)
                        )
                        response = await fallback_client.chat.completions.create(
                            model=fallback_model,
                            messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                            temperature=0.1
                        )
                        return response.choices[0].message.content
                    else:
                        raise primary_err

            retried_call_model = retry_adapter.decorate(call_model)

            retrieval_fallback_used = []

            async def answer_cell(document: dict, question: str) -> dict:
                """Answer one question against one document with per-cell context."""
                # P2-4: controlled Map-Reduce for long documents (env-gated).
                mr_enabled = os.environ.get("OPENMATRIX_MAP_REDUCE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
                mr_min_pages = int(os.environ.get("OPENMATRIX_MAP_REDUCE_MIN_PAGES", "20"))
                if mr_enabled and len(document.get("pages") or []) >= mr_min_pages:
                    try:
                        from core.adapters.map_reduce_dispatcher import DocumentChunk, MapReduceDispatcher

                        # Preserve the original 1-based page number in metadata so
                        # citations resolve to the correct page (not the filtered index).
                        chunks = [
                            DocumentChunk(
                                chunk_id=f"p{index + 1}",
                                text=page_text,
                                metadata={"page": index + 1},
                            )
                            for index, page_text in enumerate(document["pages"])
                            if page_text and page_text.strip()
                        ]
                        if chunks:
                            mr = MapReduceDispatcher(retried_call_model, retried_call_model,
                                                     max_concurrency=max(1, max_concurrency))
                            result = await mr.run(chunks, question)
                            if result.status in ("completed", "needs_review") and result.answer:
                                # Ground the quote: it must be a verbatim substring of
                                # the originating chunk's text, otherwise fall back to
                                # that chunk's leading text so we never fabricate a
                                # citation. Prefer the chunk matched by evidence chunk_id.
                                source_by_id = {chunk.chunk_id: chunk.text for chunk in chunks}
                                quote = None
                                for ev in result.evidence:
                                    clue = ev.get("clue")
                                    cid = ev.get("chunk_id")
                                    if not (isinstance(clue, str) and clue.strip()):
                                        continue
                                    src = source_by_id.get(cid) if isinstance(cid, str) else None
                                    if src and clue.strip() in src:
                                        quote = clue.strip()[:800]
                                        break
                                    if src:
                                        quote = src.strip()[:800]
                                        break
                                if result.status == "needs_review":
                                    retrieval_fallback_used.append(question)
                                return {
                                    "question": question,
                                    "answer": result.answer,
                                    "quote": quote,
                                }
                    except Exception as mr_err:  # noqa: BLE001 - controlled fallback
                        print(f"Map-Reduce failed, falling back to retrieval: {mr_err}")

                document_context, fallback = await anyio.to_thread.run_sync(
                    select_retrieved_context, [question], document["pages"]
                )
                if fallback:
                    retrieval_fallback_used.append(question)

                prompt = f"""
                You are a world-class, evidence-first institutional financial research engine.
                I will provide you with a relevant excerpt of a document, followed by a question.

                 <DOCUMENT>
                {document_context}
                </DOCUMENT>

                <QUESTIONS>
                {json.dumps([question], ensure_ascii=False, indent=2)}
                </QUESTIONS>

                CRITICAL INSTRUCTION:
                You MUST output ONLY a valid JSON array. Do not include markdown formatting like ```json.
                Each object in the array must have exactly these keys:
                - "question": the exact question asked.
                - "answer": your detailed, factual answer extracted from the text.
                - "quote": the exact quote(s) from the document that proves your answer (for traceability).
                """

                print(f"Requesting answer for '{question}' from model: {model_name}...")
                result_text = await retried_call_model(prompt)
                result_text = result_text.strip()

                if result_text.startswith("```json"):
                    result_text = result_text.replace("```json", "", 1)
                if result_text.endswith("```"):
                    result_text = result_text[:-3]

                matrix_data = json.loads(result_text.strip())
                if not isinstance(matrix_data, list) or not matrix_data:
                    raise ValueError("Model response must be a non-empty JSON array.")
                row = matrix_data[0]
                if not isinstance(row, dict):
                    raise ValueError("Model response rows must be objects.")

                return {
                    "question": question,
                    "answer": row.get("answer"),
                    "quote": row.get("quote"),
                }

            document_name = file.filename or "document.pdf"
            print(f"Running bounded matrix execution for {len(question_list)} question(s)...")
            result = await execute_matrix_run(
                documents=[{"name": document_name, "pages": document_pages}],
                questions=question_list,
                answer_cell=answer_cell,
                max_concurrency=max_concurrency,
            )

            cells = result["cells"]
            failed_cells = [cell for cell in cells if cell.get("status") == "failed"]

            if failed_cells and len(failed_cells) == len(cells):
                return JSONResponse(
                    {"error": failed_cells[0].get("error") or "Provider error"},
                    status_code=500,
                )

            data = [
                {
                    "question": cell.get("question"),
                    "answer": cell.get("answer"),
                    "quote": cell.get("quote"),
                    "citation_status": cell.get("citation_status"),
                    "support_status": cell.get("support_status"),
                    "support_reason": cell.get("support_reason"),
                    "page": cell.get("page"),
                    "error": cell.get("error"),
                }
                for cell in cells
            ]

            status = "success"
            if any(cell.get("citation_status") == "not_found" for cell in cells):
                status = "needs_review"
            if retrieval_fallback_used or failed_cells:
                status = "needs_review"

            return {"status": status, "data": data, "run_id": result["run_id"]}

        except Exception as api_err:
            return JSONResponse({"error": f"API Error: {str(api_err)}"}, status_code=500)
        finally:
            os.remove(temp_pdf_path)

    except Exception as e:
        return JSONResponse({"error": f"Internal Server Error: {_redact_sensitive_text(e, locals().get("api_key"))}"}, status_code=500)


@app.post("/api/v1/analyze-chain", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.post("/api/analyze_chain")
async def analyze_chain(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Form(...),
    base_url: str = Form(...),
    model_name: str = Form(...),
    steps: str = Form(...),
    use_kg: bool = Form(False),
    index_kg: bool = Form(False),
):
    """Run follow-up column / prompt-chaining analysis over one PDF."""
    try:
        try:
            step_list = json.loads(steps)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON format for `steps`"}, status_code=400)
        if not isinstance(step_list, list) or not step_list:
            return JSONResponse({"error": "`steps` must be a non-empty JSON array"}, status_code=400)
        if len(step_list) > 20:
            return JSONResponse({"error": "Too many chain steps (max 20)"}, status_code=400)
        normalized_steps = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(step_list):
            if not isinstance(raw, dict):
                return JSONResponse({"error": "Each chain step must be an object"}, status_code=400)
            step_id = str(raw.get("id") or f"step_{index + 1}").strip()
            question = str(raw.get("question") or "").strip()
            depends_on = raw.get("depends_on", [])
            if not step_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id):
                return JSONResponse({"error": "Step id must be 1-64 chars: letters, digits, _ or -"}, status_code=400)
            if step_id in seen_ids:
                return JSONResponse({"error": f"Duplicate step id: {step_id}"}, status_code=400)
            if not question or len(question) > 500:
                return JSONResponse({"error": f"Invalid question for step {step_id}"}, status_code=400)
            if not isinstance(depends_on, list) or not all(isinstance(dep, str) for dep in depends_on):
                return JSONResponse({"error": f"depends_on for step {step_id} must be a string array"}, status_code=400)
            seen_ids.add(step_id)
            normalized_steps.append({"id": step_id, "question": question, "depends_on": depends_on})
        step_by_id = {step["id"]: step for step in normalized_steps}
        for step in normalized_steps:
            missing = [dep for dep in step["depends_on"] if dep not in step_by_id]
            if missing:
                return JSONResponse({"error": f"Step {step['id']} depends on unknown step(s): {', '.join(missing)}"}, status_code=400)
        ordered_steps = []
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(step_id: str):
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError("Cycle detected in chain steps")
            visiting.add(step_id)
            for dep in step_by_id[step_id]["depends_on"]:
                visit(dep)
            visiting.remove(step_id)
            visited.add(step_id)
            ordered_steps.append(step_by_id[step_id])
        try:
            for step in normalized_steps:
                visit(step["id"])
        except ValueError as exc:
            return JSONResponse({"error": _redact_sensitive_text(exc, locals().get("api_key"))}, status_code=400)
        temp_pdf_path = None
        http_client = None
        try:
            pdf_digest = hashlib.sha256()
            try:
                temp_pdf_path = await _stream_upload_to_temp(file, MAX_PDF_BYTES, digest=pdf_digest)
            except PdfTooLargeError as pdf_err:
                return JSONResponse({"error": pdf_input_error_message(pdf_err)}, status_code=400)
            try:
                document_pages = extract_pages_from_pdf(temp_pdf_path)
            except PdfReadError as pdf_err:
                return JSONResponse({"error": f"Invalid PDF: {str(pdf_err)}"}, status_code=400)
            except PdfInputError as pdf_err:
                return JSONResponse({"error": pdf_input_error_message(pdf_err)}, status_code=400)
            if not "\n".join(document_pages).strip():
                return JSONResponse({"error": "Could not extract text from the PDF."}, status_code=400)
            http_client = httpx.AsyncClient(trust_env=False, timeout=120.0)
            model_name = ModelRouter(model_name).resolve(TASK_CHAIN)
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client, default_headers=model_request_headers(base_url))
            retry_adapter = TenacityRetryAdapter()
            fallback_enabled = os.getenv("FALLBACK_TO_LOCAL", "false").lower() == "true"
            async def call_model(prompt: str) -> str:
                try:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                        temperature=0.1
                    )
                    return response.choices[0].message.content
                except Exception as primary_err:
                    if fallback_enabled:
                        fallback_base = os.getenv("LOCAL_OPENAI_BASE", "http://localhost:11434/v1")
                        fallback_model = os.getenv("LOCAL_MODEL", "llama")
                        print("Primary OpenAI call failed, falling back to local LLM at", fallback_base)
                        fallback_client = AsyncOpenAI(
                            api_key="dummy",
                            base_url=fallback_base,
                            http_client=httpx.AsyncClient(trust_env=False, timeout=120.0)
                        )
                        response = await fallback_client.chat.completions.create(
                            model=fallback_model,
                            messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                            temperature=0.1
                        )
                        return response.choices[0].message.content
                    else:
                        raise primary_err
            retried_call_model = retry_adapter.decorate(call_model)
            run_id = uuid.uuid4().hex
            answers_by_id: dict[str, dict] = {}
            cells = []
            document_name = file.filename or "document.pdf"
            identity = getattr(request.state, "openmatrix_identity", {"id": "dev"})
            tenant_id = str(identity.get("id") or "dev")
            corpus_id = _kg_corpus_id(tenant_id, pdf_digest.hexdigest())
            kg_index_task_id = None
            if index_kg:
                kg_index_task_id = _submit_kg_index_task(
                    tenant_id, corpus_id, document_name, document_pages
                )
            for step_index, step in enumerate(ordered_steps):
                document_context, _fallback = await anyio.to_thread.run_sync(select_retrieved_context, [step["question"]], document_pages)
                if use_kg:
                    kg_context = await _retrieve_kg_context(
                        corpus_id, document_name, step["question"]
                    )
                    if kg_context:
                        document_context += "\n\n" + kg_context
                prior_answers = [{"id": dep, "question": answers_by_id[dep].get("question"), "answer": answers_by_id[dep].get("answer")} for dep in step["depends_on"]]
                prompt = f"""
                You are analyzing one document through a chained diligence workflow.
                Use PRIOR_ANSWERS as context only when relevant, and answer the CURRENT_QUESTION.
                <DOCUMENT>{document_context}</DOCUMENT>
                <PRIOR_ANSWERS>{json.dumps(prior_answers, ensure_ascii=False, indent=2)}</PRIOR_ANSWERS>
                <CURRENT_QUESTION>{step["question"]}</CURRENT_QUESTION>
                Output ONLY one JSON object with keys: "answer", "quote".
                """
                result_text = (await retried_call_model(prompt)).strip()
                fence = chr(96) * 3
                if result_text.startswith(fence + "json"):
                    result_text = result_text.replace(fence + "json", "", 1)
                if result_text.endswith(fence):
                    result_text = result_text[:-3]
                try:
                    parsed = json.loads(result_text.strip())
                except ValueError:
                    parsed = {}
                if isinstance(parsed, list):
                    parsed = parsed[0] if parsed else {}
                if not isinstance(parsed, dict):
                    parsed = {}
                verified_list = await anyio.to_thread.run_sync(
                    verify_matrix_citations,
                    [{"question": step["question"], "answer": parsed.get("answer"), "quote": parsed.get("quote")}],
                    document_pages,
                )
                verified = verified_list[0]
                status = "completed"
                if verified.get("citation_status") == "not_applicable":
                    status = "not_disclosed"
                elif verified.get("citation_status") == "not_found":
                    status = "needs_review"
                cell = {"cell_id": step["id"], "step_id": step["id"], "depends_on": step["depends_on"], "document_index": 0, "document_name": document_name, "question_index": step_index, "question": step["question"], "status": status, "answer": verified.get("answer"), "quote": verified.get("quote"), "citation_status": verified.get("citation_status"), "page": verified.get("page"), "error": None}
                persist_matrix_cell(run_id, cell)
                answers_by_id[step["id"]] = cell
                cells.append(cell)
            overall_status = "needs_review" if any(cell.get("status") == "needs_review" for cell in cells) else "success"
            return {
                "status": overall_status,
                "run_id": run_id,
                "data": cells,
                "steps_count": len(cells),
                "kg_index_task_id": kg_index_task_id,
            }
        finally:
            if http_client is not None:
                await http_client.aclose()
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                os.remove(temp_pdf_path)
    except Exception as e:
        return JSONResponse({"error": f"Chain Error: {_redact_sensitive_text(e, locals().get("api_key"))}"}, status_code=500)

@app.post("/api/v1/analyze-batch", dependencies=[Depends(require_api_v1_auth_dependency)])
@app.post("/api/analyze_batch")
@app.post("/api/analyze-batch")
async def analyze_documents_batch(
    files: list[UploadFile] = File(...),
    api_key: str = Form(...),
    base_url: str = Form(...),
    model_name: str = Form(...),
    questions: str = Form(...),
    max_concurrency: int = Form(3, ge=1, le=8),
):
    """Analyze multiple PDFs as document cards against a list of questions.

    Supports up to MAX_BATCH_FILES PDF documents per batch call (default 50,
    env OPENMATRIX_MAX_BATCH_FILES). The model is called once per
    document for all questions, then each returned answer is citation-verified and
    persisted for report/export compatibility.
    """
    MAX_BATCH_FILES = globals()["MAX_BATCH_FILES"]
    if not files or len(files) == 0:
        return JSONResponse({"error": "No files uploaded"}, status_code=400)
    if len(files) > MAX_BATCH_FILES:
        return JSONResponse({"error": f"Too many files in batch (max {MAX_BATCH_FILES})"}, status_code=400)

    try:
        question_list = json.loads(questions)
        MAX_QUESTIONS = 20
        MAX_QUESTION_LENGTH = 500
        MAX_TOTAL_CHARS = 5000
        if not isinstance(question_list, list) or not all(isinstance(q, str) for q in question_list):
            return JSONResponse({"error": "`questions` must be a JSON array of strings"}, status_code=400)
        if len(question_list) == 0:
            return JSONResponse({"error": "No questions provided"}, status_code=400)
        if len(question_list) > MAX_QUESTIONS:
            return JSONResponse({"error": f"Too many questions (max {MAX_QUESTIONS})"}, status_code=400)
        total_chars = sum(len(q) for q in question_list)
        if total_chars > MAX_TOTAL_CHARS:
            return JSONResponse({"error": f"Total question characters exceed limit ({MAX_TOTAL_CHARS})"}, status_code=400)

        temp_paths = []
        documents = []
        try:
            for file in files:
                try:
                    temp_path = await _stream_upload_to_temp(file, MAX_PDF_BYTES)
                except PdfTooLargeError:
                    return JSONResponse({"error": f"File {file.filename} exceeds size limit"}, status_code=400)
                temp_paths.append(temp_path)

                parser = get_parser_adapter()
                doc_pages = parser(temp_path)
                if not any(p.strip() for p in doc_pages):
                    return JSONResponse({"error": f"Could not extract text from {file.filename}"}, status_code=400)
                documents.append({"name": file.filename or "doc.pdf", "pages": doc_pages})

            model_name = ModelRouter(model_name).resolve(TASK_BATCH)
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=model_request_headers(base_url),
                http_client=httpx.AsyncClient(trust_env=False, timeout=120.0),
            )
            retry_adapter = TenacityRetryAdapter()

            fallback_enabled = os.getenv("FALLBACK_TO_LOCAL", "false").lower() == "true"
            async def call_model(prompt: str) -> str:
                try:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                        temperature=0.1
                    )
                    return response.choices[0].message.content
                except Exception as primary_err:
                    if fallback_enabled:
                        fallback_base = os.getenv("LOCAL_OPENAI_BASE", "http://localhost:11434/v1")
                        fallback_model = os.getenv("LOCAL_MODEL", "llama")
                        print("Primary OpenAI call failed, falling back to local LLM at", fallback_base)
                        fallback_client = AsyncOpenAI(
                            api_key="dummy",
                            base_url=fallback_base,
                            http_client=httpx.AsyncClient(trust_env=False, timeout=120.0)
                        )
                        response = await fallback_client.chat.completions.create(
                            model=fallback_model,
                            messages=[{"role": "system", "content": "You are a JSON-only data extraction engine."}, {"role": "user", "content": prompt}],
                            temperature=0.1
                        )
                        return response.choices[0].message.content
                    else:
                        raise primary_err

            retried_call_model = retry_adapter.decorate(call_model)
            retrieval_fallback_used = []
            run_id = uuid.uuid4().hex
            # Copy complete sources before finally removes upload temp files.
            for document_index, temp_path in enumerate(temp_paths):
                _save_run_source(temp_path, run_id, document_index)

            from anyio import CapacityLimiter

            limiter = CapacityLimiter(max(1, max_concurrency))

            async def analyze_document_once(document_index: int, document: dict) -> list[dict]:
                document_context, fallback = await anyio.to_thread.run_sync(
                    select_batch_context, question_list, document["pages"]
                )
                if fallback:
                    retrieval_fallback_used.append(document["name"])

                prompt = f"""
                You are a world-class financial analyst engine.
                I will provide relevant excerpts of ONE document, followed by a list of questions.
                Answer ALL questions for this document in one JSON array.

                <DOCUMENT>
                {document_context}
                </DOCUMENT>

                <QUESTIONS>
                {json.dumps(question_list, ensure_ascii=False, indent=2)}
                </QUESTIONS>

                CRITICAL INSTRUCTION:
                Output ONLY a valid JSON array. Each item must have keys: "question", "answer", "quote".
                """
                async with limiter:
                    result_text = await retried_call_model(prompt)
                result_text = result_text.strip()
                if result_text.startswith(chr(96) * 3 + "json"):
                    result_text = result_text.replace(chr(96) * 3 + "json", "", 1)
                if result_text.endswith(chr(96) * 3):
                    result_text = result_text[:-3]

                parsed = json.loads(result_text.strip())
                rows = parsed if isinstance(parsed, list) else []
                by_question = {str(row.get("question", "")): row for row in rows if isinstance(row, dict)}
                cells = []
                for question_index, question in enumerate(question_list):
                    row = by_question.get(question)
                    if row is None and question_index < len(rows) and isinstance(rows[question_index], dict):
                        row = rows[question_index]
                    row = row or {"answer": None, "quote": None}
                    verified = verify_matrix_citations([{
                        "question": question,
                        "answer": row.get("answer"),
                        "quote": row.get("quote"),
                    }], document["pages"])[0]
                    status = "completed"
                    if verified.get("citation_status") == "not_applicable":
                        status = "not_disclosed"
                    elif verified.get("citation_status") == "not_found":
                        status = "needs_review"
                    cell = {
                        "cell_id": f"d{document_index}q{question_index}",
                        "document_index": document_index,
                        "document_name": document["name"],
                        "question_index": question_index,
                        "question": question,
                        "status": status,
                        "answer": verified.get("answer"),
                        "quote": verified.get("quote"),
                        "citation_status": verified.get("citation_status"),
                        "support_status": verified.get("support_status"),
                        "support_reason": verified.get("support_reason"),
                        "page": verified.get("page"),
                        "error": None,
                    }
                    persist_matrix_cell(run_id, cell)
                    cells.append(cell)
                return cells

            nested_cells = await asyncio.gather(*[
                analyze_document_once(document_index, document)
                for document_index, document in enumerate(documents)
            ])
            cells = [cell for document_cells in nested_cells for cell in document_cells]
            result = {"run_id": run_id, "status": "completed", "cells": cells}

            data = [
                {
                    "document_name": cell.get("document_name"),
                    "document_index": cell.get("document_index"),
                    "question": cell.get("question"),
                    "answer": cell.get("answer"),
                    "quote": cell.get("quote"),
                    "citation_status": cell.get("citation_status"),
                    "support_status": cell.get("support_status"),
                    "support_reason": cell.get("support_reason"),
                    "page": cell.get("page"),
                    "error": cell.get("error"),
                }
                for cell in cells
            ]
            status = "success"
            if any(cell.get("citation_status") == "not_found" for cell in cells) or retrieval_fallback_used:
                status = "needs_review"

            return {"status": status, "data": data, "run_id": result["run_id"], "documents_count": len(documents)}

        finally:
            for p in temp_paths:
                if os.path.exists(p):
                    os.remove(p)

    except Exception as e:
        return JSONResponse({"error": f"Batch Error: {_redact_sensitive_text(e, locals().get("api_key"))}"}, status_code=500)

from core.xeno_matrix_pipeline import XenoMatrixDispatcher
from pydantic import BaseModel, ConfigDict, StrictStr

class MatrixRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr
    index: StrictStr = "default_matrix"
    api_key: StrictStr | None = None
    base_url: StrictStr | None = None
    model_name: StrictStr | None = None


@app.get("/docs/MATRIX-DEEP-DIVE-SETUP.md", include_in_schema=False)
def matrix_setup_doc():
    path = Path(__file__).resolve().parent / "docs" / "MATRIX-DEEP-DIVE-SETUP.md"
    if not path.is_file():
        return JSONResponse({"error": "setup guide unavailable"}, status_code=404)
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")


@app.get("/api/matrix/status")
async def matrix_status():
    xlat_path = os.getenv("OPENMATRIX_XLAT_PATH", "").strip()
    semantic_index = os.getenv("OPENMATRIX_SEMANTIC_INDEX", ".openmatrix/semantic-index")
    server_llm_configured = bool(
        (os.getenv("OPENMATRIX_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
        and (os.getenv("OPENMATRIX_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
    )
    return {
        "ready": bool(xlat_path and os.path.isfile(xlat_path)),
        "xlat_configured": bool(xlat_path and os.path.isfile(xlat_path)),
        "server_llm_configured": server_llm_configured,
        "semantic_index": semantic_index,
    }

@app.post("/api/matrix/run")
async def run_matrix_pipeline(req: MatrixRunRequest, request: Request):
    """
    OpenMatrix High-Density Latent Swarm (Matrix Mode)
    """
    query = req.query.strip()
    index_name = req.index.strip()
    if not query:
        return JSONResponse(status_code=400, content={"error": "query must be a non-empty string"})
    if not index_name:
        return JSONResponse(status_code=400, content={"error": "index must be a non-empty string"})

    api_key = req.api_key or os.getenv("OPENMATRIX_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = req.base_url or os.getenv("OPENMATRIX_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    model_name = req.model_name or os.getenv("OPENMATRIX_MATRIX_MODEL", "gpt-4o-mini")
    if not api_key or not base_url:
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    "Matrix mode requires OPENMATRIX_LLM_API_KEY and "
                    "OPENMATRIX_LLM_BASE_URL (or the OPENAI_* equivalents)."
                )
            },
        )
    if not base_url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "base_url must use http or https"})
    if not model_name.strip():
        return JSONResponse(status_code=400, content={"error": "model_name must be a non-empty string"})

    http_client = httpx.AsyncClient(trust_env=False, timeout=120.0)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=model_request_headers(base_url),
        http_client=http_client,
    )
    dispatcher = XenoMatrixDispatcher(openai_client=client, model_name=model_name)

    try:
        result = await dispatcher.run_pipeline(index_name, query, top_k=3)
        if result.get("status") == "error":
            return JSONResponse(status_code=422, content={"error": result.get("message", "Matrix run failed")})
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": _redact_sensitive_text(e, locals().get("api_key"))})
    finally:
        await http_client.aclose()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--eval-retrieval":
        raise SystemExit(_run_retrieval_eval_cli())
    import uvicorn
    _install_sensitive_log_filter()
    port = int(os.getenv("OPENMATRIX_PORT", "8000"))
    print(f"Starting OpenMatrix server on http://localhost:{port}/ui/")
    uvicorn.run(app, host=os.getenv("OPENMATRIX_HOST", "127.0.0.1"), port=port)

