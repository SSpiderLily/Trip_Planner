import asyncio
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.services import mcp_tool_executor
from app.services.mcp_tool_executor import MCPToolExecutor


class MCPToolExecutorTests(unittest.TestCase):
    def test_timed_out_queued_call_never_starts_after_worker_slot_opens(self):
        lock = threading.Lock()
        started_ids = []
        four_started = threading.Event()
        release = threading.Event()

        def actual_tool(_name, arguments):
            with lock:
                started_ids.append(arguments["id"])
                if len(started_ids) == 4:
                    four_started.set()
            release.wait(1)
            return {"result_status": "ok"}

        executor = MCPToolExecutor(actual_tool)
        callers = ThreadPoolExecutor(max_workers=4)
        running = []
        try:
            running = [
                callers.submit(
                    executor.run, "maps_text_search", {"id": str(index)}, timeout_seconds=2,
                )
                for index in range(4)
            ]
            self.assertTrue(four_started.wait(1))
            with self.assertRaises(TimeoutError):
                executor.run("maps_text_search", {"id": "queued"}, timeout_seconds=0.03)
            release.set()
            for future in running:
                future.result(timeout=1)
            time.sleep(0.05)
            self.assertEqual(len(started_ids), 4)
            self.assertNotIn("queued", started_ids)
        finally:
            release.set()
            callers.shutdown(wait=True)
            executor.close(wait=True)

    def test_tool_timeout_exception_propagates_without_waiting_to_call_deadline(self):
        def actual_tool(_name, _arguments):
            raise TimeoutError("provider timed out")

        executor = MCPToolExecutor(actual_tool)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "provider timed out"):
                executor.run("maps_text_search", {}, timeout_seconds=1)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            executor.close(wait=True)

    def test_cancelled_async_queued_call_never_starts(self):
        lock = threading.Lock()
        started_ids = []
        four_started = threading.Event()
        release = threading.Event()

        def actual_tool(_name, arguments):
            with lock:
                started_ids.append(arguments["id"])
                if len(started_ids) == 4:
                    four_started.set()
            release.wait(1)
            return {"result_status": "ok"}

        executor = MCPToolExecutor(actual_tool)
        callers = ThreadPoolExecutor(max_workers=4)
        running = [
            callers.submit(executor.run, "maps_text_search", {"id": str(i)}, timeout_seconds=2)
            for i in range(4)
        ]
        try:
            self.assertTrue(four_started.wait(1))

            async def cancel_queued():
                queued = asyncio.create_task(executor.run_async(
                    "maps_text_search", {"id": "cancelled"}, timeout_seconds=1,
                ))
                await asyncio.sleep(0.03)
                queued.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await queued

            asyncio.run(cancel_queued())
            release.set()
            for future in running:
                future.result(timeout=1)
            time.sleep(0.05)
            self.assertEqual(len(started_ids), 4)
            self.assertNotIn("cancelled", started_ids)
        finally:
            release.set()
            callers.shutdown(wait=True)
            executor.close(wait=True)

    def test_interactive_waiters_overtake_background_without_occupying_workers(self):
        lock = threading.Lock()
        started_ids = []
        first_four_started = threading.Event()
        interactive_started = threading.Event()
        background_started = threading.Event()
        release_first_four = threading.Event()
        release_interactive = threading.Event()

        def actual_tool(_name, arguments):
            request_id = arguments["id"]
            with lock:
                started_ids.append(request_id)
                if len(started_ids) == 4:
                    first_four_started.set()
            if request_id.startswith("initial-"):
                release_first_four.wait(1)
            elif request_id == "interactive-new":
                interactive_started.set()
                release_interactive.wait(1)
            elif request_id == "background":
                background_started.set()
            return {"result_status": "ok"}

        executor = MCPToolExecutor(actual_tool)
        callers = ThreadPoolExecutor(max_workers=4)
        active = [
            callers.submit(
                executor.run, "maps_text_search", {"id": f"initial-{index}"},
                timeout_seconds=2,
            )
            for index in range(4)
        ]
        try:
            self.assertTrue(first_four_started.wait(1))

            async def queued_requests():
                background = asyncio.create_task(executor.run_async(
                    "maps_text_search", {"id": "background"},
                    timeout_seconds=2, priority="background",
                ))
                await asyncio.sleep(0.02)
                interactive = asyncio.create_task(executor.run_async(
                    "maps_text_search", {"id": "interactive-new"}, timeout_seconds=2,
                ))
                await asyncio.sleep(0.02)
                queued_priorities = [
                    (ticket.priority.priority, ticket.state)
                    for ticket in executor._admission._tickets
                ]
                self.assertIn(("background", "waiting"), queued_priorities)
                self.assertIn(("interactive", "waiting"), queued_priorities)
                release_first_four.set()
                await asyncio.wait_for(asyncio.to_thread(interactive_started.wait, 1), timeout=1)
                self.assertFalse(background_started.is_set())
                release_interactive.set()
                await asyncio.gather(interactive, background)

            asyncio.run(queued_requests())
            for future in active:
                future.result(timeout=1)
            self.assertLess(started_ids.index("interactive-new"), started_ids.index("background"))
            self.assertFalse(executor.has_interactive_work)
        finally:
            release_first_four.set()
            release_interactive.set()
            callers.shutdown(wait=True)
            executor.close(wait=True)

    def test_background_admission_waits_until_interactive_work_is_idle(self):
        interactive_started = threading.Event()
        release_interactive = threading.Event()
        background_started = threading.Event()

        def actual_tool(_name, arguments):
            if arguments["id"] == "interactive":
                interactive_started.set()
                release_interactive.wait(1)
            else:
                background_started.set()
            return {"result_status": "ok"}

        executor = MCPToolExecutor(actual_tool)
        caller = ThreadPoolExecutor(max_workers=1)
        running = caller.submit(
            executor.run, "maps_text_search", {"id": "interactive"}, timeout_seconds=2,
        )
        try:
            self.assertTrue(interactive_started.wait(1))

            async def queue_background():
                task = asyncio.create_task(executor.run_async(
                    "maps_text_search", {"id": "background"},
                    timeout_seconds=2, priority="background",
                ))
                await asyncio.sleep(0.03)
                self.assertFalse(background_started.is_set())
                self.assertTrue(executor.has_interactive_work)
                release_interactive.set()
                await task

            asyncio.run(queue_background())
            running.result(timeout=1)
            self.assertTrue(background_started.is_set())
        finally:
            release_interactive.set()
            caller.shutdown(wait=True)
            executor.close(wait=True)

    def test_singleton_creation_is_locked_and_mcp_discovery_is_lazy(self):
        old_executor = mcp_tool_executor._shared_executor
        if old_executor is not None:
            old_executor.close(wait=True)
        with mcp_tool_executor._shared_executor_lock:
            mcp_tool_executor._shared_executor = None

        class FakeMCPTool:
            def run(self, _request):
                return {"result_status": "ok"}

        try:
            with patch("app.services.amap_service.get_amap_mcp_tool", return_value=FakeMCPTool()) as discover:
                with ThreadPoolExecutor(max_workers=16) as callers:
                    executors = list(callers.map(lambda _: mcp_tool_executor.get_mcp_tool_executor(), range(32)))
                self.assertTrue(all(item is executors[0] for item in executors))
                self.assertEqual(discover.call_count, 0)
                result = executors[0].run("maps_text_search", {}, timeout_seconds=1)
                self.assertEqual(result["result_status"], "ok")
                self.assertEqual(discover.call_count, 1)
                executors[0].close(wait=True)
        finally:
            with mcp_tool_executor._shared_executor_lock:
                mcp_tool_executor._shared_executor = None
            if old_executor is not None:
                mcp_tool_executor._shared_executor = old_executor

    def test_slow_lazy_discovery_counts_toward_call_deadline(self):
        old_executor = mcp_tool_executor._shared_executor
        if old_executor is not None:
            old_executor.close(wait=True)
        with mcp_tool_executor._shared_executor_lock:
            mcp_tool_executor._shared_executor = None
        discovery_started = threading.Event()
        release_discovery = threading.Event()
        actual_calls = []

        class FakeMCPTool:
            def run(self, request):
                actual_calls.append(request)
                return {"result_status": "ok"}

        def discover():
            discovery_started.set()
            release_discovery.wait(1)
            return FakeMCPTool()

        try:
            with patch("app.services.amap_service.get_amap_mcp_tool", side_effect=discover):
                executor = mcp_tool_executor.get_mcp_tool_executor()
                with self.assertRaises(TimeoutError):
                    executor.run("maps_text_search", {}, timeout_seconds=0.03)
                self.assertTrue(discovery_started.is_set())
                release_discovery.set()
                time.sleep(0.05)
                self.assertEqual(actual_calls, [])
                executor.close(wait=True)
        finally:
            release_discovery.set()
            with mcp_tool_executor._shared_executor_lock:
                mcp_tool_executor._shared_executor = None
            if old_executor is not None:
                mcp_tool_executor._shared_executor = old_executor


if __name__ == "__main__":
    unittest.main()
