"""观测失败应留下安全原因，且不影响业务执行。"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from app.services.task_repository import TaskRepository
from app.services.observation_service import ObservationService, span


class ObservationFailuresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = TaskRepository(Path(self.tmp.name) / 'tasks.db')
        self.repo.initialize()
        self.repo.create('synthetic-task', {})
        self.recorder = ObservationService(self.repo)

    def test_begin_and_end_failures_are_safe_and_do_not_stop_business(self):
        for method, reason in [('begin_span', 'span_begin_failed'), ('end_span', 'span_end_failed')]:
            error = sqlite3.OperationalError('secret-token synthetic-private-response')
            error.sqlite_errorcode = 5
            output = io.StringIO()
            with patch.object(self.repo, method, side_effect=error), redirect_stdout(output):
                with self.recorder.task('synthetic-task'):
                    with span('synthetic-step', 'tool') as record:
                        record.output = 'business succeeded'
            self.assertEqual(record.output, 'business succeeded')
            self.assertTrue(self.repo.status('synthetic-task')['observation_incomplete'])
            self.assertIn(reason, output.getvalue())
            self.assertIn('OperationalError', output.getvalue())
            self.assertIn('sqlite_code=5', output.getvalue())
            self.assertNotIn('secret-token', output.getvalue())
            self.assertNotIn('synthetic-private-response', output.getvalue())

    def test_capacity_and_missing_parent_have_distinct_reasons(self):
        self.repo.max_bytes = 0
        output = io.StringIO()
        with redirect_stdout(output), self.recorder.task('synthetic-task'):
            with span('parent', 'planning'):
                with span('child', 'tool'):
                    pass
        self.assertIn('capacity_exceeded', output.getvalue())
        self.assertIn('parent_not_saved', output.getvalue())

    def test_disappearing_wal_does_not_drop_span(self):
        original = Path.stat
        def disappearing(path, *args, **kwargs):
            if str(path).endswith('-wal'):
                raise FileNotFoundError()
            return original(path, *args, **kwargs)
        with patch.object(Path, 'exists', return_value=True), patch.object(Path, 'stat', disappearing):
            self.assertGreater(self.repo.size(), 0)

    def test_console_failure_does_not_interrupt_business(self):
        with patch('builtins.print', side_effect=BrokenPipeError()):
            self.recorder.missing('synthetic-task', 'capacity_exceeded')
        self.assertTrue(self.repo.status('synthetic-task')['observation_incomplete'])
