"""Optional knowledge-graph retrieval adapter for ISD-KG1.

The adapter has no effect unless ``OPENMATRIX_KG_ENABLED`` is set to an
exact supported value. Missing packages and engine failures degrade to an
unavailable adapter without affecting the existing retrieval path.
"""

from __future__ import annotations

import logging
import os
import time
import base64
import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    success: bool
    doc_id: str | None = None
    chunks_indexed: int | None = None
    entities_indexed: int | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class SectionDocument:
    """Clean, citation-addressable text unit submitted to the KG batch."""

    section_id: str
    text: str
    source_id: str | None = None


@dataclass(frozen=True)
class GraphHit:
    """Graph evidence mapped back to OpenMatrix citation fields."""

    doc_id: str
    chunk_id: str | None
    page: int | None
    text: str
    score: float = 0.0


class KnowledgeGraphAdapter(Protocol):
    def is_available(self) -> bool: ...

    async def index_document(self, doc_id: str, text: str) -> IndexResult: ...

    async def index_sections(
        self, doc_id: str, sections: Iterable[SectionDocument]
    ) -> IndexResult: ...

    async def retrieve(self, question: str, mode: str = "mix") -> list[GraphHit]: ...


class _ModeParam:
    def __init__(self, mode: str) -> None:
        self.mode = mode


