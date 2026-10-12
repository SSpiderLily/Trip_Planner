"""v4 异步预览任务与持久化契约测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.trip import router
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import (
    BackfillProgress, BackfillSuperseded, PreviewPipelineResult, PreviewProgress,
    PreviewTaskService,
)
from app.services.reference_cost import aggregate_reference_costs
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService
from app.services.observation_service import ObservationService
from test_planning_v4_contract import make_full_baseline_preview


def request_body():
    planning_input, _, _, _, _ = make_full_baseline_preview()
    return planning_input.request


def snapshot_for(plan_id: str) -> PreviewSnapshot:
    planning_input, facts, _, _, original = make_full_baseline_preview()
    preview_value = original.model_dump(mode="json")
    preview_value["draft"]["plan_id"] = plan_id
    preview_value["validation"]["plan_id"] = plan_id
    preview = type(original).model_validate(preview_value)
    cost = aggregate_reference_costs(planning_input, preview.draft, facts)
    return PreviewSnapshot(preview=preview, cost=cost)


class FakePipeline:
    def __init__(self, *, fail=False, backfill=None, context=None):
        self.fail = fail
        self.backfill = backfill
        self.context = context

    async def run(self, request, *, task_id, plan_id, progress):
        await progress(PreviewProgress(stage="dispatch", completed=1, total=1, message="已完成"))
        if self.fail:
            raise RuntimeError("expected test failure")
        return PreviewPipelineResult(
            snapshot=snapshot_for(plan_id),
            route_context=self.context or (FakeRouteContext() if self.backfill else None),
            backfill=self.backfill,
        )


class FakeRouteContext:
    def assert_binding(self, planning_input, draft):
        self.bound = (planning_input, draft.plan_id)

    def register_facts(self, facts):
        self.fact_count = len(facts.places)

    async def aclose(self):
        pass


class PreviewTaskApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "tasks.sqlite3"
        self.preview_db = Path(self.temp.name) / "previews.sqlite3"
        self.tasks = TaskRepository(self.db)
        self.tasks.initialize()
        self.observation = ObservationService(self.tasks)
        self.task_service = TaskService(self.tasks, self.observation, lambda: None)
        self.previews = PreviewRepository(self.preview_db)
        self.previews.initialize()
        self.task_service.preview_completion_resolver = self.previews.ready_task_completion
        self.task_service.preview_completion_batch_resolver = self.previews.ready_task_completions

    def tearDown(self):
        self.temp.cleanup()

    def make_client(self, pipeline):
        @asynccontextmanager
        async def lifespan(app):
            yield
            await app.state.preview_task_service.close()
            await self.task_service.close()

        app = FastAPI(lifespan=lifespan)
        app.include_router(router, prefix="/api")
        app.state.task_service = self.task_service
        app.state.preview_task_service = PreviewTaskService(self.task_service, self.previews, pipeline)
        return app, TestClient(app)

    def submit_ready_preview(self, client):
        response = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json"))
        self.assertEqual(response.status_code, 202)
        accepted = response.json()
        token, task_id = accepted["access_token"], accepted["task_id"]
        status = None
        for _ in range(100):
            response = client.get(
                f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
            )
            status = response.json()
            if status["status"] in ("ready", "failed"):
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["preview_ready"])
        return accepted

    def test_old_snapshot_read_and_restore_are_explicitly_rejected_without_conversion(self):
        _, client = self.make_client(FakePipeline())
        with client:
            accepted = self.submit_ready_preview(client)
            token, task_id = accepted["access_token"], accepted["task_id"]
            headers = {"Authorization": f"Bearer {token}"}
            self.previews.save_final(token, save_id="old-save", source_store_version=1)
            with sqlite3.connect(self.preview_db) as connection:
                raw = connection.execute("SELECT preview_json FROM preview_drafts").fetchone()[0]
                value = json.loads(raw)
                value["draft"]["days"][0]["activities"][0]["duration_s"] = 3600
                old_json = json.dumps(value)
                connection.execute("UPDATE preview_drafts SET preview_json=?", (old_json,))
                connection.execute("UPDATE preview_finals SET preview_json=?", (old_json,))
            base = f"/api/trip/v4/tasks/{task_id}"
            responses = [
                client.get(f"{base}/preview", headers=headers),
                client.get(f"{base}/final", headers=headers),
                client.post(f"{base}/final/restore", headers=headers, json={
                    "restore_id": "reject-old", "expected_store_version": 1,
                    "expected_plan_revision": 1, "expected_save_id": "old-save",
                }),
            ]
            for response in responses:
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"], "PREVIEW_CONTRACT_UNSUPPORTED")
                self.assertIn("历史数据不支持当前版本", response.text)
                self.assertNotIn("duration_s", response.text)
            with sqlite3.connect(self.preview_db) as connection:
                row = connection.execute("SELECT preview_json,plan_revision,store_version FROM preview_drafts").fetchone()
                self.assertEqual(row, (old_json, 1, 1))

    def test_final_save_read_and_restore_api_are_bearer_bound_and_idempotent(self):
        _, client = self.make_client(FakePipeline())
        with client:
            accepted = self.submit_ready_preview(client)
            token, task_id, plan_id = accepted["access_token"], accepted["task_id"], accepted["plan_id"]
            headers = {"Authorization": f"Bearer {token}"}
            final_url = f"/api/trip/v4/tasks/{task_id}/final"
            save_url = f"{final_url}/save"
            restore_url = f"{final_url}/restore"

            self.assertEqual(client.get(final_url).status_code, 401)
            self.assertEqual(client.get(final_url, headers={"Authorization": "Bearer wrong"}).status_code, 404)
            empty = client.get(final_url, headers=headers)
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json(), {
                "task_id": task_id,
                "plan_id": plan_id,
                "restorable": False,
                "final": None,
                "unavailable_reason": None,
            })

            saved = client.post(save_url, headers=headers, json={
                "save_id": "api-save-original", "source_store_version": 1,
            })
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["status"], "saved")
            self.assertTrue(saved.json()["restorable"])
            original_final = self.previews.read_final(token)
            self.assertEqual(original_final.save_id, "api-save-original")
            self.assertTrue(client.get(final_url, headers=headers).json()["restorable"])

            forged = client.post(save_url, headers=headers, json={
                "save_id": "api-save-forged", "source_store_version": 1,
                "preview": self.previews.get_preview(token).preview.model_dump(mode="json"),
            })
            self.assertEqual(forged.status_code, 422)

            restore_body = {
                "restore_id": "api-restore-once",
                "expected_store_version": 1,
                "expected_plan_revision": 1,
                "expected_save_id": "api-save-original",
            }
            restored = client.post(restore_url, headers=headers, json=restore_body)
            self.assertEqual(restored.status_code, 200)
            restored_body = restored.json()
            self.assertEqual(set(restored_body), {
                "envelope", "restored_from", "replayed", "receipt_store_version",
            })
            self.assertEqual(restored_body["envelope"]["store_version"], 2)
            self.assertEqual(restored_body["envelope"]["plan_revision"], 2)
            self.assertEqual(restored_body["restored_from"]["save_id"], "api-save-original")
            self.assertFalse(restored_body["replayed"])
            self.assertEqual(self.previews.read_final(token), original_final)

            saved_again = client.post(save_url, headers=headers, json={
                "save_id": "api-save-after-restore", "source_store_version": 2,
            })
            self.assertEqual(saved_again.status_code, 200)
            self.assertEqual(saved_again.json()["status"], "saved")
            replay = client.post(restore_url, headers=headers, json=restore_body)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["receipt_store_version"], 2)
            self.assertEqual(replay.json()["restored_from"]["save_id"], "api-save-original")
            self.assertEqual(replay.json()["envelope"]["store_version"], 2)
            self.assertEqual(self.previews.read_final(token).save_id, "api-save-after-restore")

            changed_final = client.post(restore_url, headers=headers, json={
                "restore_id": "api-restore-changed-final",
                "expected_store_version": 2,
                "expected_plan_revision": 2,
                "expected_save_id": "api-save-original",
            })
            self.assertEqual(changed_final.status_code, 409)
            self.assertEqual(changed_final.json()["detail"]["code"], "FINAL_CHANGED")
            self.assertEqual(changed_final.json()["detail"]["final"]["save_id"], "api-save-after-restore")

            stale_restore = client.post(restore_url, headers=headers, json={
                **restore_body,
                "restore_id": "api-restore-stale",
            })
            self.assertEqual(stale_restore.status_code, 409)
            self.assertEqual(stale_restore.json()["detail"]["code"], "PREVIEW_VERSION_CONFLICT")
            stale_save = client.post(save_url, headers=headers, json={
                "save_id": "api-save-stale", "source_store_version": 1,
            })
            self.assertEqual(stale_save.status_code, 200)
            self.assertEqual(stale_save.json()["status"], "stale")
            self.assertEqual(self.previews.read_final(token).save_id, "api-save-after-restore")

    def test_final_storage_failures_return_retryable_api_error(self):
        _, client = self.make_client(FakePipeline())
        with client:
            accepted = self.submit_ready_preview(client)
            token, task_id = accepted["access_token"], accepted["task_id"]
            headers = {"Authorization": f"Bearer {token}"}
            final_url = f"/api/trip/v4/tasks/{task_id}/final"

            with patch.object(self.previews, "read_final", side_effect=sqlite3.OperationalError("injected read failure")):
                response = client.get(final_url, headers=headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "PREVIEW_STORAGE_UNAVAILABLE")

            with patch.object(self.previews, "restore_final", side_effect=sqlite3.OperationalError("injected restore failure")):
                response = client.post(f"{final_url}/restore", headers=headers, json={
                    "restore_id": "api-restore-failure",
                    "expected_store_version": 1,
                    "expected_plan_revision": 1,
                    "expected_save_id": "saved-before-failure",
                })
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "PREVIEW_STORAGE_UNAVAILABLE")

            with patch.object(self.previews, "save_final", side_effect=sqlite3.OperationalError("injected save failure")):
                response = client.post(f"{final_url}/save", headers=headers, json={
                    "save_id": "api-save-failure", "source_store_version": 1,
                })
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "PREVIEW_STORAGE_UNAVAILABLE")

    def test_legacy_final_without_calculations_is_reported_unavailable(self):
        _, client = self.make_client(FakePipeline())
        with client:
            accepted = self.submit_ready_preview(client)
            token, task_id = accepted["access_token"], accepted["task_id"]
            headers = {"Authorization": f"Bearer {token}"}
            self.previews.save_final(token, save_id="legacy-final", source_store_version=1)
            with sqlite3.connect(self.preview_db) as conn:
                conn.execute(
                    "UPDATE preview_finals SET cost_json=NULL WHERE save_id='legacy-final'"
                )

            state = client.get(f"/api/trip/v4/tasks/{task_id}/final", headers=headers)
            self.assertEqual(state.status_code, 200)
            self.assertFalse(state.json()["restorable"])
            self.assertEqual(state.json()["final"]["save_id"], "legacy-final")
            self.assertEqual(state.json()["unavailable_reason"], "snapshot_incomplete")

            restored = client.post(f"/api/trip/v4/tasks/{task_id}/final/restore", headers=headers, json={
                "restore_id": "restore-incomplete-legacy",
                "expected_store_version": 1,
                "expected_plan_revision": 1,
                "expected_save_id": "legacy-final",
            })
            self.assertEqual(restored.status_code, 409)
            self.assertEqual(restored.json()["detail"]["code"], "FINAL_SNAPSHOT_UNAVAILABLE")

    def test_post_returns_token_early_status_and_preview_survive_reopen_and_final_save(self):
        app, client = self.make_client(FakePipeline())
        with client:
            response = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json"))
            self.assertEqual(response.status_code, 202)
            accepted = response.json()
            self.assertEqual(accepted["status"], "accepted")
            token, task_id, plan_id = accepted["access_token"], accepted["task_id"], accepted["plan_id"]
            self.assertEqual(client.get(f"/api/trip/v4/tasks/{task_id}").status_code, 401)
            self.assertEqual(client.get(
                f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": "Bearer wrong"}
            ).status_code, 404)

            status = None
            for _ in range(100):
                status_response = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                )
                status = status_response.json()
                if status["status"] in ("ready", "failed"):
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["preview_ready"])
            self.assertEqual(status["plan_id"], plan_id)

            response = client.get(
                f"/api/trip/v4/tasks/{task_id}/preview",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["plan_id"], plan_id)
            self.assertEqual(payload["store_version"], 1)
            self.assertIn("preview", payload)
            self.assertNotIn("timing", payload)
            self.assertIn("cost", payload)
            self.assertIsNone(self.tasks.result(task_id))

        reopened = PreviewRepository(self.preview_db)
        reopened.initialize()
        stored = reopened.get_preview(token)
        self.assertEqual(stored.snapshot.preview.draft.plan_id, plan_id)
        saved = reopened.save_final(token, save_id="save-1", source_store_version=1)
        final = reopened.read_final(token)
        self.assertEqual(saved.status, "saved")
        self.assertEqual(final.cost, stored.cost)

        connection = sqlite3.connect(self.preview_db)
        try:
            raw = connection.execute(
                "SELECT access_token_hash FROM preview_jobs WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(raw, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(raw, token)
        self.assertNotIn(token, str(self.tasks.request(task_id)))

    def test_unwired_pipeline_returns_503_without_accepting_a_phantom_job(self):
        _, client = self.make_client(None)
        with client:
            response = client.post(
                "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "PREVIEW_PIPELINE_UNAVAILABLE")
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_public_submit_rejects_client_generated_requirements_and_rules(self):
        _, client = self.make_client(FakePipeline())
        body = request_body().model_dump(mode="json")
        body["requirements"] = {}
        body["rules"] = {}
        with client:
            response = client.post("/api/trip/v4/tasks", json=body)
        self.assertEqual(response.status_code, 422)

    def test_v4_read_storage_failures_return_503(self):
        _, client = self.make_client(None)
        token = self.previews.issue_access_token()
        with patch.object(self.previews, "job_status", side_effect=OSError("read failed")):
            with client:
                response = client.get(
                    "/api/trip/v4/tasks/trip_missing",
                    headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "PREVIEW_STORAGE_UNAVAILABLE")

        token = self.previews.issue_access_token()
        task_id, plan_id = "trip_preview_read_failure", "plan_preview_read_failure"
        self.previews.create_job(task_id, plan_id, token)
        self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
        self.previews.complete_job(task_id, snapshot_for(plan_id))
        with patch.object(self.previews, "get_preview", side_effect=OSError("read failed")):
            with client:
                response = client.get(
                    f"/api/trip/v4/tasks/{task_id}/preview",
                    headers={"Authorization": f"Bearer {token}"},
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "PREVIEW_STORAGE_UNAVAILABLE")

    def test_snapshot_requires_cost_day_sets_to_match_preview(self):
        snapshot = snapshot_for("plan-set-check")
        value = snapshot.model_dump(mode="json")
        value["cost"]["days"].pop()
        with self.assertRaises(ValidationError):
            PreviewSnapshot.model_validate(value)

    def test_preview_not_ready_is_409_and_failed_pipeline_is_visible(self):
        class WaitingPipeline(FakePipeline):
            async def run(self, request, *, task_id, plan_id, progress):
                await asyncio.sleep(0.1)
                return await super().run(request, task_id=task_id, plan_id=plan_id, progress=progress)

        app, client = self.make_client(WaitingPipeline())
        with client:
            accepted = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json")).json()
            task_id, token = accepted["task_id"], accepted["access_token"]
            preview = client.get(
                f"/api/trip/v4/tasks/{task_id}/preview", headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(preview.status_code, 409)
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()
                if status["status"] == "ready":
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "ready")

    def test_business_preview_validation_runs_before_atomic_ready_write(self):
        app, client = self.make_client(FakePipeline())
        original = self.previews.complete_job
        with patch(
            "app.services.planning_v4_validator.validate_preview",
            side_effect=ValueError("business validation failed"),
        ), patch.object(self.previews, "complete_job", wraps=original) as complete:
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if status["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.01)
        self.assertEqual(status["status"], "failed")
        complete.assert_not_called()

    def test_uncommitted_route_context_closes_when_snapshot_write_fails(self):
        closed = []

        class Context(FakeRouteContext):
            async def aclose(self):
                closed.append(True)

        app, client = self.make_client(FakePipeline(context=Context()))
        with patch.object(self.previews, "complete_job", side_effect=RuntimeError("disk failed")):
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if status["status"] == "failed":
                        break
                    time.sleep(0.01)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(closed, [True])

    def test_generation_progress_write_failure_does_not_prevent_ready_preview(self):
        original = self.previews.update_job

        def fail_progress(task_id, **updates):
            if "generation_stage" in updates or "generation_completed" in updates:
                raise OSError("progress store unavailable")
            return original(task_id, **updates)

        app, client = self.make_client(FakePipeline())
        with patch.object(self.previews, "update_job", side_effect=fail_progress):
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if status["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.01)
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["preview_ready"])

    def test_ready_preview_is_success_when_observation_completion_write_fails(self):
        app, client = self.make_client(FakePipeline())
        with patch.object(self.tasks, "mark_preview_succeeded", side_effect=OSError("observation store failed")):
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if status["status"] == "ready":
                        break
                    time.sleep(0.01)
                task_status = client.get(f"/api/trip/tasks/{task_id}").json()
                metrics = client.get(f"/api/trip/tasks/{task_id}/metrics").json()
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["preview_ready"])
        self.assertEqual(task_status["status"], "succeeded")
        self.assertEqual(task_status["persisted_status"], "running")
        self.assertTrue(task_status["observation_incomplete"])
        self.assertEqual(task_status["persistence_error"]["code"], "OBSERVATION_COMPLETION_INCOMPLETE")
        self.assertEqual(metrics["task"]["status"], "succeeded")
        self.assertTrue(metrics["task"]["observation_incomplete"])
        self.assertEqual(self.tasks.status(task_id)["status"], "running")

        async def read_task_lists():
            succeeded = await self.task_service.list_tasks("succeeded")
            running = await self.task_service.list_tasks("running")
            return succeeded, running

        succeeded, running = asyncio.run(read_task_lists())
        self.assertEqual([row["task_id"] for row in succeeded], [task_id])
        self.assertEqual(running, [])

    def test_observation_start_write_failure_still_runs_pipeline_to_ready(self):
        class TrackingPipeline(FakePipeline):
            def __init__(self):
                super().__init__()
                self.called = False

            async def run(self, request, *, task_id, plan_id, progress):
                self.called = True
                return await super().run(request, task_id=task_id, plan_id=plan_id, progress=progress)

        pipeline = TrackingPipeline()
        app, client = self.make_client(pipeline)
        with patch.object(self.tasks, "start", side_effect=OSError("observation start failed")) as start:
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    preview_status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if preview_status["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.01)
                task_status = client.get(f"/api/trip/tasks/{task_id}").json()
                preview = client.get(
                    f"/api/trip/v4/tasks/{task_id}/preview",
                    headers={"Authorization": f"Bearer {token}"},
                )

        start.assert_called_once_with(task_id)
        self.assertTrue(pipeline.called)
        self.assertEqual(preview_status["status"], "ready")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(task_status["status"], "succeeded")
        self.assertTrue(task_status["observation_incomplete"])
        self.assertEqual(task_status["persisted_status"], "accepted")

    def test_observation_start_write_failure_still_records_pipeline_failure(self):
        class TrackingPipeline(FakePipeline):
            def __init__(self):
                super().__init__(fail=True)
                self.called = False

            async def run(self, request, *, task_id, plan_id, progress):
                self.called = True
                return await super().run(request, task_id=task_id, plan_id=plan_id, progress=progress)

        pipeline = TrackingPipeline()
        app, client = self.make_client(pipeline)
        with patch.object(self.tasks, "start", side_effect=OSError("observation start failed")) as start:
            with client:
                accepted = client.post(
                    "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
                ).json()
                task_id, token = accepted["task_id"], accepted["access_token"]
                for _ in range(100):
                    preview_status = client.get(
                        f"/api/trip/v4/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ).json()
                    if preview_status["status"] in ("ready", "failed"):
                        break
                    time.sleep(0.01)
                task_status = client.get(f"/api/trip/tasks/{task_id}").json()

        start.assert_called_once_with(task_id)
        self.assertTrue(pipeline.called)
        self.assertEqual(preview_status["status"], "failed")
        self.assertEqual(preview_status["error_code"], "PLANNING_FAILED")
        self.assertFalse(preview_status["preview_ready"])
        self.assertEqual(task_status["status"], "failed")
        self.assertEqual(task_status["error_code"], "PLANNING_FAILED")

    def test_durable_ready_preview_resolves_observation_recovery_for_status_and_metrics(self):
        task_id, plan_id = "trip_durable_ready", "plan_durable_ready"
        token = self.previews.issue_access_token()
        self.tasks.create(task_id, request_body().model_dump(mode="json"))
        self.tasks.start(task_id)
        self.previews.create_job(task_id, plan_id, token)
        self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
        self.previews.complete_job(task_id, snapshot_for(plan_id))
        self.tasks.recover()

        async def read_status_and_metrics():
            status = await self.task_service.status(task_id)
            metrics = await self.task_service.metrics(task_id)
            return status, metrics

        status, metrics = asyncio.run(read_status_and_metrics())
        self.assertEqual(status["persisted_status"], "interrupted")
        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status["observation_incomplete"])
        self.assertEqual(status["persistence_error"]["code"], "OBSERVATION_COMPLETION_INCOMPLETE")
        self.assertEqual(metrics["task"]["status"], "succeeded")
        self.assertEqual(self.tasks.status(task_id)["status"], "interrupted")

    def test_ready_preview_resolves_interrupted_observation_after_restart(self):
        task_id, plan_id, token = "trip_restart_ready", "plan_restart_ready", self.previews.issue_access_token()
        self.tasks.create(task_id, request_body().model_dump(mode="json"))
        self.tasks.start(task_id)
        self.previews.create_job(task_id, plan_id, token)
        self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
        self.previews.complete_job(task_id, snapshot_for(plan_id))
        self.tasks.recover()

        task_service = TaskService(self.tasks, self.observation, lambda: None)
        task_service.preview_completion_resolver = self.previews.ready_task_completion
        task_service.preview_completion_batch_resolver = self.previews.ready_task_completions

        async def read_status_and_metrics():
            status = await task_service.status(task_id)
            metrics = await task_service.metrics(task_id)
            await task_service.close()
            return status, metrics

        status, metrics = asyncio.run(read_status_and_metrics())
        self.assertEqual(status["persisted_status"], "interrupted")
        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status["observation_incomplete"])
        self.assertEqual(metrics["task"]["status"], "succeeded")
        self.assertEqual(self.tasks.status(task_id)["status"], "interrupted")

    def test_generation_finish_time_is_stable_when_backfill_updates_job(self):
        task_id, plan_id = "trip_finish_time", "plan_finish_time"
        token = self.previews.issue_access_token()
        self.tasks.create(task_id, request_body().model_dump(mode="json"))
        self.tasks.start(task_id)
        self.previews.create_job(task_id, plan_id, token)
        self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
        generation_finished_at = "2026-10-10T10:00:00+00:00"
        backfill_updated_at = "2026-10-10T10:05:00+00:00"
        with patch.object(self.previews, "_now", side_effect=[generation_finished_at, backfill_updated_at]):
            self.previews.complete_job(task_id, snapshot_for(plan_id))
            self.previews.update_job(task_id, backfill_state="running")

        single = self.previews.ready_task_completion(task_id)
        batch = self.previews.ready_task_completions((task_id,))[task_id]
        self.assertEqual(single, batch)
        self.assertEqual(single["generation_finished_at"], generation_finished_at)
        self.assertNotIn("updated_at", single)
        self.tasks.recover()

        async def read_status():
            return await self.task_service.status(task_id)

        status = asyncio.run(read_status())
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["finished_at"], generation_finished_at)

    def test_legacy_ready_preview_does_not_guess_missing_generation_finish_time(self):
        task_id, plan_id = "trip_old_finish_time", "plan_old_finish_time"
        token = self.previews.issue_access_token()
        self.tasks.create(task_id, request_body().model_dump(mode="json"))
        self.tasks.start(task_id)
        self.previews.create_job(task_id, plan_id, token)
        self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
        self.previews.complete_job(task_id, snapshot_for(plan_id))
        with sqlite3.connect(self.preview_db) as connection:
            connection.execute(
                "UPDATE preview_jobs SET generation_finished_at=NULL,updated_at=? WHERE task_id=?",
                ("2026-10-10T11:00:00+00:00", task_id),
            )

        ready = self.previews.ready_task_completion(task_id)
        self.assertIsNone(ready["generation_finished_at"])
        self.tasks.recover()

        async def read_status():
            return await self.task_service.status(task_id)

        status = asyncio.run(read_status())
        self.assertEqual(status["status"], "succeeded")
        self.assertIsNone(status["finished_at"])

    def test_list_status_filter_and_pagination_use_durable_ready_preview_summaries(self):
        ready_tasks = ("trip_list_ready_a", "trip_list_ready_b")
        for index, task_id in enumerate(ready_tasks):
            self.tasks.create(task_id, request_body().model_dump(mode="json"))
            self.tasks.start(task_id)
            token = self.previews.issue_access_token()
            plan_id = f"plan_list_ready_{index}"
            self.previews.create_job(task_id, plan_id, token)
            self.previews.update_job(task_id, generation_state="running", generation_stage="cost")
            self.previews.complete_job(task_id, snapshot_for(plan_id))

        # 模拟真正规划失败的观测记录。即使错误数据旁出现 ready 摘要，列表也不能将失败改成成功。
        failed_task = "trip_list_real_failure"
        self.tasks.create(failed_task, request_body().model_dump(mode="json"))
        self.tasks.start(failed_task)
        self.tasks.fail(failed_task, "PLANNING_FAILED", "真实规划失败")
        token = self.previews.issue_access_token()
        self.previews.create_job(failed_task, "plan_list_real_failure", token)
        self.previews.update_job(failed_task, generation_state="running", generation_stage="cost")
        self.previews.complete_job(failed_task, snapshot_for("plan_list_real_failure"))
        self.tasks.recover()

        task_service = TaskService(self.tasks, self.observation, lambda: None)
        task_service.preview_completion_resolver = self.previews.ready_task_completion
        task_service.preview_completion_batch_resolver = self.previews.ready_task_completions

        async def read_lists():
            with patch.object(self.previews, "ready_task_completions", wraps=self.previews.ready_task_completions) as batch_resolver:
                task_service.preview_completion_batch_resolver = batch_resolver
                second_success = await task_service.list_tasks("succeeded", limit=1, offset=1)
                batch_resolver.assert_called_once()
            succeeded = await task_service.list_tasks("succeeded")
            interrupted = await task_service.list_tasks("interrupted")
            failed = await task_service.list_tasks("failed")
            await task_service.close()
            return second_success, succeeded, interrupted, failed

        second_success, succeeded, interrupted, failed = asyncio.run(read_lists())
        self.assertEqual(len(second_success), 1)
        self.assertIn(second_success[0]["task_id"], ready_tasks)
        self.assertEqual({row["task_id"] for row in succeeded}, set(ready_tasks))
        self.assertEqual({row["status"] for row in succeeded}, {"succeeded"})
        self.assertEqual({row["task_id"] for row in interrupted}, set())
        self.assertEqual([row["task_id"] for row in failed], [failed_task])
        self.assertEqual(failed[0]["error_code"], "PLANNING_FAILED")

    def test_old_result_endpoint_explains_v4_preview_storage(self):
        app, client = self.make_client(FakePipeline())
        with client:
            accepted = client.post(
                "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
            ).json()
            task_id, token = accepted["task_id"], accepted["access_token"]
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
                if status["status"] in ("ready", "failed"):
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "ready")
            response = client.get(f"/api/trip/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "PREVIEW_RESULT_USE_PREVIEW_API")
        self.assertIn(f"/api/trip/v4/tasks/{task_id}/preview", response.json()["detail"]["message"])

    def test_failed_pipeline_is_visible_without_preview(self):
        app, client = self.make_client(FakePipeline(fail=True))
        with client:
            accepted = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json")).json()
            task_id, token = accepted["task_id"], accepted["access_token"]
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()
                if status["status"] == "failed":
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error_code"], "PLANNING_FAILED")
            self.assertFalse(status["preview_ready"])

    def test_pipeline_closes_context_it_created_before_raising(self):
        closed = []

        class Context(FakeRouteContext):
            async def aclose(self):
                closed.append(True)

        class FailingPipeline:
            async def run(self, request, *, task_id, plan_id, progress):
                context = Context()
                try:
                    raise RuntimeError("pipeline failed after creating route context")
                finally:
                    await context.aclose()

        app, client = self.make_client(FailingPipeline())
        with client:
            accepted = client.post(
                "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
            ).json()
            task_id, token = accepted["task_id"], accepted["access_token"]
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
                if status["status"] == "failed":
                    break
                time.sleep(0.01)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(closed, [True])

    def test_backfill_failure_keeps_preview_ready_and_releases_active_admission(self):
        async def failing_backfill(progress):
            await progress(BackfillProgress(completed=1, total=2))
            await asyncio.sleep(0.1)
            raise RuntimeError("background failure")

        app, client = self.make_client(FakePipeline(backfill=failing_backfill))
        with client:
            accepted = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json")).json()
            task_id, token = accepted["task_id"], accepted["access_token"]
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()
                if status["status"] == "ready" and status["backfill"]["status"] == "running":
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "ready")
            self.assertEqual(self.task_service.active_id, None)
            self.assertEqual(self.previews.get_preview(token).preview.draft.plan_id, accepted["plan_id"])
            next_response = client.post(
                "/api/trip/v4/tasks", json=request_body().model_dump(mode="json")
            )
            self.assertEqual(next_response.status_code, 202)
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()
                if status["backfill"]["status"] == "failed":
                    break
                time.sleep(0.01)
            self.assertEqual(status["backfill"]["status"], "failed")
            self.assertTrue(status["preview_ready"])

    def test_background_context_closes_on_shutdown(self):
        closed = []

        class Context:
            def assert_binding(self, planning_input, draft):
                self.bound = draft.plan_id

            def register_facts(self, facts):
                self.fact_count = len(facts.places)

            async def aclose(self):
                closed.append(True)

        async def pending_backfill(progress):
            await asyncio.sleep(60)

        app, client = self.make_client(FakePipeline(backfill=pending_backfill, context=Context()))
        with client:
            accepted = client.post("/api/trip/v4/tasks", json=request_body().model_dump(mode="json")).json()
            token, task_id = accepted["access_token"], accepted["task_id"]
            for _ in range(100):
                status = client.get(
                    f"/api/trip/v4/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
                ).json()
                if status["backfill"]["status"] == "running":
                    break
                time.sleep(0.01)
            self.assertEqual(status["backfill"]["status"], "running")
        self.assertTrue(closed)
        self.assertEqual(self.previews.job_status(task_id, token)["status"], "ready")
        self.assertEqual(self.previews.job_status(task_id, token)["backfill"]["status"], "interrupted")

    def test_restart_marks_running_job_interrupted_but_keeps_ready_preview(self):
        ready_token = self.previews.issue_access_token()
        self.previews.create_job("ready-task", "ready-plan", ready_token)
        self.previews.update_job("ready-task", generation_state="running", generation_stage="cost")
        self.previews.complete_job("ready-task", snapshot_for("ready-plan"))
        token = self.previews.issue_access_token()
        self.previews.create_job("running-task", "running-plan", token)
        self.previews.update_job("running-task", generation_state="running", generation_stage="dispatch")
        reopened = PreviewRepository(self.preview_db)
        reopened.initialize()
        reopened.recover_jobs()
        status = reopened.job_status("running-task", token)
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(status["error_code"], "PROCESS_INTERRUPTED")
        self.assertEqual(reopened.job_status("ready-task", ready_token)["status"], "ready")
        self.assertIsNotNone(reopened.get_preview(ready_token).snapshot)


class RouteBackfillAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_progress_storage_failure_does_not_undo_ready_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tasks.sqlite3"
            preview_database = Path(temp) / "previews.sqlite3"
            task_repository = TaskRepository(database)
            task_repository.initialize()
            preview_repository = PreviewRepository(preview_database)
            preview_repository.initialize()
            token = preview_repository.issue_access_token()
            preview_repository.create_job("task", "plan", token)
            preview_repository.update_job("task", generation_state="running", generation_stage="cost")
            preview_repository.complete_job("task", snapshot_for("plan"))

            observation = ObservationService(task_repository)
            task_service = TaskService(task_repository, observation, lambda: None)
            preview_service = PreviewTaskService(task_service, preview_repository)

            async def broken_backfill(progress):
                await progress(BackfillProgress(completed=1, total=2))
                raise RuntimeError("route provider failed")

            with patch.object(preview_repository, "update_job", side_effect=OSError("status store failed")):
                await preview_service._run_backfill("task", broken_backfill)

            status = preview_repository.job_status("task", token)
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["preview_ready"])
            await task_service.close()


class PreviewSubmitCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_http_submitter_does_not_cancel_accepted_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tasks.sqlite3"
            preview_database = Path(temp) / "previews.sqlite3"
            task_repository = TaskRepository(database)
            task_repository.initialize()
            preview_repository = PreviewRepository(preview_database)
            preview_repository.initialize()
            observation = ObservationService(task_repository)
            task_service = TaskService(task_repository, observation, lambda: None)
            preview_service = PreviewTaskService(task_service, preview_repository, FakePipeline())
            entered, release = threading.Event(), threading.Event()
            original = preview_repository.create_job

            def delayed_create_job(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release job registration")
                return original(*args)

            with patch.object(preview_repository, "create_job", side_effect=delayed_create_job):
                submitter = asyncio.create_task(preview_service.submit(request_body()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                submitter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await submitter
                release.set()
                await task_service.close()
                await preview_service.close()

            connection = sqlite3.connect(preview_database)
            try:
                states = connection.execute("SELECT generation_state FROM preview_jobs").fetchall()
                drafts = connection.execute("SELECT COUNT(*) FROM preview_drafts").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(states, [("ready",)])
            self.assertEqual(drafts, 1)


if __name__ == "__main__":
    unittest.main()
