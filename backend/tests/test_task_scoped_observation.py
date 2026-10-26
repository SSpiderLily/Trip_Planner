"""v4观测接口复用任务凭据，所有测试均为合成记录且不调用模型。"""
import json
import tempfile
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes.trip import router
from app.services.task_repository import TaskRepository
from app.services.observation_service import ObservationService
from app.services.task_service import TaskService
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import PreviewTaskService

class TaskScopedObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = TaskRepository(Path(self.temp.name) / 'tasks.sqlite3'); self.repo.initialize()
        self.previews = PreviewRepository(Path(self.temp.name) / 'previews.sqlite3'); self.previews.initialize()
        self.service = TaskService(self.repo, ObservationService(self.repo), lambda: None)
        app = FastAPI(); app.include_router(router, prefix='/api')
        app.state.task_service = self.service
        app.state.preview_task_service = PreviewTaskService(self.service, self.previews)
        self.client = TestClient(app)
        for task in ('a','b'):
            self.repo.create(task, {}); self.repo.start(task)
            self.previews.create_job(task, 'plan-'+task, 'synthetic-token-'+task)
            self.repo.begin_span(('span-'+task, task, None, 'llm.native_tool_loop', 'llm',
                                  '2026-10-26T00:00:00+00:00', json.dumps({'role':'master','marker':'private-'+task}), 0))
        self.headers = {'Authorization':'Bearer synthetic-token-a'}

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def path(self, suffix): return '/api/trip/v4/tasks/a/'+suffix

    def test_each_endpoint_requires_its_own_matching_credential(self):
        for suffix in ('metrics','spans','spans/span-a'):
            for headers in ({}, {'Authorization':'Bearer '}, {'Authorization':'Bearer wrong'},
                            {'Authorization':'Bearer synthetic-token-b'}):
                with self.subTest(suffix=suffix, headers_present=bool(headers)):
                    response=self.client.get(self.path(suffix),headers=headers)
                    self.assertIn(response.status_code,(401,403))
                    self.assertNotIn('private-a',response.text)
                    self.assertEqual(response.headers.get('cache-control'),'no-store')

    def test_running_task_can_read_metrics_spans_and_own_detail(self):
        metrics=self.client.get(self.path('metrics'),headers=self.headers)
        self.assertEqual(metrics.status_code,200,metrics.text)
        data=metrics.json(); self.assertEqual(data['task']['task_id'],'a')
        self.assertEqual(data['task']['status'],'running'); self.assertEqual(data['model']['total'],1)
        self.assertFalse(data['tokens']['complete'])
        self.assertEqual(metrics.headers.get('cache-control'),'no-store')
        spans=self.client.get(self.path('spans'),headers=self.headers)
        self.assertEqual(spans.status_code,200)
        self.assertEqual([x['span_id'] for x in spans.json()],['span-a'])
        self.assertNotIn('private-a',spans.text)
        detail=self.client.get(self.path('spans/span-a'),headers=self.headers)
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.json()['input_data']['marker'],'private-a')

    def test_span_id_is_bound_to_authorized_task(self):
        for span in ('span-b','missing'):
            response=self.client.get(self.path('spans/'+span),headers=self.headers)
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.json()['detail']['code'],'SPAN_NOT_FOUND')
            self.assertNotIn('private-b',response.text)

    def test_completed_and_incomplete_observation_remain_readable(self):
        self.repo.end_span('span-a','succeeded','{}',False,None,dict(input_tokens=10,output_tokens=2,total_tokens=12))
        self.repo.fail('a','SYNTHETIC','fixture')
        with self.repo.connection() as c:c.execute("UPDATE tasks SET observation_incomplete=1 WHERE task_id='a'")
        response=self.client.get(self.path('metrics'),headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['task']['observation_incomplete'])
        self.assertEqual(response.json()['tokens']['total_tokens'],12)

    def test_valid_preview_credential_with_missing_observation_is_distinct(self):
        with self.repo.connection() as c:c.execute("DELETE FROM spans WHERE task_id='a'");c.execute("DELETE FROM tasks WHERE task_id='a'")
        for suffix in ('metrics','spans','spans/span-a'):
            response=self.client.get(self.path(suffix),headers=self.headers)
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.json()['detail']['code'],'OBSERVATION_NOT_FOUND')

    def test_no_new_public_task_list(self):
        self.assertEqual(self.client.get('/api/trip/v4/tasks').status_code,405)