class LightRagKnowledgeGraphAdapter:
    """Narrow adapter around an engine exposing ``ainsert`` and ``aquery``."""

    def __init__(
        self,
        working_dir: str = "./kg_storage",
        engine: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._engine = engine
        self._degraded_error: str | None = None
        self._storages_initialized = False
        self._usage = {
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "embedding_tokens": 0,
            "llm_calls": 0,
            "embedding_calls": 0,
            "usage_missing_calls": 0,
        }

        if not enabled or engine is not None:
            return

        try:
            from lightrag import LightRAG

            self._engine = self._build_engine(LightRAG, working_dir)
        except Exception as exc:
            self._degrade(f"KG engine initialization failed: {exc}")

    def _build_engine(self, light_rag_cls: Any, working_dir: str) -> Any:
        api_key = os.getenv("OPENMATRIX_KG_API_KEY")
        if not api_key:
            raise RuntimeError("OPENMATRIX_KG_API_KEY is not configured")
        import httpx
        import numpy as np
        from lightrag.utils import EmbeddingFunc
        from openai import AsyncOpenAI

        base_url = os.getenv(
            "OPENMATRIX_KG_BASE_URL", "https://integrate.api.nvidia.com/v1"
        )
        llm_model = os.getenv("OPENMATRIX_KG_LLM_MODEL", "openai/gpt-oss-120b")
        embed_model = os.getenv(
            "OPENMATRIX_KG_EMBED_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2"
        )
        embed_dim = int(os.getenv("OPENMATRIX_KG_EMBED_DIM", "2048"))

        def create_client() -> AsyncOpenAI:
            return AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=httpx.AsyncClient(trust_env=False, timeout=180.0),
            )

        async def llm_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, str]] | None = None,
            **kwargs: Any,
        ) -> str:
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.extend(history_messages or [])
            messages.append({"role": "user", "content": prompt})
            allowed = {"temperature", "top_p", "max_tokens", "response_format"}
            request_kwargs = {key: value for key, value in kwargs.items() if key in allowed}
            async with create_client() as client:
                response = await client.chat.completions.create(
                    model=llm_model, messages=messages, **request_kwargs
                )
            usage = response.usage
            if usage is None:
                self._usage["usage_missing_calls"] += 1
            else:
                self._usage["llm_prompt_tokens"] += usage.prompt_tokens or 0
                self._usage["llm_completion_tokens"] += usage.completion_tokens or 0
            self._usage["llm_calls"] += 1
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise RuntimeError("KG LLM response was truncated")
            content = choice.message.content
            if not content or not content.strip():
                raise RuntimeError("KG LLM returned empty content")
            return content

        async def embed_func(
            texts: list[str], context: str = "document"
        ) -> np.ndarray:
            input_type = "query" if context == "query" else "passage"
            async with create_client() as client:
                response = await client.embeddings.create(
                    model=embed_model,
                    input=texts,
                    encoding_format="float",
                    extra_body={"input_type": input_type, "truncate": "END"},
                )
            usage = response.usage
            if usage is None:
                self._usage["usage_missing_calls"] += 1
            else:
                self._usage["embedding_tokens"] += usage.prompt_tokens or 0
            self._usage["embedding_calls"] += 1
            vectors = np.array([item.embedding for item in response.data])
            if vectors.ndim != 2 or vectors.shape[1] != embed_dim:
                raise RuntimeError(
                    f"KG embedding dimension mismatch: expected {embed_dim}, got {vectors.shape}"
                )
            return vectors

        return light_rag_cls(
            working_dir=working_dir,
            llm_model_func=llm_func,
            llm_model_name=llm_model,
            embedding_func=EmbeddingFunc(
                embedding_dim=embed_dim,
                max_token_size=8192,
                model_name=embed_model,
                supports_asymmetric=True,
                func=embed_func,
            ),
        )

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self._usage)

    def _degrade(self, error: str) -> None:
        self._degraded_error = error
        self._engine = None
        _logger.warning(error)

    def is_available(self) -> bool:
        return self._enabled and self._engine is not None and self._degraded_error is None

    async def _ensure_initialized(self) -> None:
        if self._storages_initialized or self._engine is None:
            return
        initializer = getattr(self._engine, "initialize_storages", None)
        if initializer is not None:
            await initializer()
        self._storages_initialized = True

    async def index_document(self, doc_id: str, text: str) -> IndexResult:
        if not isinstance(text, str) or not text.strip():
            return IndexResult(success=False, doc_id=doc_id, error="text must be a non-empty string")
        start = time.perf_counter()
        if not self.is_available():
            return IndexResult(success=False, doc_id=doc_id, error=self._degraded_error or "KG adapter unavailable")
        try:
            await self._ensure_initialized()
            result = await self._engine.ainsert(text, ids=[doc_id], file_paths=[doc_id])
            status_reader = getattr(self._engine, "aget_docs_by_ids", None)
            if status_reader is not None:
                statuses = await status_reader(doc_id)
                status_obj = statuses.get(doc_id) if isinstance(statuses, dict) else None
                status_value = status_obj.get("status") if isinstance(status_obj, dict) else getattr(status_obj, "status", None)
                if getattr(status_value, "value", status_value) != "processed":
                    raise RuntimeError("KG engine did not confirm document processing")
        except Exception as exc:
            error = f"KG indexing failed: {exc}"
            self._degrade(error)
            return IndexResult(success=False, doc_id=doc_id, error=error, elapsed_seconds=time.perf_counter() - start)
        return IndexResult(
            success=True,
            doc_id=doc_id,
            chunks_indexed=self._count_result(result, "chunks_indexed", "chunks"),
            entities_indexed=self._count_result(result, "entities_indexed", "entities"),
            elapsed_seconds=time.perf_counter() - start,
        )

    async def index_sections(
        self, doc_id: str, sections: Iterable[SectionDocument]
    ) -> IndexResult:
        start = time.perf_counter()
        if not self.is_available():
            return IndexResult(
                success=False,
                error=self._degraded_error or "KG adapter unavailable",
                elapsed_seconds=time.perf_counter() - start,
            )
        if not isinstance(doc_id, str) or not doc_id.strip():
            return IndexResult(
                success=False,
                error="doc_id must be a non-empty string",
                elapsed_seconds=time.perf_counter() - start,
            )
        if sections is None:
            return IndexResult(
                success=False,
                doc_id=doc_id,
                error="sections must not be None",
                elapsed_seconds=time.perf_counter() - start,
            )

        try:
            section_list = list(sections)
        except TypeError:
            return IndexResult(
                success=False,
                doc_id=doc_id,
                error="sections must be iterable",
                elapsed_seconds=time.perf_counter() - start,
            )
        if not section_list:
            return IndexResult(
                success=False,
                doc_id=doc_id,
                error="sections must contain at least one item",
                elapsed_seconds=time.perf_counter() - start,
            )

        texts: list[str] = []
        ids: list[str] = []
        file_paths: list[str] = []
        seen_ids: set[str] = set()
        for section in section_list:
            if not isinstance(section, SectionDocument):
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error="sections must contain SectionDocument values",
                    elapsed_seconds=time.perf_counter() - start,
                )
            if not isinstance(section.section_id, str) or not section.section_id.strip():
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error="section_id must be a non-empty string",
                    elapsed_seconds=time.perf_counter() - start,
                )
            if not isinstance(section.text, str) or not section.text.strip():
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error="section text must be a non-empty string",
                    elapsed_seconds=time.perf_counter() - start,
                )
            if section.source_id is not None and (
                not isinstance(section.source_id, str) or not section.source_id.strip()
            ):
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error="source_id must be a non-empty string when provided",
                    elapsed_seconds=time.perf_counter() - start,
                )
            lowered_text = section.text.lower()
            structural_tags = ("html", "body", "div", "main", "article", "section")
            has_structural_dom = any(
                re.search(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", lowered_text, re.DOTALL)
                for tag in structural_tags
            )
            has_executable_dom = any(
                re.search(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", lowered_text, re.DOTALL)
                for tag in ("script", "style", "noscript", "template")
            )
            if has_structural_dom or has_executable_dom:
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error="section text must be cleaned plain text, not raw DOM",
                    elapsed_seconds=time.perf_counter() - start,
                )
            section_key = f"{doc_id}:{section.section_id.strip()}"
            if section_key in seen_ids:
                return IndexResult(
                    success=False,
                    doc_id=doc_id,
                    error=f"duplicate section_id: {section.section_id}",
                    elapsed_seconds=time.perf_counter() - start,
                )
            seen_ids.add(section_key)
            texts.append(section.text.strip())
            ids.append(section_key)
            file_paths.append((section.source_id or section_key).strip())

        try:
            await self._ensure_initialized()
            result = await self._engine.ainsert(texts, ids=ids, file_paths=file_paths)
            status_reader = getattr(self._engine, "aget_docs_by_ids", None)
            if status_reader is not None:
                statuses = await status_reader(ids)
                for section_id in ids:
                    status_obj = statuses.get(section_id) if isinstance(statuses, dict) else None
                    if isinstance(status_obj, dict):
                        status_value = status_obj.get("status")
                    else:
                        status_value = getattr(status_obj, "status", None)
                    if getattr(status_value, "value", status_value) != "processed":
                        raise RuntimeError(f"KG engine did not confirm processing for {section_id}")
        except Exception as exc:
            error = f"KG indexing failed: {exc}"
            self._degrade(error)
            return IndexResult(
                success=False,
                doc_id=doc_id,
                error=error,
                elapsed_seconds=time.perf_counter() - start,
            )

        return IndexResult(
            success=True,
            doc_id=doc_id,
            chunks_indexed=self._count_result(result, "chunks_indexed", "chunks"),
            entities_indexed=self._count_result(result, "entities_indexed", "entities"),
            elapsed_seconds=time.perf_counter() - start,
        )

    async def retrieve(self, question: str, mode: str = "mix") -> list[GraphHit]:
        if not self.is_available() or not question:
            return []
        try:
            await self._ensure_initialized()
            try:
                from lightrag import QueryParam

                param = QueryParam(mode=mode)
            except Exception:
                param = _ModeParam(mode)
            query_data = getattr(self._engine, "aquery_data", None)
            if query_data is None:
                raise RuntimeError("KG engine does not expose structured aquery_data")
            result = await query_data(question, param=param)
            return self._map_hits(result)
        except Exception as exc:
            self._degrade(f"KG retrieval failed: {exc}")
            return []

    @staticmethod
    def _count_result(result: Any, field: str, collection: str) -> int | None:
        if isinstance(result, dict):
            value = result.get(field)
            if type(value) is int and value >= 0:
                return value
            items = result.get(collection)
            if isinstance(items, (list, tuple, set)):
                return len(items)
        value = getattr(result, field, None)
        if type(value) is int and value >= 0:
            return value
        items = getattr(result, collection, None)
        if isinstance(items, (list, tuple, set)):
            return len(items)
        return None

    @classmethod
    def _map_hits(cls, result: Any) -> list[GraphHit]:
        if not isinstance(result, dict):
            raise ValueError("KG query data must be a mapping")
        if result.get("status") not in (None, "success"):
            raise ValueError(f"KG query failed: {result.get('message', 'unknown error')}")
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise ValueError("KG query data payload must be a mapping")
        chunks = data.get("chunks", [])
        if not isinstance(chunks, list):
            raise ValueError("KG query chunks must be a list")
        hits: list[GraphHit] = []
        for index, chunk in enumerate(chunks or []):
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("content") or chunk.get("text") or "").strip()
            source_path = str(
                chunk.get("doc_id") or chunk.get("file_path") or ""
            ).strip()
            doc_id, source_page = cls._parse_source_path(source_path)
            if not text or not doc_id:
                continue
            page = cls._optional_non_negative_int(chunk.get("page"))
            if page is None:
                page = source_page
            chunk_id_value = chunk.get(
                "chunk_id", chunk.get("reference_id", chunk.get("id", index))
            )
            score_value = chunk.get("score", 0.0)
            score = float(score_value) if isinstance(score_value, (int, float)) else 0.0
            hits.append(
                GraphHit(
                    doc_id=doc_id,
                    chunk_id=str(chunk_id_value) if chunk_id_value is not None else None,
                    page=page,
                    text=text,
                    score=score,
                )
            )
        if chunks and not hits:
            raise ValueError("KG query chunks lack citation metadata")
        return hits

    @staticmethod
    def page_source_id(doc_id: str, page: int) -> str:
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("doc_id must be a non-empty string")
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        encoded = base64.urlsafe_b64encode(doc_id.encode("utf-8")).decode("ascii").rstrip("=")
        return f"openmatrix-page-{page}-{encoded}.pdf"

    @staticmethod
    def _parse_source_path(source_path: str) -> tuple[str, int | None]:
        match = re.search(
            r"openmatrix-page-([1-9][0-9]*)-([A-Za-z0-9_-]+)\.pdf$",
            source_path,
        )
        if match is None:
            return source_path, None
        encoded = match.group(2)
        padding = "=" * (-len(encoded) % 4)
        try:
            doc_id = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return source_path, None
        return doc_id, int(match.group(1))

    @staticmethod
    def _optional_non_negative_int(value: Any) -> int | None:
        if type(value) is int and value >= 0:
            return value
        return None


def create_kg_adapter(working_dir: str = "./kg_storage") -> KnowledgeGraphAdapter:
    flag = os.getenv("OPENMATRIX_KG_ENABLED", "").lower()
    enabled = flag in {"1", "true", "yes", "on"}
    return LightRagKnowledgeGraphAdapter(working_dir=working_dir, enabled=enabled)
