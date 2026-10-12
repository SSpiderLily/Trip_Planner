"""v4 编辑 API 与异步单日路线任务的离线回归。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.trip import router
from app.models.planning_v4 import (
    FactSource,
    MapCoordinate,
    PlaceFact,
)
from app.models.planning_v4.plan import AddActivity, EditCommand
from app.models.planning_v4_preview import DestinationSelectionRequest
from app.services.current_route_service import (
    CurrentRouteExecution,
    CurrentRouteLegOutcome,
    CurrentRouteModeOutcome,
)
from app.services.destination_candidate_repository import DestinationCandidateRepository
from app.services.destination_search_service import DestinationSearchService
from app.services.planning_v4_preview_edit_service import PlanningV4PreviewEditService
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan
from app.services.planning_v4_validator import validate_preview
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import PreviewPipelineResult, PreviewTaskService
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService
from app.services.observation_service import ObservationService
from app.services.observation_service import span
from test_preview_task_service import FakePipeline, request_body


class FakeDestinationExecutor:
    def __init__(self):
        self.calls = []

    def run(self, tool_name, arguments, *, timeout_seconds, cancel_event, priority):
        self.calls.append((tool_name, dict(arguments), priority))
        return {
            "result_status": "ok",
            "result_source": {
                "api_version": "v3",
                "queried_at": "2026-10-11T12:00:00+08:00",
                "effective_params": {
                    "keywords": arguments["keywords"],
                    "city": arguments["city"],
                    "citylimit": "true",
                    "page": arguments["page"],
                    "offset": arguments["offset"],
                    "extensions": arguments["extensions"],
                },
            },
            "count": "1",
            "pois": [{
                "id": "destination-api-meal-1",
                "name": "江南小馆",
                "cityname": "上海市",
                "location": "121.490,31.240",
                "type": "餐饮服务;中餐厅",
                "typecode": "050100",
                "cost": "80元/人",
                "address": "上海市黄浦区江边路1号",
            }],
        }


class EditPipeline:
    """Remove the stale route leg in the shared baseline fixture."""

    async def run(self, request, *, task_id, plan_id, progress):
        result = await FakePipeline().run(
            request, task_id=task_id, plan_id=plan_id, progress=progress,
        )
        preview = result.snapshot.preview
        draft = preview.draft.model_copy(update={
            "legs": tuple(
                leg for leg in preview.draft.legs
                if leg.leg_id != "synthetic-leg-day2"
            ),
        }, deep=True)
        normalized = preview.model_copy(update={"draft": draft}, deep=True)
        snapshot = result.snapshot.model_copy(update={"preview": normalized}, deep=True)
        return PreviewPipelineResult(
            snapshot=snapshot,
            route_context=result.route_context,
            backfill=result.backfill,
        )


class DepartureEditPipeline(EditPipeline):
    async def run(self, request, *, task_id, plan_id, progress):
        result = await super().run(
            request, task_id=task_id, plan_id=plan_id, progress=progress,
        )
        preview = result.snapshot.preview
        planning_input = preview.planning_input.model_copy(update={
            "request": preview.planning_input.request.model_copy(update={
                "departure_place_query": "上海虹桥站",
            }),
        }, deep=True)
        departure_source = FactSource(
            tool_call_id="edit-test-departure-source",
            tool_name="maps_text_search",
            api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
            effective_params={"keywords": "上海虹桥站"},
        )
        departure_fact = PlaceFact(
            fact_id="edit-test-departure-fact",
            poi_id="edit-test-departure-poi",
            name="上海虹桥站",
            location=MapCoordinate(lng=121.327, lat=31.200),
            city="上海",
            source_refs=(departure_source.tool_call_id,),
            field_sources={
                "name": departure_source.tool_call_id,
                "location": departure_source.tool_call_id,
                "city": departure_source.tool_call_id,
            },
        )
        facts = preview.facts.model_copy(update={
            "sources": (*preview.facts.sources, departure_source),
            "places": (*preview.facts.places, departure_fact),
        }, deep=True)
        draft = preview.draft
        report = validate_daily_plan(
            planning_input, draft, facts, preview.candidates,
            repair_states=preview.validation.repair_states,
        )
        updated = preview.model_copy(update={
            "planning_input": planning_input,
            "draft": draft,
            "facts": facts,
            "validation": report,
            "day_summaries": build_day_summaries(planning_input, draft, facts),
        }, deep=True)
        validate_preview(updated)
        return PreviewPipelineResult(
            snapshot=result.snapshot.model_copy(update={"preview": updated}, deep=True),
            route_context=result.route_context,
            backfill=result.backfill,
        )


class FakeRouteService:
    def __init__(self, *, gate: threading.Event | None = None, fail: bool = False):
        self.gate = gate
        self.fail = fail
        self.calls: list[str] = []

    async def run_async(
        self, planning_input, draft, facts, *, verified_departure_fact_id=None,
        route_context=None, only_day_id=None, only_leg_ids=None,
    ):
        self.calls.append(only_day_id)
        if self.gate is not None and len(self.calls) == 1:
            await asyncio.to_thread(self.gate.wait, 5)
        if self.fail:
            raise TimeoutError("offline timeout")
        legs = []
        leg_results = []
        for leg in draft.legs:
            if leg.day_id != only_day_id or (only_leg_ids is not None and leg.leg_id not in only_leg_ids):
                legs.append(leg)
                continue
            modes = tuple(
                result.model_copy(update={
                    "status": "error",
                    "route_fact_id": None,
                    "option_ids": (),
                    "error_category": "provider_error",
                }) if result.status == "pending" else result
                for result in leg.mode_results
            )
            legs.append(leg.model_copy(update={"mode_results": modes}))
            pending_modes = [mode for mode in leg.mode_results if mode.status == "pending"]
            if pending_modes:
                leg_results.append(CurrentRouteLegOutcome(
                    leg_id=leg.leg_id,
                    day_id=leg.day_id,
                    from_reference_id=leg.from_endpoint.reference_id,
                    to_reference_id=leg.to_endpoint.reference_id,
                    status="queried",
                    modes=tuple(CurrentRouteModeOutcome(
                        leg_id=leg.leg_id,
                        mode=mode.mode,
                        status="error",
                        error_category="provider_error",
                    ) for mode in pending_modes),
                ))
        updated_draft = draft.model_copy(update={"legs": tuple(legs)}, deep=True)
        return CurrentRouteExecution(
            plan_id=draft.plan_id,
            request_id=draft.request_id,
            based_on_revision=draft.revision,
            status="complete",
            draft=updated_draft,
            fact_store=facts,
            leg_results=tuple(leg_results),
        )


class _CancelContext:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.closed = False

    async def aclose(self):
        self.closed = True
        self.cancel_event.set()


class CancellingRouteService(FakeRouteService):
    def __init__(self):
        super().__init__()
        self.contexts: list[_CancelContext] = []
        self.contexts_seen: list[object] = []

    def create_route_context(self, planning_input, plan_id):
        context = _CancelContext()
        self.contexts.append(context)
        return context

    async def run_async(self, planning_input, draft, facts, *, route_context=None,
                        verified_departure_fact_id=None, only_day_id=None, only_leg_ids=None):
        self.contexts_seen.append(route_context)
        if only_day_id == "synthetic-day-1" and len(self.calls) == 0:
            self.calls.append(only_day_id)
            route_context.cancel_event.set()
            raise TimeoutError("first day timed out and cancelled its context")
        return await super().run_async(
            planning_input,
            draft,
            facts,
            route_context=route_context,
            verified_departure_fact_id=verified_departure_fact_id,
            only_day_id=only_day_id, only_leg_ids=only_leg_ids,
        )


class FailingContextFactoryRouteService(FakeRouteService):
    def create_route_context(self, planning_input, plan_id):
        raise RuntimeError("injected context factory failure")


class ObservedMcpRouteService(FakeRouteService):
    async def run_async(self, planning_input, draft, facts, *, verified_departure_fact_id=None,
                        route_context=None, only_day_id=None, only_leg_ids=None):
        with span("tool.mcp_fixture_route", "tool", {
            "tool_name": "maps_direction_walking",
            "day_id": only_day_id,
        }) as record:
            record.output = {"status": "fixture_response"}
            return await super().run_async(
                planning_input,
                draft,
                facts,
                verified_departure_fact_id=verified_departure_fact_id,
                route_context=route_context,
                only_day_id=only_day_id, only_leg_ids=only_leg_ids,
            )


class DepartureAwareRouteService(FakeRouteService):
    def __init__(self):
        super().__init__()
        self.verified_departure_ids: list[str | None] = []

    async def run_async(
        self, planning_input, draft, facts, *, verified_departure_fact_id=None,
        route_context=None, only_day_id=None, only_leg_ids=None,
    ):
        self.verified_departure_ids.append(verified_departure_fact_id)
        target_legs = [leg for leg in draft.legs if leg.day_id == only_day_id]
        if not target_legs or any(
            endpoint.kind == "departure"
            for leg in target_legs for endpoint in (leg.from_endpoint, leg.to_endpoint)
        ):
            raise AssertionError("编辑只能查询目标日住处起点及当前相邻活动")
        return await super().run_async(
            planning_input, draft, facts,
            verified_departure_fact_id=verified_departure_fact_id,
            route_context=route_context, only_day_id=only_day_id, only_leg_ids=only_leg_ids,
        )


class PlanningV4PreviewEditApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks_repo = TaskRepository(Path(self.temp.name) / "tasks.sqlite3")
        self.tasks_repo.initialize()
        self.tasks = TaskService(
            self.tasks_repo,
            ObservationService(self.tasks_repo),
            lambda: None,
        )
        self.previews = PreviewRepository(Path(self.temp.name) / "previews.sqlite3")
        self.previews.initialize()
        self.destination_repository = DestinationCandidateRepository(
            Path(self.temp.name) / "destination-candidates.sqlite3",
            ttl_seconds=60,
        )
        self.destination_repository.initialize()
        self.destination_executor = FakeDestinationExecutor()

    def tearDown(self):
        self.temp.cleanup()

    def make_client(self, route_service: FakeRouteService, *, pipeline=None):
        @asynccontextmanager
        async def lifespan(app):
            yield
            await app.state.preview_edit_service.close()
            await app.state.preview_task_service.close()
            await self.tasks.close()

        app = FastAPI(lifespan=lifespan)
        app.include_router(router, prefix="/api")
        app.state.task_service = self.tasks
        app.state.preview_task_service = PreviewTaskService(
            self.tasks, self.previews, pipeline or EditPipeline(),
        )
        destination_search = DestinationSearchService(
            self.destination_repository,
            tool_executor=self.destination_executor,
            observation_service=self.tasks.observation,
            tool_timeout_seconds=3,
        )
        app.state.destination_search_service = destination_search
        app.state.preview_edit_service = PlanningV4PreviewEditService(
            app.state.preview_task_service,
            self.previews,
            route_service=route_service,
            route_deadline_seconds=10,
            destination_search_service=destination_search,
        )
        app.state.preview_task_service.edit_coordinator = app.state.preview_edit_service
        return app, TestClient(app)

    def submit_ready(self, client: TestClient, request=None):
        accepted_response = client.post(
            "/api/trip/v4/tasks",
            json=(request or request_body()).model_dump(mode="json"),
        )
        self.assertEqual(accepted_response.status_code, 202)
        accepted = accepted_response.json()
        token, task_id = accepted["access_token"], accepted["task_id"]
        headers = {"Authorization": f"Bearer {token}"}
        for _ in range(200):
            state = client.get(f"/api/trip/v4/tasks/{task_id}", headers=headers).json()
            if state["status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(state["status"], "ready")
        return accepted, headers, client.get(
            f"/api/trip/v4/tasks/{task_id}/preview", headers=headers,
        ).json()

    @staticmethod
    def add_command(envelope: dict, *, day_id: str, slot_id: str, command_id: str):
        candidate_id = next(
            item["candidate_id"] for item in envelope["preview"]["candidates"]
            if item["category"] == "sightseeing"
        )
        return EditCommand.model_validate({
            "plan_id": envelope["plan_id"],
            "day_id": day_id,
            "base_revision": envelope["plan_revision"],
            "command_id": command_id,
            "operations": [{
                "type": "add_activity",
                "client_ref": f"client-{command_id}",
                "candidate_ref": candidate_id,
                "slot_id": slot_id,
                "day_id": day_id,
            }],
        })

    def wait_job(self, client, task_id, headers, calculation_id):
        for _ in range(200):
            response = client.get(
                f"/api/trip/v4/tasks/{task_id}/edit-jobs/{calculation_id}",
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            if payload["job"]["status"] in {"completed", "failed", "superseded"}:
                return payload
            time.sleep(0.01)
        self.fail("路线任务未在离线测试期限内结束")

    def wait_running_job(self, client, task_id, headers, calculation_id):
        for _ in range(200):
            response = client.get(
                f"/api/trip/v4/tasks/{task_id}/edit-jobs/{calculation_id}",
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            if payload["job"]["status"] == "running":
                return payload
            if payload["job"]["status"] in {"failed", "completed", "superseded"}:
                self.fail(f"路线任务在等待运行时提前结束：{payload['job']}")
            time.sleep(0.01)
        self.fail("路线任务未进入 running")

    def test_destination_search_then_atomic_select_and_expired_token_replay(self):
        _, client = self.make_client(FakeRouteService())
        with client:
            accepted, headers, initial = self.submit_ready(client)
            task_id = accepted["task_id"]
            search_url = f"/api/trip/v4/tasks/{task_id}/destinations/search"
            search = client.post(search_url, headers=headers, json={
                "keywords": "本帮菜",
                "category": "meal",
                "page": 1,
            })
            self.assertEqual(search.status_code, 200, search.text)
            payload = search.json()
            self.assertEqual(payload["city"], "上海")
            self.assertEqual(payload["category"], "meal")
            self.assertEqual(payload["candidates"][0]["name"], "江南小馆")
            self.assertTrue(payload["candidates"][0]["selectable"])
            self.assertEqual(payload["candidates"][0]["source"]["tool_name"], "maps_text_search")
            candidate_token = payload["candidates"][0]["candidate_token"]
            self.assertEqual(self.destination_executor.calls[0][0], "maps_text_search")

            preview_url = f"/api/trip/v4/tasks/{task_id}/preview"
            unchanged = client.get(preview_url, headers=headers).json()
            self.assertEqual(unchanged["store_version"], initial["store_version"])
            self.assertEqual(unchanged["plan_revision"], initial["plan_revision"])

            planning_days = initial["preview"]["planning_input"]["days"]
            sight_day = next(day for day in planning_days if day["day_id"] == "synthetic-day-1")
            sight_slot = next(slot for slot in sight_day["slots"] if slot["period"] == "afternoon")
            meal_day = next(day for day in planning_days if day["day_id"] == "synthetic-day-3")
            occupied = {
                activity["slot_id"]
                for day in initial["preview"]["draft"]["days"]
                if day["day_id"] == meal_day["day_id"]
                for activity in day["activities"]
            }
            meal_slot = next(
                slot for slot in meal_day["slots"]
                if slot["period"] == "dinner" and slot["eligibility"] != "skipped"
                and slot["slot_id"] not in occupied
            )
            select_url = f"/api/trip/v4/tasks/{task_id}/destinations/select"
            bad_selection = DestinationSelectionRequest(
                candidate_token=candidate_token,
                day_id=sight_day["day_id"],
                slot_id=sight_slot["slot_id"],
                command_id="destination-category-mismatch",
                client_ref="bad-destination-client-ref",
                expected_store_version=initial["store_version"],
                expected_plan_revision=initial["plan_revision"],
            )
            rejected = client.post(select_url, headers=headers, json=bad_selection.model_dump(mode="json"))
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(rejected.json()["detail"]["code"], "CANDIDATE_CATEGORY_MISMATCH")
            after_rejection = client.get(preview_url, headers=headers).json()
            self.assertEqual(after_rejection["store_version"], initial["store_version"])
            self.assertFalse(any(
                place["poi_id"] == "destination-api-meal-1"
                for place in after_rejection["preview"]["facts"]["places"]
            ))

            selection = DestinationSelectionRequest(
                candidate_token=candidate_token,
                day_id=meal_day["day_id"],
                slot_id=meal_slot["slot_id"],
                command_id="destination-select-once",
                client_ref="destination-select-client-ref",
                expected_store_version=initial["store_version"],
                expected_plan_revision=initial["plan_revision"],
            )
            selected = client.post(select_url, headers=headers, json=selection.model_dump(mode="json"))
            self.assertEqual(selected.status_code, 202, selected.text)
            selected_payload = selected.json()
            envelope = selected_payload["envelope"]
            self.assertEqual(envelope["store_version"], initial["store_version"] + 1)
            self.assertEqual(envelope["plan_revision"], initial["plan_revision"] + 1)
            self.assertFalse(selected_payload["replayed"])
            activity_id = selected_payload["changed_activity_ids"][0]
            activity = next(
                activity
                for day in envelope["preview"]["draft"]["days"]
                for activity in day["activities"]
                if activity["activity_id"] == activity_id
            )
            self.assertEqual(activity["activity_source"], "user")
            self.assertEqual(activity["poi_id"], "destination-api-meal-1")
            candidate = next(
                candidate for candidate in envelope["preview"]["candidates"]
                if candidate["candidate_id"] == activity["candidate_ref"]
            )
            self.assertEqual(candidate["category"], "meal")
            place = next(
                place for place in envelope["preview"]["facts"]["places"]
                if place["fact_id"] == candidate["place_fact_id"]
            )
            source_ids = {source["tool_call_id"] for source in envelope["preview"]["facts"]["sources"]}
            self.assertTrue(set(place["source_refs"]).issubset(source_ids))

            with sqlite3.connect(self.destination_repository.path) as connection:
                connection.execute("UPDATE destination_search_candidates SET expires_at=0")
            replay = client.post(select_url, headers=headers, json=selection.model_dump(mode="json"))
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["receipt_store_version"], initial["store_version"] + 1)
            self.assertEqual(len(self.destination_executor.calls), 1)

            if selected_payload["job"] is not None:
                self.wait_job(
                    client,
                    task_id,
                    headers,
                    selected_payload["job"]["calculation_id"],
                )
            before_expired_selection = client.get(preview_url, headers=headers).json()
            expired_token_request = selection.model_copy(update={
                "command_id": "destination-select-expired-token",
                "client_ref": "destination-select-expired-client-ref",
                "expected_store_version": before_expired_selection["store_version"],
                "expected_plan_revision": before_expired_selection["plan_revision"],
            })
            unavailable = client.post(
                select_url,
                headers=headers,
                json=expired_token_request.model_dump(mode="json"),
            )
            self.assertEqual(unavailable.status_code, 422, unavailable.text)
            self.assertEqual(
                unavailable.json()["detail"]["code"],
                "DESTINATION_CANDIDATE_UNAVAILABLE",
            )
            after_expired_selection = client.get(preview_url, headers=headers).json()
            self.assertEqual(after_expired_selection["store_version"], before_expired_selection["store_version"])
            self.assertEqual(after_expired_selection["plan_revision"], before_expired_selection["plan_revision"])
            self.assertEqual(
                after_expired_selection["preview"]["facts"],
                before_expired_selection["preview"]["facts"],
            )

    def test_edit_api_is_bearer_bound_idempotent_and_returns_synced_envelope(self):
        routes = FakeRouteService()
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            command = self.add_command(
                original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                command_id="add-second-day",
            )
            body = {
                "command": command.model_dump(mode="json"),
                "expected_store_version": original["store_version"],
            }
            path = f"/api/trip/v4/tasks/{task_id}/edits"
            first = client.post(path, json=body, headers=headers)
            self.assertEqual(first.status_code, 202, first.text)
            accepted_edit = first.json()
            self.assertFalse(accepted_edit["replayed"])
            self.assertEqual(accepted_edit["envelope"]["plan_revision"], 2)
            self.assertEqual(accepted_edit["job"]["affected_day_ids"], ["synthetic-day-2"])
            self.assertEqual(
                accepted_edit["envelope"]["cost"]["based_on_revision"], 2,
            )
            self.assertEqual(
                accepted_edit["envelope"]["cost"]["based_on_revision"], 2,
            )
            added = next(
                activity for day in accepted_edit["envelope"]["preview"]["draft"]["days"]
                if day["day_id"] == "synthetic-day-2"
                for activity in day["activities"]
                if activity["activity_id"] in accepted_edit["changed_activity_ids"]
            )
            self.assertEqual(added["activity_source"], "user")

            completed = self.wait_job(
                client, task_id, headers, accepted_edit["job"]["calculation_id"],
            )
            self.assertEqual(completed["job"]["status"], "failed")
            self.assertEqual(completed["job"]["error_code"], "ROUTE_PROVIDER_ERROR")
            self.assertEqual(completed["envelope"]["preview"]["calculation"]["pending_leg_ids"], [])
            self.assertEqual(routes.calls, ["synthetic-day-2"])

            replay = client.post(path, json=body, headers=headers)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["receipt_store_version"], 2)
            self.assertEqual(replay.json()["envelope"]["plan_revision"], 2)

            changed = dict(body)
            changed_command = command.model_copy(update={
                "operations": (command.operations[0].model_copy(update={
                    "slot_id": "synthetic-2-morning",
                }),),
            })
            changed["command"] = changed_command.model_dump(mode="json")
            conflict = client.post(path, json=changed, headers=headers)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["detail"]["code"], "IDEMPOTENCY_CONFLICT")
            wrong_bearer = client.get(
                f"/api/trip/v4/tasks/{task_id}/edit-jobs/{accepted_edit['job']['calculation_id']}",
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(wrong_bearer.status_code, 404)

    def test_departure_day_edit_queries_neighbors_without_return_and_retains_input_buffer(self):
        routes = DepartureAwareRouteService()
        _, client = self.make_client(routes, pipeline=DepartureEditPipeline())
        with client:
            departure_request = request_body().model_copy(update={
                "departure_place_query": "上海虹桥站",
            }, deep=True)
            accepted, headers, original = self.submit_ready(client, departure_request)
            task_id = accepted["task_id"]
            self.assertNotIn("timing", original)
            self.assertEqual(original["preview"]["planning_input"]["rules"]["return_buffer_default_s"], 3600)

            response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-3", slot_id="synthetic-3-afternoon",
                        command_id="add-departure-day-neighbor",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertNotIn("timing", response.json()["envelope"])

            result = self.wait_job(
                client, task_id, headers, response.json()["job"]["calculation_id"],
            )
            self.assertEqual(result["job"]["status"], "failed")
            self.assertEqual(routes.verified_departure_ids, [None])
            final_preview = result["envelope"]["preview"]
            self.assertNotIn("timing", result["envelope"])
            self.assertTrue(any(
                place["fact_id"] == "edit-test-departure-fact"
                for place in final_preview["facts"]["places"]
            ))
            self.assertTrue(final_preview["draft"]["legs"])
            self.assertFalse(any(
                endpoint["kind"] == "departure"
                for leg in final_preview["draft"]["legs"]
                for endpoint in (leg["from_endpoint"], leg["to_endpoint"])
            ))
            self.assertEqual(final_preview["planning_input"]["rules"]["return_buffer_default_s"], 3600)

    def test_edit_route_mcp_spans_are_bound_to_originating_task(self):
        _, client = self.make_client(ObservedMcpRouteService())
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            edit = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                        command_id="observe-edit-route",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(edit.status_code, 202, edit.text)
            finished = self.wait_job(
                client, task_id, headers, edit.json()["job"]["calculation_id"],
            )
            self.assertEqual(finished["job"]["status"], "failed")
            spans = self.tasks_repo.spans(task_id)
            edit_span = next(item for item in spans if item["name"] == "planning.v4.edit_routes")
            mcp_span = next(item for item in spans if item["name"] == "tool.mcp_fixture_route")
            self.assertEqual(edit_span["task_id"], task_id)
            self.assertEqual(mcp_span["task_id"], task_id)
            self.assertEqual(mcp_span["parent_span_id"], edit_span["span_id"])
            self.assertEqual(mcp_span["status"], "succeeded")

    def test_observation_binding_failure_does_not_block_committed_edit_or_route_call(self):
        routes = FakeRouteService()
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            observation = self.tasks.observation

            def fail_task_binding(_task_id):
                raise RuntimeError("injected observation failure")

            observation.task = fail_task_binding
            response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                        command_id="observation-failure-edit",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["envelope"]["plan_revision"], 2)
            failed = self.wait_job(
                client, task_id, headers, response.json()["job"]["calculation_id"],
            )
            self.assertEqual(failed["job"]["status"], "failed")
            self.assertEqual(routes.calls, ["synthetic-day-2"])

    def test_new_day_edit_inherits_running_route_scope_and_late_result_cannot_publish(self):
        gate = threading.Event()
        routes = FakeRouteService(gate=gate)
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            first_command = self.add_command(
                original, day_id="synthetic-day-1", slot_id="synthetic-1-afternoon",
                command_id="edit-day-one",
            )
            first_response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": first_command.model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(first_response.status_code, 202, first_response.text)
            first = first_response.json()
            first = self.wait_running_job(
                client, task_id, headers, first["job"]["calculation_id"],
            )
            self.assertEqual(first["job"]["status"], "running")

            second_command = self.add_command(
                first["envelope"], day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                command_id="edit-day-two",
            )
            second_response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": second_command.model_dump(mode="json"),
                    "expected_store_version": first["envelope"]["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(second_response.status_code, 202, second_response.text)
            second = second_response.json()
            self.assertEqual(
                second["job"]["affected_day_ids"],
                ["synthetic-day-1", "synthetic-day-2"],
            )
            self.assertEqual(
                len(second["envelope"]["preview"]["calculation"]["pending_leg_ids"]),
                len([
                    leg for leg in second["envelope"]["preview"]["draft"]["legs"]
                    if leg["day_id"] in {"synthetic-day-1", "synthetic-day-2"}
                    and any(mode["status"] == "pending" for mode in leg["mode_results"])
                ]),
            )

            gate.set()
            result = self.wait_job(
                client, task_id, headers, second["job"]["calculation_id"],
            )
            self.assertEqual(result["job"]["status"], "failed", result["job"])
            self.assertEqual(result["envelope"]["plan_revision"], 3)
            self.assertEqual(result["envelope"]["preview"]["calculation"]["pending_leg_ids"], [])
            self.assertEqual(routes.calls.count("synthetic-day-1"), 2)
            self.assertEqual(routes.calls.count("synthetic-day-2"), 1)
            old_job = client.get(
                f"/api/trip/v4/tasks/{task_id}/edit-jobs/{first['job']['calculation_id']}",
                headers=headers,
            ).json()["job"]
            self.assertEqual(old_job["status"], "superseded")

    def test_timeout_marks_pending_modes_error_and_keeps_editable_draft(self):
        routes = FakeRouteService(fail=True)
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            command = self.add_command(
                original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                command_id="route-failure-edit",
            )
            response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": command.model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            accepted_edit = response.json()
            finished = self.wait_job(
                client, task_id, headers, accepted_edit["job"]["calculation_id"],
            )
            self.assertEqual(finished["job"]["status"], "failed")
            self.assertEqual(finished["job"]["error_code"], "ROUTE_RECALCULATION_TIMEOUT")
            preview = finished["envelope"]["preview"]
            self.assertEqual(preview["calculation"]["current_routes"], "failed")
            self.assertEqual(preview["calculation"]["pending_leg_ids"], [])
            failed_modes = [
                mode for leg in preview["draft"]["legs"]
                if leg["day_id"] == "synthetic-day-2"
                for mode in leg["mode_results"]
            ]
            self.assertTrue(failed_modes)
            self.assertTrue(all(mode["status"] == "error" for mode in failed_modes))
            self.assertTrue(all(mode["error_category"] == "timeout" for mode in failed_modes))

    def test_cancelled_context_is_replaced_between_inherited_day_recalculations(self):
        routes = CancellingRouteService()
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            editor = client.app.state.preview_edit_service
            stale_shared_context = _CancelContext()
            stale_shared_context.cancel_event.set()
            client.app.state.preview_task_service.route_contexts[accepted["plan_id"]] = stale_shared_context
            schedule = editor._ensure_worker

            async def defer_schedule(_plan_id):
                return None

            editor._ensure_worker = defer_schedule
            first = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-1", slot_id="synthetic-1-afternoon",
                        command_id="defer-first-day",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(first.status_code, 202, first.text)
            first_envelope = first.json()["envelope"]
            second = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        first_envelope, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                        command_id="defer-second-day",
                    ).model_dump(mode="json"),
                    "expected_store_version": first_envelope["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(second.status_code, 202, second.text)
            self.assertEqual(
                second.json()["job"]["affected_day_ids"],
                ["synthetic-day-1", "synthetic-day-2"],
            )

            editor._ensure_worker = schedule
            completed = self.wait_job(
                client, task_id, headers, second.json()["job"]["calculation_id"],
            )
            self.assertEqual(completed["job"]["status"], "failed")
            self.assertEqual(routes.calls, ["synthetic-day-1", "synthetic-day-2"])
            self.assertEqual(len(routes.contexts), 2)
            self.assertIs(routes.contexts_seen[0], routes.contexts[0])
            self.assertIs(routes.contexts_seen[1], routes.contexts[1])
            self.assertIsNot(routes.contexts_seen[0], routes.contexts_seen[1])
            self.assertTrue(routes.contexts[0].cancel_event.is_set())
            self.assertTrue(routes.contexts[0].closed)
            self.assertTrue(stale_shared_context.closed)

    def test_route_context_factory_failure_finishes_job_as_editable_failure(self):
        _, client = self.make_client(FailingContextFactoryRouteService())
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                        command_id="context-factory-failure",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            failed = self.wait_job(
                client, task_id, headers, response.json()["job"]["calculation_id"],
            )
            self.assertEqual(failed["job"]["status"], "failed")
            self.assertEqual(failed["job"]["error_code"], "ROUTE_RECALCULATION_FAILED")
            self.assertEqual(
                failed["envelope"]["preview"]["calculation"]["current_routes"], "failed",
            )

    def test_running_job_is_recovered_after_both_publication_attempts_fail(self):
        routes = FakeRouteService()
        _, client = self.make_client(routes)
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            real_publish = self.previews.publish_route_job
            publish_attempts = 0

            def fail_first_two_publications(*args, **kwargs):
                nonlocal publish_attempts
                publish_attempts += 1
                if publish_attempts <= 2:
                    raise RuntimeError("injected route publish failure")
                return real_publish(*args, **kwargs)

            self.previews.publish_route_job = fail_first_two_publications
            response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": self.add_command(
                        original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                        command_id="recover-stuck-running-job",
                    ).model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            calculation_id = response.json()["job"]["calculation_id"]
            editor = client.app.state.preview_edit_service
            for _ in range(200):
                worker = editor.workers.get(accepted["plan_id"])
                if worker is None or worker.done():
                    break
                time.sleep(0.01)
            self.previews.publish_route_job = real_publish

            recovered = self.wait_job(client, task_id, headers, calculation_id)
            self.assertGreaterEqual(publish_attempts, 2)
            self.assertEqual(recovered["job"]["status"], "failed")
            self.assertEqual(
                recovered["envelope"]["preview"]["calculation"]["pending_leg_ids"], [],
            )

    def test_committed_edit_and_pending_restore_survive_scheduler_failure(self):
        _, client = self.make_client(FakeRouteService())
        with client:
            accepted, headers, original = self.submit_ready(client)
            task_id = accepted["task_id"]
            editor = client.app.state.preview_edit_service

            async def fail_schedule(_plan_id):
                raise RuntimeError("injected scheduler failure")

            editor._ensure_worker = fail_schedule
            command = self.add_command(
                original, day_id="synthetic-day-2", slot_id="synthetic-2-afternoon",
                command_id="committed-without-worker",
            )
            edit_response = client.post(
                f"/api/trip/v4/tasks/{task_id}/edits",
                json={
                    "command": command.model_dump(mode="json"),
                    "expected_store_version": original["store_version"],
                },
                headers=headers,
            )
            self.assertEqual(edit_response.status_code, 202, edit_response.text)
            edited = edit_response.json()
            self.assertEqual(edited["envelope"]["store_version"], 2)
            self.assertEqual(edited["job"]["status"], "pending")

            final_url = f"/api/trip/v4/tasks/{task_id}/final"
            saved = client.post(
                f"{final_url}/save",
                json={"save_id": "pending-final", "source_store_version": 2},
                headers=headers,
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            restored = client.post(
                f"{final_url}/restore",
                json={
                    "restore_id": "restore-pending-final",
                    "expected_store_version": 2,
                    "expected_plan_revision": 2,
                    "expected_save_id": "pending-final",
                },
                headers=headers,
            )
            self.assertEqual(restored.status_code, 200, restored.text)
            restored_body = restored.json()
            self.assertEqual(restored_body["envelope"]["store_version"], 3)
            self.assertEqual(restored_body["envelope"]["plan_revision"], 3)
            restored_calc = restored_body["envelope"]["preview"]["calculation"]
            self.assertTrue(restored_calc["calculation_id"].startswith("restore:"))
            self.assertEqual(restored_calc["current_routes"], "pending")

            calculation_id = restored_calc["calculation_id"]
            stored_job = self.previews.get_route_job_by_plan(
                accepted["plan_id"], calculation_id,
            )
            self.assertEqual(stored_job.status, "pending")
            claimed = self.previews.claim_route_job(accepted["plan_id"])
            self.assertEqual(claimed.job.status, "running")
            self.previews.recover_jobs()
            recovered = self.previews.get_route_job_by_plan(
                accepted["plan_id"], calculation_id,
            )
            self.assertEqual(recovered.status, "pending")

            status = client.get(
                f"/api/trip/v4/tasks/{task_id}/edit-jobs/{calculation_id}",
                headers=headers,
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["job"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
