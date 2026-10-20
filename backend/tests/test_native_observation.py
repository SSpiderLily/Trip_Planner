"""独立模拟原生观测：真实原生执行器和SQLite/HTTP契约，供应商全部替身。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
