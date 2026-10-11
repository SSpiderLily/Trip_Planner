"""真实应用启停：临时数据库与配置替身，不调用外部模型或地图。"""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app.api import main
from app.config import Settings


class AppLifespanTest(unittest.TestCase):
    def test_startup_initializes_services_and_shutdown_closes_them(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'tasks.sqlite3'
            preview_database = Path(folder) / 'previews.sqlite3'
            settings = Settings(_env_file=None, task_db_path=str(database), preview_db_path=str(preview_database))
            with patch.object(main, 'settings', settings), patch.object(main, 'validate_config'), \
                    patch.object(main, 'print_config'), contextlib.redirect_stdout(io.StringIO()):
                with TestClient(main.app) as client:
                    self.assertTrue(database.is_file())
                    self.assertTrue(preview_database.is_file())
                    self.assertEqual(client.get('/health').status_code, 200)
                    self.assertEqual(client.get('/api/trip/tasks').json(), [])
                    self.assertIsNotNone(main.app.state.day_edit_service)
                    self.assertEqual(main.app.state.preview_repository.path, preview_database)
                    self.assertEqual(main.app.state.task_service.preview_completion_resolver,
                                     main.app.state.preview_repository.ready_task_completion)
                    self.assertEqual(main.app.state.task_service.preview_completion_batch_resolver,
                                     main.app.state.preview_repository.ready_task_completions)
                    task_connection = sqlite3.connect(database)
                    preview_connection = sqlite3.connect(preview_database)
                    try:
                        task_tables = {row[0] for row in task_connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        preview_tables = {row[0] for row in preview_connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                    finally:
                        task_connection.close()
                        preview_connection.close()
                    self.assertIn('tasks', task_tables)
                    self.assertNotIn('preview_jobs', task_tables)
                    self.assertIn('preview_jobs', preview_tables)
                    self.assertNotIn('tasks', preview_tables)
                    close = AsyncMock(wraps=main.app.state.task_service.close)
                    main.app.state.task_service.close = close
                close.assert_awaited_once()

    def test_invalid_config_prevents_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'tasks.sqlite3'
            preview_database = Path(folder) / 'previews.sqlite3'
            settings = Settings(_env_file=None, task_db_path=str(database), preview_db_path=str(preview_database))
            with patch.object(main, 'settings', settings), patch.object(main, 'print_config'), \
                    patch.object(main, 'validate_config', side_effect=ValueError('invalid config')), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'invalid config'):
                    with TestClient(main.app):
                        self.fail('配置失败时不能完成启动')
            self.assertFalse(database.exists())
            self.assertFalse(preview_database.exists())

    def test_observation_and_preview_databases_cannot_share_a_path(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'tasks.sqlite3'
            settings = Settings(_env_file=None, task_db_path=str(database), preview_db_path=str(database))
            with patch.object(main, 'settings', settings), patch.object(main, 'validate_config'), \
                    patch.object(main, 'print_config'), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'preview_db_path 必须与 task_db_path 分开'):
                    with TestClient(main.app):
                        self.fail('共享数据库路径必须拒绝启动')
            self.assertFalse(database.exists())
