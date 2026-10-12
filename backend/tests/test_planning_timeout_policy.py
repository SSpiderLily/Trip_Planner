"""无超时模式：超出旧期限仍完成，且不影响其他任务的期限/取消。"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import time
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.planning_timeout_policy import timeout_policy, timeouts_enabled
from app.services.llm_service import UsageAwareLLM
from app.services.mcp_tool_executor import MCPToolExecutor
from app.agents.master_decider import MasterDecider
from test_master_decider import FakeLLM, make_context
from test_planning_v4_pipeline import (
    PlanningV4Pipeline, PlanningV4PipelineError, request_fixture, endpoint_fixture, FakeInterpreter,
    FakeDispatch, FakeDailyPlanAdapter, FakeRouteService, FakeRepair, ManualClock,
)


class PlanningTimeoutPolicyTests(unittest.TestCase):
    def test_model_sdk_receives_explicit_none_in_worker_context(self):
        llm = UsageAwareLLM.__new__(UsageAwareLLM)
        llm.model, llm.temperature = 'fixture', 0
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='ok'), finish_reason='stop')],
            usage=None)
        llm._client = client
        async def run():
            with timeout_policy(False):
                return await asyncio.to_thread(llm.invoke, [], request_timeout=0.001)
        self.assertEqual(asyncio.run(run()), 'ok')
        self.assertEqual(client.with_options.call_args.kwargs['timeout'], None)
        self.assertTrue(timeouts_enabled())

    def test_master_finishes_after_its_old_deadline(self):
        def slow(_):
            self.assertFalse(timeouts_enabled())
            time.sleep(0.03)
            return json.dumps({'action': 'plan', 'reason': 'done'})
        master = MasterDecider(llm=FakeLLM([slow]), request_timeout_seconds=0.001)
        with timeout_policy(False):
            result = master.run(make_context())
        self.assertEqual(result.decision.action, 'plan')

    def test_mcp_sync_async_waits_and_still_honors_cancellation(self):
        def slow(*_):
            self.assertFalse(timeouts_enabled())
            time.sleep(0.03)
            return {'ok': True}
        executor = MCPToolExecutor(slow)
        try:
            with timeout_policy(False):
                self.assertEqual(executor.run('fixture', {}, timeout_seconds=0.001), {'ok': True})
                async def run():
                    return await executor.run_async('fixture', {}, timeout_seconds=0.001)
                self.assertEqual(asyncio.run(run()), {'ok': True})
                cancelled = Event()
                cancelled.set()
                with self.assertRaises(TimeoutError):
                    executor.run('fixture', {}, timeout_seconds=0.001, cancel_event=cancelled)
        finally:
            executor.close()

    def test_pipeline_repair_and_retained_cache_survive_old_global_deadline(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request)
        tracker = {}
        clock = ManualClock(deadline=10)
        pipeline = PlanningV4Pipeline(
            enforce_timeouts=False, total_timeout_seconds=10,
            requirement_interpreter_factory=lambda _: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _: FakeDailyPlanAdapter(tracker, clock),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
            clock=clock)
        result = asyncio.run(pipeline.run(
            request, task_id='pipeline-task', plan_id='plan-no-timeout', progress=lambda _: None))
        self.assertEqual(tracker['repair_collection'].stop_reason, 'no_progress')
        self.assertEqual(len(tracker['dispatch_contexts']), 2)
        self.assertTrue(timeouts_enabled())
        self.assertIsNone(result.backfill)
        self.assertEqual(result.snapshot.preview.calculation.alternative_backfill, 'not_required')

    def test_amap_http_timeout_can_be_explicitly_disabled(self):
        code = """
from unittest.mock import Mock
from vendor import amap_mcp_server_0_1_11 as server
server.requests.get = Mock(return_value=object())
server._http_get('https://example.invalid')
assert server.requests.get.call_args.kwargs['timeout'] is None
"""
        env = {**os.environ, 'AMAP_MAPS_API_KEY': 'fixture-key',
               'AMAP_HTTP_TIMEOUT_ENABLED': 'false',
               'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'scripts')}
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stage_wait_does_not_expire_and_context_isolated(self):
        pipeline = PlanningV4Pipeline()
        async def slow():
            await asyncio.sleep(0.02)
            return 1
        async def run():
            with timeout_policy(False):
                self.assertEqual(await pipeline._await_stage(
                    slow(), timeout=0.001, stage='fixture', cancel_event=Event()), 1)
            self.assertTrue(timeouts_enabled())
            with self.assertRaises(PlanningV4PipelineError):
                await pipeline._await_stage(slow(), timeout=0.001, stage='fixture', cancel_event=Event())
        asyncio.run(run())
