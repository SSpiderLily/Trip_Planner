"""用户目的地搜索服务的事实来源、类别、并发入口和 sidecar 回归。"""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from app.models.planning_v4.destination_search import DestinationSearchRequest
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.destination_candidate_repository import (
    DestinationCandidateNotFound,
    DestinationCandidateRepository,
)
from app.services.destination_search_service import DestinationSearchService
from app.services.observation_service import ObservationService


SEARCH_SOURCE = {
    "api_version": "v3",
    "queried_at": "2026-10-11T02:00:00+00:00",
    "effective_params": {
        "keywords": "城市广场",
        "city": "上海",
        "citylimit": "true",
        "page": "1",
        "offset": "20",
        "extensions": "all",
    },
}


def poi(poi_id="poi-1", name="滨江广场", *, city="上海市", location="121.490,31.240",
        type_name="风景名胜;城市广场", typecode="110000", cost="20元/人", opentime="全天开放"):
    return {
        "id": poi_id,
        "name": name,
        "cityname": city,
        "location": location,
        "type": type_name,
        "typecode": typecode,
        "cost": cost,
        "opentime": opentime,
        "address": "上海市黄浦区江边路1号",
    }


def search_payload(rows, *, status="ok", count=None):
    return {
        "result_status": status,
        "result_source": SEARCH_SOURCE,
        "count": str(len(rows) if count is None else count),
        "pois": rows,
    }


class FakeExecutor:
    def __init__(self, payload=None, *, block=False):
        self.payload = payload
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_cancel_event = None
        self.block = block

    def run(self, tool_name, arguments, *, timeout_seconds, cancel_event, priority):
        self.calls.append((tool_name, dict(arguments), timeout_seconds, priority))
        self.seen_cancel_event = cancel_event
        self.started.set()
        if self.block:
            self.release.wait(3)
        return self.payload


class RecordingObservationRepository:
    max_bytes = 10_000

    def __init__(self):
        self.spans = []

    def size(self):
        return 0

    def begin_span(self, row):
        self.spans.append({"begin": row})

    def end_span(self, span_id, status, output, truncated, error, usage):
        self.spans.append({"end": (span_id, status, output, truncated, error, usage)})

    def mark_incomplete(self, task_id):
        raise AssertionError("观测失败不应影响业务结果")


class DestinationSearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "destination-candidates.sqlite3"
        self.repository = DestinationCandidateRepository(self.database, ttl_seconds=60)
        self.repository.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, payload=None, *, executor=None, observation_service=None):
        fake = executor or FakeExecutor(payload)
        return DestinationSearchService(
            self.repository,
            tool_executor=fake,
            observation_service=observation_service,
            tool_timeout_seconds=3,
        ), fake

    async def search(self, service, *, category="sightseeing", city="上海", keywords="  城市  广场 ", page=1):
        return await service.search_async(
            task_id="task-1",
            plan_id="plan-1",
            access_token="access-secret-fixture",
            city=city,
            request=DestinationSearchRequest(keywords=keywords, category=category, page=page),
        )

    async def test_search_uses_shared_executor_and_returns_sourced_candidate(self):
        service, executor = self.service(search_payload([poi()]))

        result = await self.search(service, page=2)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.keywords, "城市 广场")
        self.assertEqual(result.candidates[0].name, "滨江广场")
        self.assertEqual(result.candidates[0].location.lng, 121.490)
        self.assertEqual(result.candidates[0].cost.value, 20)
        self.assertEqual(result.candidates[0].source.tool_name, "maps_text_search")
        self.assertEqual(result.candidates[0].source.effective_params["citylimit"], "true")
        self.assertTrue(result.candidates[0].selectable)
        self.assertEqual(len(executor.calls), 1)
        name, arguments, timeout, priority = executor.calls[0]
        self.assertEqual(name, "maps_text_search")
        self.assertEqual(arguments["city"], "上海")
        self.assertEqual(arguments["citylimit"], "true")
        self.assertEqual(arguments["page"], "2")
        self.assertEqual(arguments["offset"], "20")
        self.assertEqual(arguments["extensions"], "all")
        self.assertEqual(timeout, 3)
        self.assertEqual(priority, "interactive")

    async def test_meal_contract_filters_by_provider_category_and_not_ui_label(self):
        service, executor = self.service(search_payload([
            poi("food-1", "老味道餐厅", type_name="餐饮服务;中餐厅", typecode="050100"),
            poi("sight-1", "滨江广场"),
        ]))

        result = await self.search(service, category="meal", keywords="本帮菜")

        self.assertEqual([item.name for item in result.candidates], ["老味道餐厅"])
        self.assertEqual(result.candidates[0].selectable, True)
        self.assertEqual(result.omitted_count, 1)
        self.assertEqual(executor.calls[0][1]["keywords"], "本帮菜")
        with self.assertRaises(Exception):
            DestinationSearchRequest(keywords="餐馆", category="restaurant")

    async def test_search_displays_but_disables_wrong_city_missing_location_and_unverified_category(self):
        rows = [
            poi("wrong-city", "外地景点", city="杭州市"),
            poi("no-location", "无坐标景点", location=None),
            poi("unknown-type", "普通建筑", type_name="商务住宅;住宅区", typecode="120000"),
            poi("valid", "留园", type_name="风景名胜;公园", typecode="110000"),
        ]
        service, _ = self.service(search_payload(rows))

        result = await self.search(service)

        self.assertEqual(
            {item.name for item in result.candidates},
            {"外地景点", "无坐标景点", "普通建筑", "留园"},
        )
        blocked = {item.name: item for item in result.candidates if not item.selectable}
        self.assertEqual(blocked["外地景点"].selection_block_reason, "city_mismatch")
        self.assertEqual(blocked["无坐标景点"].selection_block_reason, "location_unavailable")
        self.assertEqual(blocked["普通建筑"].selection_block_reason, "category_unverified")
        self.assertTrue(all(item.candidate_token is None for item in blocked.values()))
        self.assertTrue(next(item for item in result.candidates if item.name == "留园").selectable)
        self.assertEqual(result.omitted_count, 0)
        self.assertEqual(self.repository.count(), 1)

    async def test_invalid_owner_is_rejected_before_mcp(self):
        service, executor = self.service(search_payload([poi()]))

        with self.assertRaises(ValueError):
            await service.search_async(
                task_id=" ", plan_id="plan-1", access_token="access-secret-fixture", city="上海",
                request=DestinationSearchRequest(keywords="景点", category="sightseeing"),
            )

        self.assertEqual(executor.calls, [])

    async def test_search_and_candidate_resolution_use_task_observation_spans(self):
        observation_repository = RecordingObservationRepository()
        observation = ObservationService(observation_repository)
        service, _ = self.service(
            search_payload([poi()]), observation_service=observation,
        )

        result = await self.search(service)
        service.resolve_candidate(
            result.candidates[0].candidate_token,
            task_id="task-1", plan_id="plan-1", access_token="access-secret-fixture",
        )

        starts = [item["begin"] for item in observation_repository.spans if "begin" in item]
        ends = [item["end"] for item in observation_repository.spans if "end" in item]
        self.assertEqual([row[3] for row in starts], ["destination.search", "destination.resolve_candidate"])
        self.assertEqual([row[4] for row in starts], ["planning", "validation"])
        self.assertTrue(all("access-secret-fixture" not in str(row) for row in starts + ends))
        self.assertTrue(all("candidate_token" not in str(row) for row in starts + ends))

    async def test_conflicting_duplicate_poi_is_not_silently_overwritten(self):
        service, _ = self.service(search_payload([
            poi("duplicate", "第一版本"),
            poi("duplicate", "第二版本"),
            poi("valid", "另一个广场"),
        ]))

        result = await self.search(service)

        self.assertEqual([item.name for item in result.candidates], ["另一个广场"])
        self.assertEqual(result.omitted_count, 1)

    async def test_empty_and_provider_error_are_typed_without_candidates(self):
        empty_service, _ = self.service(search_payload([], status="empty"))
        empty = await self.search(empty_service)
        self.assertEqual(empty.status, "empty")
        self.assertEqual(empty.candidates, ())

        error_payload = search_payload([], status="error")
        error_payload["result_error"] = {"category": "rate_limit", "provider_code": "LIMIT"}
        error_service, _ = self.service(error_payload)
        error = await self.search(error_service)
        self.assertEqual(error.status, "error")
        self.assertEqual(error.error.category, "rate_limit")
        self.assertEqual(error.candidates, ())

    async def test_candidate_token_is_bound_and_resolves_source_closed_reusable_candidate(self):
        service, _ = self.service(search_payload([poi()]))
        result = await self.search(service)
        token = result.candidates[0].candidate_token

        first = service.resolve_candidate(
            token, task_id="task-1", plan_id="plan-1", access_token="access-secret-fixture",
        )
        second = service.resolve_candidate(
            token, task_id="task-1", plan_id="plan-1", access_token="access-secret-fixture",
        )

        self.assertEqual(first.candidate, second.candidate)
        self.assertEqual(first.candidate.slot_ids, ())
        self.assertEqual(first.candidate.category, "sightseeing")
        self.assertEqual(first.candidate.poi_id, first.place.poi_id)
        self.assertEqual(first.candidate.place_fact_id, first.place.fact_id)
        self.assertEqual(first.place.source_refs, (first.sources[0].tool_call_id,))

        for key in (
            {"task_id": "other-task", "plan_id": "plan-1", "access_token": "access-secret-fixture"},
            {"task_id": "task-1", "plan_id": "other-plan", "access_token": "access-secret-fixture"},
            {"task_id": "task-1", "plan_id": "plan-1", "access_token": "wrong-access"},
        ):
            with self.assertRaises(DestinationCandidateNotFound):
                service.resolve_candidate(token, **key)

    async def test_sidecar_hashes_tokens_and_expires_candidates(self):
        service, _ = self.service(search_payload([poi()]))
        result = await self.search(service)
        token = result.candidates[0].candidate_token
        self.assertEqual(self.repository.count(), 1)

        with sqlite3.connect(self.database) as conn:
            row = conn.execute(
                "SELECT candidate_token_hash,access_token_hash FROM destination_search_candidates"
            ).fetchone()
        self.assertNotIn(token, row)
        self.assertNotIn("access-secret-fixture", row)

        stored = self.repository.get_candidate(
            token,
            task_id="task-1",
            plan_id="plan-1",
            access_token="access-secret-fixture",
        )
        expires_at = stored.expires_at.timestamp()
        self.assertEqual(self.repository.cleanup_expired(now=expires_at - 1), 0)
        self.assertEqual(self.repository.cleanup_expired(now=expires_at + 1), 1)
        self.assertEqual(self.repository.count(), 0)
        with self.assertRaises(DestinationCandidateNotFound):
            service.resolve_candidate(
                token, task_id="task-1", plan_id="plan-1", access_token="access-secret-fixture",
            )

    async def test_cancellation_signals_shared_executor_and_does_not_store_late_result(self):
        executor = FakeExecutor(search_payload([poi()]), block=True)
        service, _ = self.service(executor=executor)
        task = asyncio.create_task(self.search(service))
        self.assertTrue(await asyncio.to_thread(executor.started.wait, 2))

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(executor.seen_cancel_event.is_set())
        executor.release.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.repository.count(), 0)


class DestinationSearchModelTests(unittest.TestCase):
    def test_request_rejects_client_city_or_restaurant_category_fields(self):
        with self.assertRaises(Exception):
            DestinationSearchRequest(keywords="外滩", category="sightseeing", city="北京")
        with self.assertRaises(Exception):
            DestinationSearchRequest(keywords="餐馆", category="restaurant")
        request = DestinationSearchRequest(keywords="景点", category="sightseeing", page=3)
        self.assertEqual(request.page, 3)

