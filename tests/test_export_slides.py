import json
from pathlib import Path

import pytest


def test_export_slides_creates_pptx(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    import mcp_server
    monkeypatch.setenv("OPENMATRIX_EXPORT_ROOT", str(tmp_path))

    monkeypatch.setattr(mcp_server, "load_matrix_run", lambda run_id: {
        "cells": [{
            "question": "What was revenue?", "answer": "$1,445,306 thousand",
            "document_name": "fixture.pdf", "page": 89,
            "citation_status": "verified_exact", "quote": "Total revenue $ 1,445,306",
        }]
    })
    result = json.loads(mcp_server.export_slides("fixture", output_dir="slides"))
    output = Path(result["path"])
    assert result["format"] == "pptx"
    assert result["slides"] == 1
    assert output.is_file() and output.stat().st_size > 0
    from pptx import Presentation
    assert len( Presentation(str(output)).slides ) == 1
