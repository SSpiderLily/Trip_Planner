"""真实应用启停：临时数据库与配置替身，不调用外部模型或地图。"""
import contextlib
import io
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
            settings = Settings(_env_file=None, task_db_path=str(database))
            with patch.object(main, 'settings', settings), patch.object(main, 'validate_config'), \
                    patch.object(main, 'print_config'), contextlib.redirect_stdout(io.StringIO()):
                with TestClient(main.app) as client:
                    self.assertTrue(database.is_file())
                    self.assertEqual(client.get('/health').status_code, 200)
                    self.assertEqual(client.get('/api/trip/tasks').json(), [])
                    self.assertIsNotNone(main.app.state.day_edit_service)
                    close = AsyncMock(wraps=main.app.state.task_service.close)
                    main.app.state.task_service.close = close
                close.assert_awaited_once()

    def test_invalid_config_prevents_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'tasks.sqlite3'
            settings = Settings(_env_file=None, task_db_path=str(database))
            with patch.object(main, 'settings', settings), patch.object(main, 'print_config'), \
                    patch.object(main, 'validate_config', side_effect=ValueError('invalid config')), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'invalid config'):
                    with TestClient(main.app):
                        self.fail('配置失败时不能完成启动')
            self.assertFalse(database.exists())
