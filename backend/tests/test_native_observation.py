"""独立模拟原生观测：真实原生执行器和SQLite/HTTP契约，供应商全部替身。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from app.api.routes.trip import router
from app.services.native_observation import native_event_emitter, observe_native_run
from app.services.native_preview_adapter import NativePreviewPipeline
from app.services.observation_service import ObservationService, span
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService


from native_planning_fixtures import fixture_planner


class NativeObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = TaskRepository(Path(self.tmp.name)/'tasks.db'); self.repo.initialize()
        self.repo.create('mock-task', {}); self.obs = ObservationService(self.repo)

    def details(self, task='mock-task'):
        return [self.repo.span(task, s['span_id']) for s in self.repo.spans(task)]

    def run_fixture(self):
        req, planner, models = fixture_planner(Barrier(2))
        def unavailable(): raise RuntimeError('private-adapter-error')
        pipeline = NativePreviewPipeline(planner_factory=lambda: planner, current_route_service_factory=unavailable)
        with self.obs.task('mock-task'):
            result = asyncio.run(pipeline.run(req, task_id='mock-task', plan_id='mock-plan'))
        return result, models

    def test_complete_chain_records_dispatch_maps_candidates_daily_plan_and_revision(self):
        result, models = self.run_fixture()
        self.assertEqual(result.snapshot.preview.draft.revision, 2)
        rows = self.details(); names = [r['name'] for r in rows]
        for name in ('master.task', 'map.request', 'map.result', 'child.result', 'planning.initial', 'repair.revision',
                     'repair.validation', 'planning.result', 'preview.converted'):
            self.assertIn('native.'+name, names)
        tasks = [r for r in rows if r['name'] == 'native.master.task']
        self.assertEqual({r['input_data']['target_role'] for r in tasks}, {'attractions', 'dining', 'lodging'})
        self.assertEqual(sorted(r['output_data']['round'] for r in tasks), [1, 2, 2])
        ids = {r['span_id'] for r in rows}
        self.assertTrue(all(r['parent_span_id'] in ids for r in rows if r['parent_span_id']))
        children = [r for r in rows if r['name'] == 'native.child.result']
        self.assertEqual({r['input_data']['role'] for r in children}, {'attractions', 'dining', 'lodging'})
        self.assertTrue(all(r['output_data']['task_result']['candidates'] for r in children))
        calls = [r for r in rows if r['operation_type'] == 'llm']
        self.assertEqual(len(calls), sum(m.calls for m in models))
        self.assertEqual({r['input_data']['stage'] for r in calls}, {'requirements', 'collection', 'daily_plan', 'revision'})
        self.assertEqual(sum(r['total_tokens'] or 0 for r in calls), 15*sum(r['total_tokens'] is not None for r in calls))
        events = [r for r in rows if r['name'].startswith('native.') and 'sequence' in r['input_data']]
        self.assertEqual(sorted(r['input_data']['sequence'] for r in events), list(range(1,len(events)+1)))
        self.assertTrue(all(r['input_data']['plan_id'] == 'mock-plan' for r in events))
        serialized = json.dumps(rows)
        for secret in ('private-reasoning', 'private-freeform-response', 'private-adapter-error'):
            self.assertNotIn(secret, serialized)
        self.assertFalse(self.repo.status('mock-task')['observation_incomplete'])

    def test_same_controlled_trip_drops_only_five_confirmation_requests_17_to_12(self):
        from app.services.native_tool_loop import NativeToolLoop
        from native_planning_fixtures import Model, call, done
        original_init, original_invoke = NativeToolLoop.__init__, Model.invoke_native
        original_run = NativeToolLoop.run
        def legacy_run(loop, *args, **kwargs):
            from dataclasses import replace
            result = original_run(loop, *args, **kwargs)
            # 历史对照允许接受后的确认请求；新适配器仍要求接受终态。
            if any(json.loads(m['content']).get('data', {}).get('status') == 'accepted'
                   for m in result.messages if m.get('role') == 'tool'):
                result = replace(result, termination_reason='submission_accepted')
            return result
        totals = []
        for legacy in (True, False):
            req, planner, models = fixture_planner(repair=False)
            # 增加一轮真实搜索决策，使无修订受控基线为17次。
            models[1].responses.insert(1, call('maps_text_search',
                {'city': '上海', 'keywords': '第二批模拟景点'}, 'sights-query-2'))
            requests = []
            def invoke(model, messages, **kwargs):
                requests.append(deepcopy(messages))
                if legacy and messages and messages[-1].get('role') == 'tool':
                    receipt = json.loads(messages[-1]['content'])
                    if receipt.get('data', {}).get('status') == 'accepted':
                        model.calls += 1
                        return done()
                return original_invoke(model, messages, **kwargs)
            def disabled(loop, *args, **kwargs):
                kwargs.pop('accepted_submission_tools', None)
                original_init(loop, *args, **kwargs)
            with ExitStack() as stack:
                stack.enter_context(patch.object(Model, 'invoke_native', invoke))
                if legacy:
                    stack.enter_context(patch.object(NativeToolLoop, '__init__', disabled))
                    stack.enter_context(patch.object(NativeToolLoop, 'run', legacy_run))
                pipeline = NativePreviewPipeline(planner_factory=lambda: planner,
                    current_route_service_factory=lambda: (_ for _ in ()).throw(RuntimeError('no routes')))
                result = asyncio.run(pipeline.run(req, task_id='controlled', plan_id='controlled-plan'))
            self.assertEqual(result.snapshot.preview.draft.revision, 1)
            self.assertEqual([m.calls for m in models], [7, 4, 3, 3] if legacy else [5, 3, 2, 2])
            totals.append(sum(m.calls for m in models))
            # 主采集派发后的决策调用仍保留，主模型可看到三角色结果。
            self.assertTrue(any(any(m.get('tool_call_id') == 'lodging-task' for m in h) for h in requests))
            if not legacy:
                for history in requests:
                    ids = [c['id'] for m in history for c in (m.get('tool_calls') or [])]
                    paired = [m['tool_call_id'] for m in history if m.get('role') == 'tool']
                    self.assertEqual(sorted(ids), sorted(paired))
        self.assertEqual(totals, [17, 12])

    def test_submission_end_records_one_real_request_and_safe_business_event(self):
        from app.services.native_tool_loop import NativeToolLoop
        from test_native_tool_loop import FakeNativeModel, completion, call, tool
        response = completion([call(name="submit")], reasoning_content="private-reasoning")
        model = FakeNativeModel([response, AssertionError("confirmation forbidden")])
        with self.obs.task('mock-task'):
            result = NativeToolLoop(model, [tool(lambda _: {"status": "accepted"}, "submit")],
                accepted_submission_tools=frozenset({"submit"}), observation_role="dining",
                observation_stage="collection").run([])
        rows = self.details()
        calls = [r for r in rows if r['operation_type'] == 'llm']
        self.assertEqual((len(calls), result.model_calls, calls[0]['total_tokens']), (1, 1, 5))
        self.assertEqual(calls[0]['output_data']['finish_reason'], 'tool_calls')
        events = [r for r in rows if r['name'] == 'native.loop.submission_accepted']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['output_data'], {'outcome': 'recorded', 'role': 'dining',
            'stage': 'collection', 'tool_name': 'submit', 'tool_call_id': 'call_1',
            'termination_reason': 'submission_accepted'})
        self.assertNotEqual(events[0]['operation_type'], 'llm')
        self.assertNotIn('private-reasoning', json.dumps(rows))
        self.assertFalse(self.repo.status('mock-task')['observation_incomplete'])

    def test_existing_observation_http_contract_returns_all_business_details_and_isolates_task(self):
        self.run_fixture()
        self.repo.start('mock-task'); self.repo.mark_preview_succeeded('mock-task')
        app = FastAPI(); app.include_router(router, prefix='/api')
        app.state.task_service = TaskService(self.repo, self.obs, lambda: None)
        async def verify():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                response = await client.get('/api/trip/tasks/mock-task/spans'); self.assertEqual(response.status_code, 200)
                selected = next(r for r in response.json() if r['name'] == 'native.planning.result')
                detail = await client.get('/api/trip/tasks/mock-task/spans/'+selected['span_id'])
                self.assertEqual(detail.status_code, 200); self.assertEqual(detail.json()['output_data']['draft']['revision'], 2)
                self.assertEqual((await client.get('/api/trip/tasks/another/spans/'+selected['span_id'])).status_code, 404)
                metrics = await client.get('/api/trip/tasks/mock-task/metrics'); self.assertEqual(metrics.status_code, 200)
        asyncio.run(verify())

    def test_daily_plan_argument_rejections_remain_diagnosable_after_tool_limit(self):
        from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
        from app.planning.core import generate_draft
        from app.services.native_tool_loop import NativeToolLoopError
        from test_native_daily_plan_agent import FakeModel
        from test_planning_core import Runtime
        from test_planning_core_contracts import snapshot, load_case
        value = load_case()['submission']
        value['days'][0]['activities'][0].pop('candidate_ref')
        model = FakeModel(*[value for _ in range(5)])
        with self.obs.task('mock-task'), self.assertRaises(NativeToolLoopError) as error:
            generate_draft(snapshot(), generator=NativeDailyPlanAgent(model), runtime=Runtime())
        self.assertEqual(error.exception.code, 'TOOL_CALL_LIMIT')
        rows = [r for r in self.details() if r['name'] == 'native.tool.arguments_rejected']
        self.assertEqual(len(rows), 4)
        for index, row in enumerate(rows, 1):
            self.assertEqual(row['status'], 'failed')
            output = row['output_data']
            self.assertEqual(output['code'], 'INVALID_TOOL_ARGUMENTS')
            self.assertEqual((output['role'], output['stage']), ('master', 'daily_plan'))
            self.assertEqual((output['tool_name'], output['tool_call_id']), ('submit_daily_plan', f'call{index}'))
            self.assertEqual(output['issues'], [{'path': ['days', 0, 'activities', 0], 'type': 'value_error'}])
            self.assertEqual((output['issue_count'], output['issues_truncated']), (1, False))

    def test_argument_rejection_diagnostics_limit_issues_and_hide_unknown_input(self):
        from app.services.native_tool_loop import NativeToolLoop
        from test_native_tool_loop import FakeNativeModel, completion, call, tool
        value = {'city': '上海', 'dates': ['2026-10-20'], 'preferences': []}
        value.update({f'private-field-{i}': 'private-value' for i in range(12)})
        model = FakeNativeModel([completion([call(arguments=json.dumps(value))]), completion(content='停止')])
        with self.obs.task('mock-task'):
            result = NativeToolLoop(model, [tool(lambda _: self.fail('invalid arguments executed'))]).run([])
        self.assertEqual(result.text, '停止')
        rows = [r for r in self.details() if r['name'] == 'native.tool.arguments_rejected']
        self.assertEqual(len(rows), 1)
        output = rows[0]['output_data']
        self.assertEqual((output['issue_count'], output['issues_truncated']), (12, True))
        self.assertEqual(output['issues'], [{'path': ['unknown_field'], 'type': 'extra_forbidden'}] * 8)
        serialized = json.dumps(self.details())
        for text in ('private-field', 'private-value', 'expected_parameters'):
            self.assertNotIn(text, serialized)

    def test_malformed_argument_json_records_safe_reason_without_changing_receipt(self):
        from app.services.native_tool_loop import NativeToolLoop
        from test_native_tool_loop import FakeNativeModel, completion, call, tool
        for raw in ('{"city":"private-value"', '{"city":"上海","city":"杭州"}', '[]'):
            with self.subTest(raw=raw):
                before = len(self.details())
                model = FakeNativeModel([completion([call(arguments=raw)]), completion(content='停止')])
                with self.obs.task('mock-task'):
                    result = NativeToolLoop(model, [tool(lambda _: self.fail('invalid JSON executed'))]).run([])
                receipt = json.loads(next(m['content'] for m in result.messages if m['role'] == 'tool'))
                self.assertEqual(receipt['error']['code'], 'INVALID_TOOL_ARGUMENTS')
                rows = [r for r in self.details()[before:] if r['name'] == 'native.tool.arguments_rejected']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['output_data']['code'], 'INVALID_TOOL_ARGUMENTS')
                self.assertNotIn('private-value', json.dumps(rows))

    def test_event_whitelist_redaction_rejection_and_idempotent_emitter(self):
        sink = []; emit = native_event_emitter(lambda e,d: sink.append(e))
        self.assertIs(native_event_emitter(emit), emit)
        with patch.dict('os.environ', {'TEST_API_KEY': 'synthetic-secret'}), self.obs.task('mock-task'):
            emit('unknown.response', {'content':'private-response'})
            emit('map.request', {'task_id':'child', 'tool_name':'maps_text_search', 'arguments': {'keywords': 'synthetic-secret',
                'api_key':'synthetic-secret', 'raw_response': 'private-response'}, 'messages': ['private-history']})
            emit('master.task_rejected', {'round': 2, 'code':'TASK_SCOPE_INVALID'})
        rows = self.details(); self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['status'], 'failed'); self.assertEqual(rows[-1]['output_data']['outcome'], 'rejected')
        serialized = json.dumps(rows)
        for value in ('synthetic-secret', 'private-response', 'private-history'): self.assertNotIn(value, serialized)
        self.assertEqual(len(sink), 3)

    def test_failure_size_limit_and_context_reset_do_not_change_business(self):
        emit = native_event_emitter()
        self.obs.content_limit = 256
        with self.obs.task('mock-task'):
            emit('master.task', {'arguments': {'objective': '大'*1000}})
        self.assertTrue(self.details()[0]['output_truncated'])
        with patch.object(self.repo, 'begin_span', side_effect=RuntimeError('private-failure')), self.obs.task('mock-task'):
            emit('map.request', {'tool_name': 'maps_text_search'})
        self.assertTrue(self.repo.status('mock-task')['observation_incomplete'])
        before = len(self.details()); emit('planning.result', {'status':'complete'})
        self.assertEqual(len(self.details()), before)

    def test_parallel_runs_do_not_mix_identity_sequence_or_parent(self):
        self.repo.create('second-task', {})
        class Runner:
            @observe_native_run
            async def run(self, request, *, task_id, plan_id, progress=None):
                with span('root', 'planning'):
                    native_event_emitter()('planning.phase', {'phase':'initial'})
        def execute(task):
            with self.obs.task(task): asyncio.run(Runner().run(None, task_id=task, plan_id='plan-'+task))
        with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(execute, ['mock-task','second-task']))
        for task in ('mock-task','second-task'):
            rows = self.details(task); event = rows[-1]
            self.assertEqual(event['input_data']['task_id'], task); self.assertEqual(event['input_data']['sequence'], 1)
            self.assertEqual(event['parent_span_id'], rows[0]['span_id'])


if __name__ == '__main__': unittest.main()
