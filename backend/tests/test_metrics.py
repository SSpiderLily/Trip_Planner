import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.trip import router
from app.services.llm_service import UsageAwareLLM
from app.services.metrics_service import build_metrics
from app.services.observation_service import ObservationService, ObservedLLM
from app.services.task_repository import SCHEMA, TaskRepository
from app.services.task_service import TaskService


def row(span_id, parent, name, kind, status='succeeded', start='2026-01-01T00:00:00+00:00',
        end='2026-01-01T00:00:01+00:00', input_tokens=None, output_tokens=None, total_tokens=None):
    return dict(span_id=span_id, parent_span_id=parent, name=name, operation_type=kind, status=status,
                started_at=start, finished_at=end, input_tokens=input_tokens,
                output_tokens=output_tokens, total_tokens=total_tokens)


class MetricsTest(unittest.TestCase):
    def test_roles_outcomes_duration_and_partial_usage(self):
        spans = [
            row('agent', None, 'agent.collect_sights', 'agent'),
            row('one', 'agent', 'llm.invoke', 'llm', input_tokens=10, output_tokens=5, total_tokens=15),
            row('two', 'agent', 'llm.invoke', 'llm', status='failed', input_tokens=3, end=None),
            row('three', None, 'llm.invoke', 'llm', status='interrupted', end=None),
            row('tool', 'agent', 'tool.amap_maps_weather', 'tool', status='failed'),
            row('pending', 'agent', 'tool.custom', 'tool', status='running', end=None),
        ]
        task = dict(status='interrupted', created_at='2026-01-01T00:00:00+00:00', finished_at=None,
                    observation_incomplete=True)
        metrics = build_metrics(task, spans)
        self.assertIsNone(metrics['task']['duration_ms'])
        self.assertEqual(metrics['model']['total'], 3)
        self.assertEqual(metrics['model']['success_rate'], 0.5)
        self.assertEqual(metrics['model']['interrupted'], 1)
        self.assertFalse(metrics['model']['duration_complete'])
        self.assertEqual(metrics['model']['duration_ms'], 1000)
        self.assertEqual(metrics['tokens']['covered_calls'], 1)
        self.assertEqual(metrics['tokens']['input_tokens'], 13)
        self.assertEqual(metrics['tokens']['total_tokens'], 15)
        self.assertFalse(metrics['tokens']['complete'])
        self.assertEqual([item['name'] for item in metrics['model_details']], ['候选搜集', '未归属'])
        self.assertEqual([item['name'] for item in metrics['tool_details']], ['amap_maps_weather', 'custom'])
        self.assertEqual(metrics['tool']['unknown'], 1)

    def test_zero_denominator_and_total_derived_only_from_both_fields(self):
        metrics = build_metrics({'status': 'failed'}, [
            row('one', None, 'llm.invoke', 'llm', status='interrupted', input_tokens=0, output_tokens=2),
            row('two', None, 'llm.invoke', 'llm', status='running', end=None, total_tokens=8),
        ])
        self.assertIsNone(metrics['model']['success_rate'])
        self.assertEqual(metrics['tokens']['total_tokens'], 10)
        self.assertEqual(metrics['tokens']['covered_calls'], 1)

    def test_legacy_database_adds_nullable_usage_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            legacy = SCHEMA.replace(' input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,\n', '')
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(legacy)
                conn.execute("INSERT INTO tasks(task_id,status,request_data,created_at) VALUES ('old','failed','{}','2026-01-01')")
                conn.execute("""INSERT INTO spans(span_id,task_id,name,operation_type,status,started_at)
                    VALUES ('one','old','llm.invoke','llm','failed','2026-01-01')""")
            repo = TaskRepository(path)
            repo.initialize()
            repo.initialize()
            self.assertIsNone(repo.metric_spans('old')[0]['input_tokens'])

    def test_single_provider_call_records_usage_without_putting_it_in_message(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / 'metrics.sqlite3')
            repo.initialize()
            repo.create('task', {})
            class FakeUsageLLM:
                model = 'fixture'
                def __init__(self):
                    self.calls = 0
                def invoke_with_usage(self, messages, **kwargs):
                    self.calls += 1
                    return 'answer', {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
            underlying = FakeUsageLLM()
            observed = ObservedLLM(underlying)
            with ObservationService(repo).task('task'):
                self.assertEqual(observed.invoke([{'role': 'user', 'content': 'hello'}]), 'answer')
            self.assertEqual(underlying.calls, 1)
            stored = repo.metric_spans('task')[0]
            self.assertEqual((stored['input_tokens'], stored['output_tokens'], stored['total_tokens']), (4, 2, 6))
            detail = repo.span('task', stored['span_id'])
            self.assertEqual(detail['output_data'], 'answer')

    def test_project_adapter_uses_one_original_request(self):
        llm = UsageAwareLLM.__new__(UsageAwareLLM)
        llm.model, llm.temperature, llm.max_tokens = 'fixture', 0.7, None
        create = Mock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='answer'))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8)))
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        text, usage = llm.invoke_with_usage([{'role': 'user', 'content': 'hello'}], max_tokens=12)
        self.assertEqual(text, 'answer')
        self.assertEqual(usage.total_tokens, 8)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['max_tokens'], 12)

    def test_metrics_endpoint_respects_task_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / 'metrics.sqlite3')
            repo.initialize()
            repo.create('task', {})
            service = TaskService(repo, ObservationService(repo), lambda: None)
            app = FastAPI()
            app.state.task_service = service
            app.include_router(router, prefix='/api')
            with TestClient(app) as client:
                path = '/api/trip/tasks/task/metrics'
                pending = client.get(path)
                self.assertEqual(pending.status_code, 409)
                self.assertEqual(pending.json()['detail']['code'], 'METRICS_NOT_READY')
                repo.start('task')
                repo.fail('task', 'PLANNING_FAILED', '规划失败')
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['task']['status'], 'failed')
                self.assertEqual(response.json()['model']['total'], 0)
                self.assertIsNone(response.json()['tokens']['total_tokens'])
                self.assertEqual(client.get('/api/trip/tasks/missing/metrics').status_code, 404)


if __name__ == '__main__':
    unittest.main()
