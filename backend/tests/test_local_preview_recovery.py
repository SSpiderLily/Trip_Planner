"""本机恢复只更换凭据，不能重跑或修改行程。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes.trip import router
from app.services.preview_repository import PreviewRepository
from test_preview_task_service import snapshot_for


class LocalRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = PreviewRepository(Path(self.tmp.name) / 'preview.sqlite3')
        self.repo.initialize()
        self.repo.create_job('synthetic-task', 'synthetic-plan', 'synthetic-original-token')
        self.repo.complete_job('synthetic-task', snapshot_for('synthetic-plan'))
        self.app = FastAPI()
        self.app.include_router(router, prefix='/api')
        # No pipeline/task submitter: recovery must work without calling planning.
        self.app.state.preview_task_service = SimpleNamespace(repository=self.repo)
        self.url = '/api/trip/v4/tasks/synthetic-task/recover-local-access'
        self.headers = {'X-Trip-Local-Recovery': '1', 'Origin': 'http://127.0.0.1:5173'}

    def client(self, host='127.0.0.1', base='http://127.0.0.1:8000'):
        return TestClient(self.app, base_url=base, client=(host, 50000))

    def test_recover_rotates_both_hashes_without_changing_snapshot_or_versions(self):
        before = self.repo.get_preview('synthetic-original-token')
        old_status = self.repo.job_status('synthetic-task', 'synthetic-original-token')
        with self.client() as client:
            response = client.post(self.url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        access = response.json()
        self.assertEqual(access['task_id'], 'synthetic-task')
        self.assertEqual(access['plan_id'], 'synthetic-plan')
        after = self.repo.get_preview(access['access_token'])
        self.assertEqual(before, after)
        self.assertEqual(old_status, self.repo.job_status('synthetic-task', access['access_token']))
        self.assertIsNone(self.repo.get_preview('synthetic-original-token'))
        self.assertIsNone(self.repo.job_status('synthetic-task', 'synthetic-original-token'))

    def test_rejects_remote_host_origin_and_missing_header(self):
        attempts = [('192.0.2.1', 'http://127.0.0.1:8000', self.headers),
                    ('127.0.0.1', 'http://example.invalid:8000', self.headers),
                    ('127.0.0.1', 'http://127.0.0.1:8000', {**self.headers, 'Origin': 'https://example.invalid'}),
                    ('127.0.0.1', 'http://127.0.0.1:8000', {})]
        for host, base, headers in attempts:
            with self.client(host, base) as client:
                self.assertEqual(client.post(self.url, headers=headers).status_code, 403)
        self.assertIsNotNone(self.repo.get_preview('synthetic-original-token'))

    def test_missing_and_running_tasks_cannot_recover(self):
        self.repo.create_job('running-task', 'running-plan', 'running-token')
        with self.client() as client:
            for task in ['missing-task', 'running-task']:
                response = client.post(f'/api/trip/v4/tasks/{task}/recover-local-access', headers=self.headers)
                self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.repo.job_status('running-task', 'running-token'))
