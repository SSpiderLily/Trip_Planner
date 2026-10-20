"""模型连接重试：真实原生循环/SDK适配器，供应商响应全部模拟。"""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import tempfile
from threading import Event, Thread, Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import APIConnectionError, BadRequestError, AuthenticationError, RateLimitError, InternalServerError

from app.services.llm_service import UsageAwareLLM
from app.services.native_tool_loop import NativeTool, NativeToolArguments, NativeToolLoop, NativeToolLoopError
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from app.services.native_preview_adapter import NativePreviewPipeline
from app.services.native_model_retry import is_connection_failure, retry_delay_seconds
from app.services.planning_timeout_policy import timeout_policy
from hello_agents.core.exceptions import HelloAgentsException
from native_planning_fixtures import fixture_planner


class EmptyArgs(NativeToolArguments):
    pass


def connection_error():
    error = APIConnectionError(request=httpx.Request('POST', 'https://fixture.invalid/v1/chat/completions'))
    error.__cause__ = httpx.RemoteProtocolError('private-remote-error')
    return error


def response(tool_name=None, call_id='call-1', *, finish=None):
    message = {'role': 'assistant', 'content': 'private-final', 'reasoning_content': 'private-reasoning'}
    if tool_name:
        message.update(content=None, tool_calls=[{'id': call_id, 'type': 'function',
            'function': {'name': tool_name, 'arguments': '{}'}}])
    return SimpleNamespace(choices=[SimpleNamespace(message=message,
        finish_reason=finish or ('tool_calls' if tool_name else 'stop'))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))


def model(*responses):
    client = Mock()
    client.with_options.return_value = client
    # 保存独立副本，能发现重试时消息、声明或参数被修改。
    requests = []
    pending = iter(responses)
    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item
    client.chat.completions.create.side_effect = create
    llm = object.__new__(UsageAwareLLM)
    llm.model, llm.temperature, llm._client = 'fixture-model', 0, client
    return llm, client, requests


def tool(name, handler):
    return NativeTool(name, '模拟工具', EmptyArgs, handler)


