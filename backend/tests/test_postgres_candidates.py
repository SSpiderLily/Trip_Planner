"""地点候选真实PG回归；全部合成数据位于每次随机创建的独立schema。"""
from datetime import datetime, timezone
import hashlib
import os
import time
import unittest
import uuid
from unittest.mock import patch

from app.models.planning_v4.facts import FactSource, PlaceFact
from app.services.destination_candidate_repository import DestinationCandidateNotFound, DestinationCandidateRepository
from app.services.relational_storage import SCHEMAS


class CandidateStorageSelectionTests(unittest.TestCase):
    def test_postgres_url_does_not_become_a_local_database_path(self):
        repository = DestinationCandidateRepository("postgresql://localhost/trip_validation")
        self.assertIsNone(repository.path)


@unittest.skipUnless(os.environ.get("TRIP_TEST_DATABASE_URL"), "未设置TRIP_TEST_DATABASE_URL，跳过真实PostgreSQL候选测试")
class PostgresCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        cls.database_url = os.environ["TRIP_TEST_DATABASE_URL"]
        try:
            database_name = conninfo_to_dict(cls.database_url).get("dbname", "").lower()
        except Exception:
            raise RuntimeError("真实数据库测试连接配置无效") from None
        if database_name != "personal_web_dev" and "validation" not in database_name and "test" not in database_name:
            raise RuntimeError("真实数据库测试仅允许指定开发数据库或test/validation数据库")
        cls.schema = "trip_destination_test_" + uuid.uuid4().hex
        schema_patch = patch.dict(SCHEMAS, {"destination": cls.schema})
        schema_patch.start()
        cls.addClassCleanup(schema_patch.stop)
        cls.repository = DestinationCandidateRepository(cls.database_url, ttl_seconds=60)
        cls.addClassCleanup(cls.cleanup_test_schema)
        cls.repository.initialize()

    @classmethod
    def cleanup_test_schema(cls):
        from psycopg import sql
        # 唯一DROP目标来自本测试生成的随机标识，不读取生产schema名称。
        connection = cls.repository._connect()
        try:
            connection.raw.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(cls.schema)))
        finally:
            connection.close()

    def setUp(self):
        self.task_id = "pg-candidate-test-" + uuid.uuid4().hex
        self.owner = dict(task_id=self.task_id, plan_id="synthetic-plan", access_token="synthetic-access")
        self.now = time.time()
        self.source = FactSource(tool_call_id="synthetic-search", tool_name="maps_text_search", api_version="v3",
            queried_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
            effective_params={"keywords": "合成公园", "city": "上海"})
        self.place = PlaceFact(fact_id="synthetic-place", poi_id="synthetic-poi", name="合成公园",
            location={"lng": 121.49, "lat": 31.24}, city="上海市", address="合成地址",
            cost={"raw": "20元/人", "value": "20"}, photos=("https://example.invalid/photo",),
            source_refs=(self.source.tool_call_id,), field_sources={"cost": self.source.tool_call_id})
    def store(self, **changes):
        arguments = dict(self.owner, search_id="synthetic-search", category="sightseeing",
                         places=(self.place,), source=self.source, now=self.now)
        arguments.update(changes)
        return self.repository.store_search(**arguments)[0]

    def test_candidate_round_trip_keeps_all_facts_sources_and_hashed_tokens_after_reopen(self):
        token = self.store()
        reopened = DestinationCandidateRepository(self.database_url)
        reopened.initialize()
        stored = reopened.get_candidate(token, **self.owner, now=self.now)
        self.assertEqual(stored.place, self.place)
        self.assertEqual(stored.sources, (self.source,))
        self.assertEqual(stored.category, "sightseeing")
        self.assertEqual(stored.search_id, "synthetic-search")
        self.assertAlmostEqual(stored.expires_at.timestamp(), self.now + 60, places=5)
        connection = reopened._connect()
        try:
            row = connection.execute("SELECT candidate_token_hash,access_token_hash FROM destination_search_candidates WHERE task_id=?", (self.task_id,)).fetchone()
            self.assertEqual(row["candidate_token_hash"], hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(row["access_token_hash"], hashlib.sha256(b"synthetic-access").hexdigest())
            self.assertNotIn(token, row.values())
            self.assertNotIn("synthetic-access", row.values())
        finally:
            connection.close()

    def test_candidate_cannot_be_read_by_another_task_plan_or_access_token(self):
        token = self.store()
        for field, value in (("task_id", "other-task"), ("plan_id", "other-plan"), ("access_token", "other-access")):
            with self.subTest(field=field):
                owner = dict(self.owner, **{field: value})
                with self.assertRaises(DestinationCandidateNotFound):
                    self.repository.get_candidate(token, **owner, now=self.now)
        self.assertEqual(self.repository.get_candidate(token, **self.owner, now=self.now).place, self.place)

    def test_candidate_expires_at_the_exact_ttl_boundary(self):
        token = self.store()
        self.repository.get_candidate(token, **self.owner, now=self.now + 59.999)
        with self.assertRaises(DestinationCandidateNotFound):
            self.repository.get_candidate(token, **self.owner, now=self.now + 60)
        connection = self.repository._connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM destination_search_candidates WHERE task_id=?", (self.task_id,)).fetchone()[0], 0)
        finally:
            connection.close()

    def test_failed_batch_rolls_back_and_later_writes_still_work(self):
        import psycopg
        original = self.store()
        with self.assertRaises(psycopg.IntegrityError):
            self.store(category="invalid-category", now=self.now + 60)
        # 失败的写入也必须回滚同一事务内的过期清理。
        self.assertEqual(self.repository.get_candidate(original, **self.owner, now=self.now).place, self.place)
        token = self.store()
        self.assertNotEqual(original, token)
        self.assertEqual(self.repository.get_candidate(token, **self.owner, now=self.now).place, self.place)
