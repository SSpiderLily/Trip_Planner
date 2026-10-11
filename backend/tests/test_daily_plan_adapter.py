"""F06 同步生成器的异步适配器测试。"""
from __future__ import annotations

import asyncio
import threading
import unittest

from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter


class DailyPlanGeneratorAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_f06_off_loop_and_forwards_arguments(self):
        started, release = threading.Event(), threading.Event()
        expected = object()
        request, candidates, facts, areas, results = (object() for _ in range(5))
        seen = {}

        class SyncGenerator:
            def generate(self, *args, **kwargs):
                seen["args"] = args
                seen["kwargs"] = kwargs
                started.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release F06")
                return expected

        adapter = DailyPlanGeneratorAdapter(SyncGenerator())
        call = asyncio.create_task(adapter.generate(
            request, candidates, facts, areas, results, source_run_id="run-1",
        ))
        self.assertTrue(await asyncio.to_thread(started.wait, 3))

        # This yield would block if the synchronous F06 invocation ran on the event loop.
        await asyncio.sleep(0)
        self.assertFalse(call.done())
        release.set()

        self.assertIs(await call, expected)
        self.assertEqual(seen["args"], (request, candidates, facts, areas, results))
        self.assertEqual(seen["kwargs"], {"source_run_id": "run-1"})

    async def test_preserves_generator_exception(self):
        class SyncGenerator:
            def generate(self, *args, **kwargs):
                raise ValueError("F06 validation failure")

        adapter = DailyPlanGeneratorAdapter(SyncGenerator())
        with self.assertRaisesRegex(ValueError, "F06 validation failure"):
            await adapter.generate(*(object() for _ in range(5)), source_run_id="run-2")


if __name__ == "__main__":
    unittest.main()
