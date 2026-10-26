"""任务观测PostgreSQL契约；真实测试仅允许指定开发库或测试库，且使用随机schema。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from uuid import uuid4
from urllib.parse import urlsplit, unquote

from app.services.task_repository import TaskRepository
from app.services.task_diagnostics import build_task_report


class TaskStorageBoundaryTests(unittest.TestCase):
    def test_postgres_url_is_not_treated_as_a_path(self):
        repo = TaskRepository('postgresql://localhost/trip_validation')
        self.assertTrue(repo.database.is_postgres)
        self.assertIsNone(repo.path)

    def test_postgres_size_reads_single_counter_without_scanning_payloads(self):
        repo = TaskRepository('postgresql://localhost/trip_validation')
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (417,)
        with patch.object(repo, 'connection') as factory:
            factory.return_value.__enter__.return_value = connection
            self.assertEqual(repo.size(), 417)
        connection.execute.assert_called_once_with('SELECT logical_bytes FROM storage_usage WHERE singleton=1')

    def test_sqlite_fixture_keeps_atomic_result_and_read_only_report(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / 'tasks.sqlite3')
            self.assertFalse(repo.database.is_postgres)
            repo.initialize()
            repo.create('task', {'city': '上海'})
            repo.start('task')
            repo.succeed('task', {'answer': [1, '上海']}, incomplete=True)
            self.assertEqual(repo.result('task'), {'answer': [1, '上海']})
            self.assertEqual(build_task_report(repo.path, 'task')['task']['observation_incomplete'], 1)


@unittest.skipUnless(os.environ.get('TRIP_TEST_DATABASE_URL'), '未配置TRIP_TEST_DATABASE_URL，未执行真实PostgreSQL验收')
class PostgresTaskTests(unittest.TestCase):
    def setUp(self):
        from app.services.relational_storage import SCHEMAS
        self.url = os.environ['TRIP_TEST_DATABASE_URL']
        database_name = unquote(urlsplit(self.url).path.lstrip('/'))
        if database_name != 'personal_web_dev' and not any(word in database_name.lower() for word in ('test', 'validation')):
            self.fail('拒绝操作：真实PostgreSQL测试只允许personal_web_dev或名称包含test/validation的独立数据库')
        self.schema = 'trip_tasks_test_' + uuid4().hex
        self.schema_patch = patch.dict(SCHEMAS, {'observation': self.schema})
        self.schema_patch.start()
        self.addCleanup(self.schema_patch.stop)
        self.addCleanup(self._drop_schema)
        self.repo = TaskRepository(self.url)
        self.repo.initialize(recover_running=False)

    def _drop_schema(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.schema)))

    def task(self, task_id='task'):
        self.repo.create(task_id, {'city': '上海', 'nested': {'kept': True}})
        self.repo.start(task_id)

    def test_state_result_json_and_duplicate_transition(self):
        self.task()
        self.repo.mark_incomplete('task')
        self.repo.succeed('task', {'answer': ['上海', 3]})
        self.assertEqual(self.repo.status('task')['observation_incomplete'], 1)
        self.assertEqual(self.repo.result('task'), {'answer': ['上海', 3]})
        self.assertEqual(self.repo.request('task')['nested'], {'kept': True})
        with self.assertRaises(RuntimeError):
            self.repo.succeed('task', {})
        self.assertEqual(self.repo.result('task'), {'answer': ['上海', 3]})

    def test_valid_json_with_jsonb_incompatible_values_does_not_break_metrics(self):
        self.task()
        for i,payload in enumerate(('{"role":"master","text":"\\u0000"}',
                                    '{"role":"master","number":1e1000000}',
                                    '{"role":"\\u0000"}')):
            self.repo.begin_span(('unusual-'+str(i),'task',None,'llm.native_tool_loop','llm',
                                  '2026-10-26',payload,0))
        rows=self.repo.spans('task')
        self.assertEqual(len(rows),3)
        self.assertEqual(len(self.repo.metric_spans('task')),3)
        self.assertTrue(all(row['role'] is None for row in rows))

    def test_parent_chain_json_guard_tokens_and_read_only_diagnostics(self):
        self.task()
        for span_id, parent, content in (('parent', None, '{"role":"master"}'),
                                         ('broken', 'parent', '{bad'),
                                         ('array', 'parent', '[1]')):
            self.repo.begin_span((span_id, 'task', parent, 'llm', 'llm', '2026-01-01', content, 0))
            self.repo.end_span(span_id, 'succeeded', json.dumps({'finish_reason': 'stop'}), False, None,
                               {'input_tokens': 4, 'output_tokens': 5, 'total_tokens': 9})
        rows = self.repo.spans('task')
        self.assertEqual([row['span_id'] for row in rows], ['parent', 'broken', 'array'])
        self.assertEqual([row['role'] for row in rows], ['master', None, None])
        self.assertEqual(self.repo.metric_spans('task')[0]['total_tokens'], 9)
        report = build_task_report(self.url, 'task')
        self.assertEqual(report['span_count'], 3)
        self.assertEqual(self.repo.status('task')['status'], 'running')
        import psycopg
        from app.services.task_diagnostics import _read_connection
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            with _read_connection(self.url) as (connection, _):
                connection.execute("UPDATE tasks SET status='failed' WHERE task_id=?", ('task',))
        with self.assertRaises(LookupError):
            build_task_report(self.url, 'missing')

    def test_cleanup_uses_logical_size_and_preserves_running(self):
        self.task('running')
        self.task('old')
        self.repo.mark_preview_succeeded('old')
        before = self.repo.size()
        self.assertGreater(before, 0)
        self.repo.max_bytes = 1
        self.repo.cleanup()
        self.assertIsNone(self.repo.status('old'))
        self.assertIsNotNone(self.repo.status('running'))
        self.assertLess(self.repo.size(), before)

    def test_failure_recovery_and_transaction_rollback(self):
        self.task()
        self.repo.begin_span(('span', 'task', None, 'planning', 'planning', '2026-01-01', '{}', 0))
        with self.assertRaises(RuntimeError):
            with self.repo.connection() as connection:
                connection.execute('DELETE FROM tasks WHERE task_id=?', ('task',))
                raise RuntimeError('force rollback')
        self.assertIsNotNone(self.repo.status('task'))
        self.repo.recover()
        self.assertEqual(self.repo.status('task')['status'], 'interrupted')
        self.assertEqual(self.repo.spans('task')[0]['status'], 'interrupted')

    def assert_usage_matches_rows(self):
        with self.repo.connection() as connection:
            actual = sum(connection.execute(
                f'SELECT COALESCE(SUM(octet_length(row_to_json(t)::text)),0) FROM {table} t'
            ).fetchone()[0] for table in ('tasks', 'task_results', 'spans'))
        self.assertEqual(self.repo.size(), actual)
        return actual

    def test_usage_trigger_tracks_insert_update_delete_cascade_and_rollback(self):
        self.assertEqual(self.assert_usage_matches_rows(), 0)
        self.task()
        self.assert_usage_matches_rows()
        self.repo.begin_span(('parent', 'task', None, 'planning', 'planning', '2026-01-01', '{}', 0))
        self.repo.begin_span(('child', 'task', 'parent', 'llm', 'llm', '2026-01-01', '{}', 0))
        self.repo.end_span('child', 'succeeded', json.dumps({'text': '字节' * 40}), False, None)
        self.assert_usage_matches_rows()
        self.repo.succeed('task', {'saved': ['保留', 4]})
        before = self.assert_usage_matches_rows()
        with self.assertRaises(RuntimeError):
            with self.repo.connection() as connection:
                connection.execute('UPDATE task_results SET result_data=? WHERE task_id=?', ('{}', 'task'))
                connection.execute('DELETE FROM tasks WHERE task_id=?', ('task',))
                raise RuntimeError('rollback usage and cascade')
        self.assertEqual(self.assert_usage_matches_rows(), before)
        with self.repo.connection() as connection:
            connection.execute('DELETE FROM tasks WHERE task_id=?', ('task',))
        self.assertEqual(self.assert_usage_matches_rows(), 0)

    def test_usage_truncate_rebuild_and_initialize_repairs_existing_rows(self):
        self.task()
        self.repo.begin_span(('parent', 'task', None, 'planning', 'planning', '2026-01-01', '{}', 0))
        self.repo.succeed('task', {'saved': True})
        before = self.assert_usage_matches_rows()
        with self.repo.connection() as connection:
            connection.execute('TRUNCATE spans')
        self.assertLess(self.assert_usage_matches_rows(), before)
        with self.repo.connection() as connection:
            connection.execute('UPDATE storage_usage SET logical_bytes=0 WHERE singleton=1')
        self.repo.initialize(recover_running=False)
        self.assertGreater(self.assert_usage_matches_rows(), 0)
        with self.repo.connection() as connection:
            connection.execute('TRUNCATE tasks CASCADE')
        self.assertEqual(self.assert_usage_matches_rows(), 0)
