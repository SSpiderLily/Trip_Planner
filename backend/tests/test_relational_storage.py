"""连接边界的离线契约；不以此替代真实PostgreSQL集成验收。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.services.relational_storage import StorageDatabase, StorageRow, postgres_parameters

class RelationalStorageTests(unittest.TestCase):
    def test_parameter_conversion_preserves_quoted_question_marks(self):
        self.assertEqual(postgres_parameters("SELECT '?', ?, '100%', \"a?b\", 'it''s ?'"),
                         "SELECT '?', %s, '100%%', \"a?b\", 'it''s ?'")

    def test_storage_rows_support_names_and_ordinals(self):
        row=StorageRow(('name','count'),('task',3))
        self.assertEqual(row[0],'task');self.assertEqual(row['count'],3)
        self.assertEqual(dict(row),{'name':'task','count':3})

    def test_explicit_sqlite_fixture_keeps_transactions_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as d:
            db=StorageDatabase(Path(d)/'fixture.sqlite3','observation')
            self.assertFalse(db.is_postgres)
            db.initialize('CREATE TABLE example(id TEXT PRIMARY KEY, value TEXT);')
            conn=db.connect(autocommit=False)
            with self.assertRaises(RuntimeError):
                with conn:conn.execute('INSERT INTO example VALUES (?,?)',('a','kept?'));raise RuntimeError('rollback')
            self.assertEqual(conn.execute('SELECT count(*) FROM example').fetchone()[0],0);conn.close()

    def test_namespace_is_closed_and_dsn_is_not_in_errors(self):
        with self.assertRaises(ValueError):StorageDatabase('postgresql://user:synthetic-secret@localhost/trip','arbitrary')
        db=StorageDatabase('postgresql://user:synthetic-secret@localhost/trip','observation')
        with patch('psycopg.connect',side_effect=RuntimeError('synthetic-secret')):
            with self.assertRaisesRegex(RuntimeError,'数据库连接失败') as raised:db.connect()
        self.assertNotIn('synthetic-secret',str(raised.exception))
