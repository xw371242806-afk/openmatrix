import asyncio
import unittest

from core.adapters.map_reduce_dispatcher import DocumentChunk, MapReduceDispatcher


class MapReduceDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_null_map_outputs_and_reduces_with_chunk_ids(self):
        map_prompts = []
        reduce_prompts = []

        async def cheap_model(prompt: str) -> str:
            map_prompts.append(prompt)
            if "Revenue was 125" in prompt:
                return "Revenue clue: 125 million"
            return "NULL"

        async def smart_model(prompt: str) -> str:
            reduce_prompts.append(prompt)
            return "Final answer from filtered evidence"

        dispatcher = MapReduceDispatcher(cheap_model, smart_model, max_concurrency=2)
        result = await dispatcher.run(
            [
                DocumentChunk(chunk_id="c1", text="Revenue was 125 million", metadata={"page": 1}),
                DocumentChunk(chunk_id="c2", text="Unrelated boilerplate", metadata={"page": 2}),
            ],
            "What was revenue?",
            cell_run_id="cell-1",
        )

        self.assertEqual("completed", result.status)
        self.assertEqual("Final answer from filtered evidence", result.answer)
        self.assertEqual("cell-1", result.cell_run_id)
        self.assertEqual(["c1"], [item["chunk_id"] for item in result.evidence])
        self.assertEqual(2, len(map_prompts))
        self.assertEqual(1, len(reduce_prompts))
        self.assertIn("[c1] Revenue clue: 125 million", reduce_prompts[0])
        self.assertNotIn("c2", reduce_prompts[0])

    async def test_returns_not_disclosed_when_no_map_clues(self):
        async def cheap_model(_prompt: str) -> str:
            return "NULL"

        async def smart_model(_prompt: str) -> str:
            raise AssertionError("reduce should not run without clues")

        dispatcher = MapReduceDispatcher(cheap_model, smart_model)
        result = await dispatcher.run([DocumentChunk(chunk_id="c1", text="No matching data")], "Find revenue")

        self.assertEqual("not_disclosed", result.status)
        self.assertIsNone(result.answer)
        self.assertEqual([], result.evidence)

    async def test_limits_map_concurrency_with_semaphore(self):
        active = 0
        max_seen = 0

        async def cheap_model(_prompt: str) -> str:
            nonlocal active, max_seen
            active += 1
            max_seen = max(max_seen, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "clue"

        async def smart_model(_prompt: str) -> str:
            return "answer"

        dispatcher = MapReduceDispatcher(cheap_model, smart_model, max_concurrency=2)
        result = await dispatcher.run([DocumentChunk(chunk_id=f"c{i}", text="data") for i in range(5)], "Find data")

        self.assertEqual("completed", result.status)
        self.assertLessEqual(max_seen, 2)
        self.assertEqual(5, len(result.evidence))


    async def test_partial_map_failure_returns_needs_review_with_chunk_id(self):
        async def cheap_model(prompt: str) -> str:
            if "CHUNK_ID: failed" in prompt:
                raise RuntimeError("rate limited")
            return "usable clue"

        async def smart_model(_prompt: str) -> str:
            return "answer from partial evidence"

        dispatcher = MapReduceDispatcher(cheap_model, smart_model)
        result = await dispatcher.run(
            [
                DocumentChunk(chunk_id="ok", text="relevant data"),
                DocumentChunk(chunk_id="failed", text="also needs inspection"),
            ],
            "Find data",
        )

        self.assertEqual("needs_review", result.status)
        self.assertEqual("answer from partial evidence", result.answer)
        self.assertIn("failed", result.error or "")
        self.assertEqual(["ok"], [item["chunk_id"] for item in result.evidence])


    async def test_partial_map_failure_with_only_null_clues_returns_needs_review(self):
        async def cheap_model(prompt: str) -> str:
            if "CHUNK_ID: failed" in prompt:
                raise RuntimeError("rate limited")
            return "NULL"

        async def smart_model(_prompt: str) -> str:
            raise AssertionError("reduce should not run without clues")

        dispatcher = MapReduceDispatcher(cheap_model, smart_model)
        result = await dispatcher.run(
            [
                DocumentChunk(chunk_id="null", text="irrelevant"),
                DocumentChunk(chunk_id="failed", text="uninspected data"),
            ],
            "Find data",
        )

        self.assertEqual("needs_review", result.status)
        self.assertIsNone(result.answer)
        self.assertEqual([], result.evidence)
        self.assertIn("failed", result.error or "")


    async def test_cancelled_map_call_propagates_cancellation(self):
        async def cheap_model(prompt: str) -> str:
            if "CHUNK_ID: cancelled" in prompt:
                raise asyncio.CancelledError()
            return "usable clue"

        async def smart_model(_prompt: str) -> str:
            raise AssertionError("reduce should not run after cancellation")

        dispatcher = MapReduceDispatcher(cheap_model, smart_model)
        with self.assertRaises(asyncio.CancelledError):
            await dispatcher.run(
                [
                    DocumentChunk(chunk_id="ok", text="relevant data"),
                    DocumentChunk(chunk_id="cancelled", text="cancelled data"),
                ],
                "Find data",
            )

    async def test_failed_when_all_map_calls_fail(self):
        async def cheap_model(_prompt: str) -> str:
            raise RuntimeError("rate limited")

        async def smart_model(_prompt: str) -> str:
            raise AssertionError("reduce should not run")

        dispatcher = MapReduceDispatcher(cheap_model, smart_model)
        result = await dispatcher.run([DocumentChunk(chunk_id="c1", text="data")], "Find data")

        self.assertEqual("failed", result.status)
        self.assertIn("all map calls failed", result.error or "")
        self.assertIn("c1", result.error or "")


if __name__ == "__main__":
    unittest.main()
