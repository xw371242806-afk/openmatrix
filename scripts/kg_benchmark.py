"""Run a reproducible ISD-KG1 benchmark over real financial-report PDFs.

The script fails closed: missing engines, failed indexing, fewer than three
PDFs, or absent citation metadata produce a non-zero exit code and an honest
report instead of placeholder success metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.kg_adapter import create_kg_adapter


def collect_documents(docs_dir: Path) -> list[dict[str, object]]:
    from pypdf import PdfReader

    documents = []
    for path in sorted(docs_dir.glob("*.pdf")):
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        documents.append({"name": path.name, "pages": pages, "text": "\n".join(pages)})
    return documents


async def run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    documents = collect_documents(Path(args.docs))
    if len(documents) < 3:
        return {"status": "failed", "reason": "at least three real PDF files are required"}, 2

    adapter = create_kg_adapter(args.working_dir)
    if not adapter.is_available():
        return {"status": "failed", "reason": "KG engine unavailable or disabled"}, 2

    indexed = []
    for document in documents:
        start = time.perf_counter()
        result = await adapter.index_document(str(document["name"]), str(document["text"]))
        indexed.append({
            "document": document["name"],
            "success": result.success,
            "seconds": round(time.perf_counter() - start, 3),
            "chunks_indexed": result.chunks_indexed,
            "entities_indexed": result.entities_indexed,
            "error": result.error,
        })
        if not result.success:
            return {"status": "failed", "documents": indexed, "reason": result.error}, 2

    queries = []
    for question in args.question:
        start = time.perf_counter()
        hits = await adapter.retrieve(question, mode=args.mode)
        queries.append({
            "question": question,
            "seconds": round(time.perf_counter() - start, 3),
            "hits": [
                {"doc_id": hit.doc_id, "chunk_id": hit.chunk_id, "page": hit.page,
                 "score": hit.score, "text": hit.text[:240]}
                for hit in hits
            ],
        })

    flattened_hits = [hit for query in queries for hit in query["hits"]]
    citation_ready = bool(flattened_hits) and all(
        hit["doc_id"] and hit["chunk_id"] is not None and hit["page"] is not None
        for hit in flattened_hits
    )
    report = {
        "status": "ok" if citation_ready else "failed",
        "documents": indexed,
        "queries": queries,
        "citation_metadata_complete": citation_ready,
        "limitations": [
            "BM25/dense/fusion metrics and human answer-quality scoring must be supplied by the evaluation harness.",
            "Token cost requires provider usage metadata from the configured LightRAG callbacks.",
        ],
    }
    return report, 0 if citation_ready else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", required=True, help="Directory containing at least three PDF reports")
    parser.add_argument("--report", default="kg_poc_report.json")
    parser.add_argument("--working-dir", default="./kg_storage")
    parser.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix"])
    parser.add_argument("--question", action="append", default=["What was total revenue?"])
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    benchmark_report, exit_code = asyncio.run(run(arguments))
    Path(arguments.report).write_text(json.dumps(benchmark_report, indent=2), encoding="utf-8")
    print(json.dumps({"status": benchmark_report["status"], "report": arguments.report}))
    raise SystemExit(exit_code)