class NativeModelRetryTests(unittest.TestCase):
    def setUp(self):
        # 测试不等待真实退避时间；专门的取消测试另行执行真实等待函数。
        self.wait_patch = patch.object(NativeToolLoop, '_wait_before_retry', create=True)
        self.wait = self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    def test_nested_master_child_retries_reuse_completed_map_and_child_results(self):
        maps = Mock(return_value={'poi_id': 'mock-poi'})
        child, child_client, child_requests = model(response('maps_search'), connection_error(), response())
        child_loop = NativeToolLoop(child, [tool('maps_search', maps)], max_model_calls=2)
        delegate = Mock(side_effect=lambda _: child_loop.run([]).text)
        master, client, requests = model(response('find_attractions'), connection_error(), connection_error(), response())
        result = NativeToolLoop(master, [tool('find_attractions', delegate)], max_model_calls=2).run(
            [{'role': 'user', 'content': 'fixture'}], temperature=0.2, max_retries=9)
        self.assertEqual(result.text, 'private-final')
        delegate.assert_called_once(); maps.assert_called_once()
        self.assertEqual(len(requests), 4); self.assertEqual(len(child_requests), 3)
        self.assertEqual(requests[1], requests[2]); self.assertEqual(requests[2], requests[3])
        self.assertEqual(child_requests[1], child_requests[2])
        self.assertEqual(requests[-1]['messages'][-1]['role'], 'tool')
        self.assertEqual(child_requests[-1]['messages'][-1]['role'], 'tool')
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(result.completions), 2)
        self.assertEqual(result.model_calls, 4)
        for sdk in (client, child_client):
            self.assertTrue(all(c.kwargs['max_retries'] == 0 for c in sdk.with_options.call_args_list))

    def test_exhaustion_stops_after_three_attempts_without_reexecuting_tool(self):
        handler = Mock(return_value={'candidate': 'mock'})
        llm, client, requests = model(response('query'), *(connection_error() for _ in range(3)), response())
        with self.assertRaises(NativeToolLoopError) as raised:
            NativeToolLoop(llm, [tool('query', handler)]).run([])
        self.assertEqual(raised.exception.code, 'MODEL_CALL_FAILED')
        self.assertEqual(len(requests), 4)
        self.assertEqual(self.wait.call_count, 2)
        handler.assert_called_once()
        self.assertNotIn('private', str(raised.exception))

    def test_http_errors_and_programming_errors_are_not_connection_retries(self):
        for error_type, status in ((BadRequestError, 400), (AuthenticationError, 401),
                                   (RateLimitError, 429), (InternalServerError, 500)):
            with self.subTest(status=status):
                error = error_type('private-http-error', response=httpx.Response(status,
                    request=httpx.Request('POST', 'https://fixture.invalid')), body=None)
                self.assert_single_failure(error)
        self.assert_single_failure(ValueError('private-logic-error'))

    def assert_single_failure(self, error):
        llm, _, requests = model(error, response())
        with self.assertRaises(NativeToolLoopError):
            NativeToolLoop(llm, []).run([])
        self.assertEqual(len(requests), 1)
        self.wait.assert_not_called()

    def test_truncation_and_invalid_tool_calls_do_not_retry_or_execute_tools(self):
        for item, code in ((response('query', finish='length'), 'INCOMPLETE_COMPLETION'),
                           (response('query', call_id=''), 'INVALID_TOOL_CALL')):
            with self.subTest(code=code):
                handler = Mock()
                llm, _, requests = model(item, response())
                with self.assertRaises(NativeToolLoopError) as raised:
                    NativeToolLoop(llm, [tool('query', handler)]).run([])
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(requests), 1)
                handler.assert_not_called(); self.wait.assert_not_called()

    def test_cancel_during_backoff_stops_before_second_request(self):
        cancelled = Event()
        self.wait.side_effect = lambda *_: cancelled.set()
        llm, _, requests = model(connection_error(), response())
        with self.assertRaises(NativeToolLoopError) as raised:
            NativeToolLoop(llm, []).run([], cancel_event=cancelled)
        self.assertEqual(raised.exception.code, 'CANCELLED')
        self.assertEqual(len(requests), 1)

    def test_cancel_during_retried_request_discards_late_tool_calls(self):
        cancelled = Event()
        def late():
            cancelled.set()
            return response('query')
        llm, _, requests = model(connection_error(), late)
        handler = Mock()
        with self.assertRaises(NativeToolLoopError) as raised:
            NativeToolLoop(llm, [tool('query', handler)]).run([], cancel_event=cancelled)
        self.assertEqual(raised.exception.code, 'CANCELLED')
        self.assertEqual(len(requests), 2); handler.assert_not_called()

    def test_attempts_are_separate_safe_observation_records(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = TaskRepository(Path(folder)/'tasks.db'); repo.initialize(); repo.create('mock', {})
            llm, _, requests = model(connection_error(), connection_error(), response())
            with ObservationService(repo).task('mock'):
                NativeToolLoop(llm, [], observation_role='master', observation_stage='collection').run([])
            rows = [repo.span('mock', s['span_id']) for s in repo.spans('mock')]
            attempts = [r for r in rows if r['operation_type'] == 'llm']
            self.assertEqual([r['status'] for r in attempts], ['failed', 'failed', 'succeeded'])
            self.assertEqual([r['input_data']['attempt'] for r in attempts], [1, 2, 3])
            self.assertEqual({r['input_data']['request_number'] for r in attempts}, {1})
            self.assertEqual(len({r['input_data']['model_request_id'] for r in attempts}), 1)
            self.assertTrue(all(r['output_data']['retry']['will_retry'] for r in attempts[:2]))
            self.assertEqual(attempts[0]['output_data']['model_call']['exception_chain'][0], 'APIConnectionError')
            self.assertEqual([r['total_tokens'] for r in attempts], [None, None, 15])
            self.assertNotIn('private', json.dumps(rows))
            self.assertFalse(repo.status('mock')['observation_incomplete'])

    def test_full_native_pipeline_recovers_all_roles_without_duplicate_dispatch_or_maps(self):
        req, planner, models = fixture_planner(Barrier(2))
        master, *children = models
        def fail(_):
            raise connection_error()
        # 重现主采集第3次请求失败；各子智能体也在完成地图查询之后失败一次。
        master.responses.insert(4, fail)
        for child in children:
            child.responses.insert(1, fail)
        maps = Mock(wraps=planner.collector.adapter._tool_runner)
        planner.collector.adapter._tool_runner = maps
        with tempfile.TemporaryDirectory() as folder:
            repo = TaskRepository(Path(folder)/'tasks.db'); repo.initialize(); repo.create('mock', {})
            with ObservationService(repo).task('mock'):
                pipeline = NativePreviewPipeline(planner_factory=lambda: planner,
                    current_route_service_factory=Mock(side_effect=RuntimeError('fixture-no-routes')))
                result = asyncio.run(pipeline.run(req, task_id='mock', plan_id='mock-plan'))
            rows = [repo.span('mock', s['span_id']) for s in repo.spans('mock')]
            self.assertEqual(result.snapshot.preview.draft.revision, 2)
            self.assertEqual([m.calls for m in models], [10, 4, 4, 4])
            self.assertEqual(maps.call_count, 3)  # 景点1、餐饮同址两餐复用1、住宿1。
            self.assertEqual(len([r for r in rows if r['name'] == 'native.master.task']), 3)
            self.assertEqual(len([r for r in rows if r['name'] == 'native.child.result']), 3)
            self.assertEqual(len([r for r in rows if r['name'] == 'native.planning.initial']), 1)
            attempts = [r for r in rows if r['operation_type'] == 'llm']
            failed = [r for r in attempts if r['status'] == 'failed']
            self.assertEqual(len(failed), 4)
            self.assertEqual({r['input_data']['role'] for r in failed}, {'master','attractions','dining','lodging'})
            for row in failed:
                same_request = [r for r in attempts if r['input_data']['model_request_id'] == row['input_data']['model_request_id']]
                self.assertEqual([r['status'] for r in same_request], ['failed', 'succeeded'])
            self.assertFalse(repo.status('mock')['observation_incomplete'])

    def test_retry_preserves_existing_timeout_policy(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), timeout_policy(enabled):
                llm, sdk, _ = model(connection_error(), response())
                NativeToolLoop(llm, []).run([], request_timeout=12)
                self.assertEqual([c.kwargs['timeout'] for c in sdk.with_options.call_args_list],
                                 [12 if enabled else None] * 2)

    def test_real_backoff_is_interruptible(self):
        self.wait_patch.stop()
        cancelled = Event(); entered = Event(); stopped = Event()
        def waiting():
            entered.set()
            NativeToolLoop._wait_before_retry(30, cancelled)
            stopped.set()
        thread = Thread(target=waiting, daemon=True); thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancelled.set()
            self.assertTrue(stopped.wait(1))
        finally:
            cancelled.set(); thread.join(1)

    def test_retry_classification_does_not_follow_unrelated_exception_context(self):
        wrapped = HelloAgentsException('private-wrapper')
        wrapped.__cause__ = connection_error()
        self.assertTrue(is_connection_failure(wrapped))
        self.assertTrue(is_connection_failure(httpx.ReadTimeout('private-timeout')))
        unrelated = ValueError('private-validation')
        unrelated.__context__ = connection_error()
        self.assertFalse(is_connection_failure(unrelated))
        cycle = HelloAgentsException('private-cycle'); cycle.__cause__ = cycle
        self.assertFalse(is_connection_failure(cycle))
        with patch('app.services.native_model_retry.uniform', return_value=1.5):
            self.assertEqual([retry_delay_seconds(a) for a in (1,2)], [1.5,3.0])


if __name__ == '__main__':
    unittest.main()
