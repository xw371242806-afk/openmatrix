import sqlite3
import main

def test_support_status_flags_number_mismatch():
    quote = "Shack sales increased 15.2% to $1.4 billion."
    row = main.verify_matrix_citations([{"question": "What was total revenue for fiscal 2025?", "answer": "Total revenue was $2.0 billion", "quote": quote}], [quote])[0]
    assert row["citation_status"] == "verified_exact"
    assert row["support_status"] == "unsupported"

def test_table_amount_scale_and_metric_support():
    quote = "Total revenue $ 1,445,306 $ 1,252,608"
    assert main.assess_answer_support("What was total revenue?", "$1,445,306 thousand", quote, quote)["support_status"] == "supported"
    assert main.assess_answer_support("What was total revenue?", "$1.4453 billion", quote, "in thousands " + quote)["support_status"] == "supported"
    assert main.assess_answer_support("What was total revenue?", "$1.4 billion", "Shack sales $1.4 billion", "Shack sales $1.4 billion")["support_status"] == "partial"
    assert main.assess_answer_support("What was net income?", "2.3%", "Net income $2.3 million", "Net income $2.3 million")["support_status"] == "unsupported"

def test_old_matrix_schema_migrates_without_losing_cells(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE matrix_cells (run_id TEXT, cell_id TEXT, document_index INTEGER, document_name TEXT, question_index INTEGER, question TEXT, status TEXT, answer TEXT, quote TEXT, citation_status TEXT, page INTEGER, error TEXT, PRIMARY KEY(run_id,cell_id))")
    conn.commit(); conn.close()
    main.init_matrix_store(path)
    main.persist_matrix_cell("run", {"cell_id":"c", "document_index":0,"document_name":"a","question_index":0,"question":"revenue","status":"completed","support_status":"supported","support_reason":"matching"}, path)
    assert main.load_matrix_run("run", path)["cells"][0]["support_status"] == "supported"

def test_async_old_matrix_schema_migrates(tmp_path):
    import asyncio
    path = tmp_path / "old_async.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE matrix_cells (run_id TEXT, cell_id TEXT, document_index INTEGER, document_name TEXT, question_index INTEGER, question TEXT, status TEXT, answer TEXT, quote TEXT, citation_status TEXT, page INTEGER, error TEXT, PRIMARY KEY(run_id,cell_id))")
    conn.commit(); conn.close()
    async def exercise():
        store = main.AiosqliteResultStore(path)
        try:
            await store.persist_cell("run", {"cell_id":"c", "document_index":0,"document_name":"a","question_index":0,"question":"revenue","status":"completed","support_status":"unsupported","support_reason":"mismatch"})
            run = await store.load_run("run")
            assert run["cells"][0]["support_reason"] == "mismatch"
        finally:
            await store.close()
    asyncio.run(exercise())
