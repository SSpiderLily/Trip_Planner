"""真实PostgreSQL预览契约；无专用测试库时明确跳过，绝不清理生产库。"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from app.models.preview_storage import PreviewConflict
from app.services.preview_repository import PreviewRepository
from app.services import relational_storage
from test_preview_repository import baseline_snapshot, revised_snapshot


class PreviewStorageSelectionTests(unittest.TestCase):
    def test_unsupported_database_url_is_rejected_without_sqlite_fallback(self):
        with self.assertRaises(ValueError):
            PreviewRepository('mysql://invalid/test')


@unittest.skipUnless(os.environ.get('TRIP_TEST_DATABASE_URL'),
                     '未配置TRIP_TEST_DATABASE_URL，真实PostgreSQL预览测试未执行')
class PostgresPreviewTests(unittest.TestCase):
    def setUp(self):
        self.source = os.environ['TRIP_TEST_DATABASE_URL']
        parsed = urlsplit(self.source)
        database_name = unquote(parsed.path.lstrip('/')).lower()
        if parsed.scheme not in ('postgres', 'postgresql') or not (
            database_name == 'personal_web_dev'
            or any(marker in database_name for marker in ('validation', 'test'))
        ):
            raise RuntimeError('仅允许已有开发库personal_web_dev或名称含validation/test的测试库')
        self.schema = 'trip_preview_test_' + uuid.uuid4().hex
        namespace_patch = patch.dict(relational_storage.SCHEMAS, {'preview': self.schema})
        namespace_patch.start()
        self.addCleanup(namespace_patch.stop)
        self.repo = PreviewRepository(self.source)
        self.addCleanup(self._drop_schema)
        self.repo.initialize()
        self.assertIsNone(self.repo.path)
        self.assertTrue(self.repo.database.is_postgres)
        self.snapshot = baseline_snapshot()

    def _drop_schema(self):
        # 仅删除本例生成的schema；不读取或清理默认trip_preview中的任何表。
        from psycopg import sql
        if not self.schema.startswith('trip_preview_test_'):
            raise RuntimeError('拒绝删除非本测试命名空间')
        conn = self.repo.database.connect()
        try:
            conn.raw.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                sql.Identifier(self.schema),
            ))
        finally:
            conn.close()

    def test_json_hash_and_versions_survive_reopen(self):
        created = self.repo.create(self.snapshot)
        reopened = PreviewRepository(self.source)
        reopened.initialize()
        record = reopened.get_preview(created.access_token)
        self.assertEqual(record.snapshot, self.snapshot)
        self.assertEqual(record.store_version, 1)
        self.assertIsNone(reopened.get_preview('wrong-token'))
        with self.repo._read_connection() as conn:
            stored = conn.execute('SELECT access_token_hash FROM preview_drafts').fetchone()[0]
        self.assertNotEqual(stored, created.access_token)
        self.assertEqual(len(stored), 64)

    def test_save_restore_and_receipt_replay_are_atomic(self):
        created = self.repo.create(self.snapshot)
        token = created.access_token
        saved = self.repo.save_final(token, save_id='first-save', source_store_version=1)
        self.assertEqual(saved.status, 'saved')
        updated = revised_snapshot(self.snapshot, 2, 'replacement')
        request = dict(expected_store_version=1, expected_plan_revision=1, command_id='replace')
        first = self.repo.replace_preview(token, updated, **request)
        replay = self.repo.replace_preview(token, updated, **request)
        self.assertEqual(first.record.store_version, 2)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt_store_version, 2)
        restore = dict(restore_id='restore', expected_store_version=2,
                       expected_plan_revision=2, expected_save_id='first-save')
        restored = self.repo.restore_final(token, **restore)
        self.assertEqual(restored.record.store_version, 3)
        self.assertEqual(restored.record.plan_revision, 3)
        self.assertTrue(self.repo.restore_final(token, **restore).replayed)
        self.assertEqual(self.repo.read_final(token).save_id, 'first-save')

    def test_concurrent_replacements_accept_exactly_one_version(self):
        created = self.repo.create(self.snapshot)
        barrier = threading.Barrier(2)
        updated = revised_snapshot(self.snapshot, 2, 'concurrent')

        def replace(index):
            local = PreviewRepository(self.source)
            barrier.wait(timeout=10)
            try:
                return local.replace_preview(
                    created.access_token, updated, expected_store_version=1,
                    expected_plan_revision=1, command_id='writer-' + str(index),
                ).record.store_version
            except PreviewConflict:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replace, (1, 2)))
        self.assertCountEqual(results, [2, 'conflict'])
        self.assertEqual(self.repo.get_preview(created.access_token).store_version, 2)

    def test_job_ready_and_credential_rotation_keep_snapshot(self):
        token = self.repo.issue_access_token()
        plan_id = self.snapshot.preview.draft.plan_id
        self.repo.create_job('postgres-job', plan_id, token)
        self.repo.complete_job('postgres-job', self.snapshot)
        self.assertEqual(self.repo.job_status('postgres-job', token)['status'], 'ready')
        self.assertIsNotNone(self.repo.ready_task_completion('postgres-job'))
        rotated = self.repo.recover_local_access('postgres-job')
        self.assertIsNotNone(rotated)
        self.assertIsNone(self.repo.get_preview(token))
        self.assertEqual(self.repo.get_preview(rotated['access_token']).snapshot, self.snapshot)

    def test_missing_route_claim_with_optional_filter_returns_none(self):
        self.assertIsNone(self.repo.claim_route_job('missing-plan'))
        self.assertIsNone(self.repo.claim_route_job('missing-plan', calculation_id='missing-calc'))

    def test_receipt_limit_prunes_old_receipts_without_losing_current_replay(self):
        self.repo.receipt_limit = 1
        created = self.repo.create(self.snapshot)
        for save_id in ('save-a', 'save-b', 'save-c'):
            self.repo.save_final(created.access_token, save_id=save_id, source_store_version=1)
        with self.repo._read_connection() as conn:
            rows = conn.execute('SELECT mutation_id FROM preview_mutations').fetchall()
        self.assertEqual([row['mutation_id'] for row in rows], ['save-c'])
        self.assertTrue(self.repo.save_final(
            created.access_token, save_id='save-c', source_store_version=1,
        ).replayed)
