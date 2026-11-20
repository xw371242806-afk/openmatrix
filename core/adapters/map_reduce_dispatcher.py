"""Lightweight heterogeneous Map-Reduce dispatcher skeleton.

This module intentionally uses only stdlib asyncio/dataclasses. It does not
introduce a vector database, LangChain, LlamaIndex, or a production queue.
The dispatcher consumes already-parsed document chunks and returns a result
shape that can be mapped onto the existing CellRun/CellResult status contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, Sequence


@dataclass(frozen=True)
class DocumentChunk:
    """Parsed document chunk supplied by the existing parser/retrieval layer."""

    chunk_id: str
    text: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MapClue:
    """High-value clue emitted by the cheap Map model."""

    chunk_id: str
    clue: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MapReduceResult:
    """CellRun-compatible dispatcher result.

    status maps to the current CellRun answer_status vocabulary:
    completed | not_disclosed | needs_review | failed.
    """

    status: str
    answer: str | None
    evidence: list[dict[str, object]] = field(default_factory=list)
    error: str | None = None
    cell_run_id: str | None = None


class PromptModel(Protocol):
    """Minimal async model contract used by this skeleton."""

    def __call__(self, prompt: str) -> Awaitable[str]:
        ...


class MapReduceDispatcher:
    """Business-level text scheduler: cheap Map filtering + smart Reduce synthesis."""

    def __init__(
        self,
        cheap_model: PromptModel | Callable[[str], Awaitable[str]],
        smart_model: PromptModel | Callable[[str], Awaitable[str]],
        *,
        max_concurrency: int = 5,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._cheap_model = cheap_model
        self._smart_model = smart_model
        self._max_concurrency = max_concurrency

    async def run(
        self,
        document_chunks: Sequence[DocumentChunk],
        cell_prompt: str,
        *,
        cell_run_id: str | None = None,
    ) -> MapReduceResult:
        """Run Map-Reduce over chunks for one logical CellRun.

        Map phase asks the cheap model to return only prompt-relevant clues or
        NULL. Reduce phase sends compact clues plus chunk ids to the smart model.
        """

        if not cell_prompt.strip():
            return MapReduceResult(status="failed", answer=None, error="cell_prompt is required", cell_run_id=cell_run_id)
        chunks = [chunk for chunk in document_chunks if chunk.text.strip()]
        if not chunks:
            return MapReduceResult(status="not_disclosed", answer=None, evidence=[], cell_run_id=cell_run_id)

        semaphore = asyncio.Semaphore(self._max_concurrency)
        map_outputs = await asyncio.gather(
            *(self._map_one(chunk, cell_prompt, semaphore) for chunk in chunks),
            return_exceptions=True,
        )

        clues: list[MapClue] = []
        map_errors: list[str] = []
        for chunk, output in zip(chunks, map_outputs):
            if isinstance(output, asyncio.CancelledError):
                raise output
            if isinstance(output, Exception):
                map_errors.append(f"{chunk.chunk_id}: {output}")
            elif output is not None:
                clues.append(output)

        if not clues:
            if len(map_errors) == len(chunks):
                return MapReduceResult(
                    status="failed",
                    answer=None,
                    error="all map calls failed: " + "; ".join(map_errors),
                    cell_run_id=cell_run_id,
                )
            if map_errors:
                return MapReduceResult(
                    status="needs_review",
                    answer=None,
                    evidence=[],
                    error="map failed for chunk(s): " + "; ".join(map_errors),
                    cell_run_id=cell_run_id,
                )
            return MapReduceResult(status="not_disclosed", answer=None, evidence=[], cell_run_id=cell_run_id)

        reduce_prompt = self._build_reduce_prompt(cell_prompt, clues)
        try:
            answer = (await self._smart_model(reduce_prompt)).strip()
        except Exception as exc:  # pragma: no cover - defensive integration boundary
            return MapReduceResult(status="failed", answer=None, evidence=self._evidence(clues), error=str(exc), cell_run_id=cell_run_id)

        if not answer:
            return MapReduceResult(status="needs_review", answer=None, evidence=self._evidence(clues), error="reduce returned empty answer", cell_run_id=cell_run_id)
        if map_errors:
            return MapReduceResult(
                status="needs_review",
                answer=answer,
                evidence=self._evidence(clues),
                error="map failed for chunk(s): " + "; ".join(map_errors),
                cell_run_id=cell_run_id,
            )
        return MapReduceResult(status="completed", answer=answer, evidence=self._evidence(clues), cell_run_id=cell_run_id)

    async def _map_one(self, chunk: DocumentChunk, cell_prompt: str, semaphore: asyncio.Semaphore) -> MapClue | None:
        async with semaphore:
            prompt = self._build_map_prompt(cell_prompt, chunk)
            raw = (await self._cheap_model(prompt)).strip()
        if not raw or raw.casefold() == "null":
            return None
        return MapClue(chunk_id=chunk.chunk_id, clue=raw, metadata=chunk.metadata)

    @staticmethod
    def _build_map_prompt(cell_prompt: str, chunk: DocumentChunk) -> str:
        return f"""You are the cheap Map filter in a document analysis pipeline.
Task: extract ONLY clues relevant to the CELL_PROMPT.
If the chunk has no relevant information, output exactly NULL.
CELL_PROMPT:
{cell_prompt}

CHUNK_ID: {chunk.chunk_id}
CHUNK_TEXT:
{chunk.text}
"""

    @staticmethod
    def _build_reduce_prompt(cell_prompt: str, clues: Sequence[MapClue]) -> str:
        clue_text = "\n".join(f"[{clue.chunk_id}] {clue.clue}" for clue in clues)
        return f"""You are the smart Reduce synthesizer in a document analysis pipeline.
Use only the supplied chunk-tagged clues. Preserve source chunk ids in the reasoning evidence when useful.
CELL_PROMPT:
{cell_prompt}

FILTERED_CLUES:
{clue_text}
"""

    @staticmethod
    def _evidence(clues: Sequence[MapClue]) -> list[dict[str, object]]:
        return [{"chunk_id": clue.chunk_id, "clue": clue.clue, "metadata": clue.metadata} for clue in clues]
