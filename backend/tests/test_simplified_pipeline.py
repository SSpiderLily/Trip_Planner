"""S07：实际生成/持久化/相邻交通 worker 接线的禁网回归。"""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.agents.daily_plan_generator import DailyPlanRecoveryNotice
from app.models.planning_v4 import EditCommand, FactStore
from app.models.planning_v4_preview import PreviewSnapshot
from app.models.preview_storage import PreviewIdentityConflict
from app.services.current_route_service import CurrentRouteService
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.observation_service import ObservationService
from app.services.planning_v4_preview_edit_service import PlanningV4PreviewEditService
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import PreviewTaskService
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService
from test_current_route_service import FakeMapTool
from test_day_repair_diagnostics import offline_pipeline
from test_daily_plan_generator import make_facts


class SimplifiedPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch("socket.socket.connect", side_effect=AssertionError("network prohibited")),
            patch("socket.create_connection", side_effect=AssertionError("network prohibited")),
            patch("app.services.hotel_selection_service.HotelSelectionService.verify_stays", return_value=(FactStore(), ())),
        ]
        for item in self.patches:
            item.start()
        self.tracker = {}
        self.pipeline, self.request, self.unused_executor = offline_pipeline(
            {"transit": 901, "bicycling": 301, "walking": 601}, self.tracker,
        )
        self.tool = FakeMapTool(delay=0)
        self.executor = MCPToolExecutor(self.tool)
        self.route_started = asyncio.Event()
        self.route_release = asyncio.Event()
        self.route_calls = []
        self.failure = None
        owner = self

        class GatedRoutes(CurrentRouteService):
            async def run_async(service, *args, **kwargs):
                owner.route_calls.append((args[1].revision, kwargs["only_day_id"]))
                owner.route_started.set()
                await owner.route_release.wait()
                if owner.failure == "timeout":
                    raise TimeoutError("sensitive-provider-response")
                result = await super().run_async(*args, **kwargs)
                if owner.failure == "identity":
                    return result.model_copy(update={"plan_id": "foreign-plan"})
                return result

        self.routes = GatedRoutes(tool_executor=self.executor, tool_timeout_seconds=1)
        self.pipeline.current_route_service_factory = lambda: self.routes
        self.task_repo = TaskRepository(Path(self.temp.name) / "tasks.sqlite3")
        self.task_repo.initialize()
        self.repository = PreviewRepository(Path(self.temp.name) / "previews.sqlite3")
        self.repository.initialize()
        self.tasks = TaskService(self.task_repo, ObservationService(self.task_repo), lambda: None)
        self.tasks.preview_completion_resolver = self.repository.ready_task_completion
        self.tasks.preview_completion_batch_resolver = self.repository.ready_task_completions
        self.previews = PreviewTaskService(self.tasks, self.repository, self.pipeline)
        self.coordinator = PlanningV4PreviewEditService(
            self.previews, self.repository, route_service=self.routes, route_deadline_seconds=2,
        )
        self.previews.edit_coordinator = self.coordinator

    async def asyncTearDown(self):
        self.route_release.set()
        await self.tasks.close()
        await self.previews.close()
        self.executor.close()
        self.unused_executor.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def wait_status(self, accepted, state="ready"):
        for _ in range(200):
            status = await self.previews.status(accepted["task_id"], accepted["access_token"])
            if status["status"] == state:
                return status
            await asyncio.sleep(0.01)
        self.fail(f"generation never reached {state}: {status}")

    async def ready(self):
        accepted = await self.previews.submit(self.request)
        status = await self.wait_status(accepted)
        await asyncio.wait_for(self.route_started.wait(), 2)
        envelope = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        return accepted, status, envelope

    async def finish_routes(self, accepted, calculation_id):
        self.route_release.set()
        for _ in range(200):
            job = self.repository.get_route_job_by_plan(accepted["plan_id"], calculation_id)
            if job.status in {"completed", "failed", "superseded"}:
                return job
            await asyncio.sleep(0.01)
        self.fail("route task did not terminate")

    async def assert_readable_and_saveable(self, accepted, envelope):
        status = await self.tasks.status(accepted["task_id"])
        self.assertEqual(status["status"], "succeeded")
        self.assertIsNone(status["error_code"])
        self.assertTrue(envelope.preview.draft.days[1].activities)
        saved = await self.previews.save_final(
            accepted["task_id"], accepted["access_token"], save_id=f"export-{envelope.store_version}",
            source_store_version=envelope.store_version,
        )
        self.assertEqual(saved.status, "saved")
        self.assertTrue(saved.restorable)

    async def test_place_repair_finishes_before_any_route_request_or_ready_stage(self):
        stages = []
        result = await self.pipeline.run(
            self.request, task_id="stage-task", plan_id="stage-plan", progress=lambda update: stages.append(update.stage),
        )
        try:
            self.assertEqual([len(day.activities) for day in result.snapshot.preview.draft.days], [2, 6, 4])
            self.assertEqual([attempt.outcome for attempt in self.tracker["attempts"]], ["applied", "applied"])
            self.assertLess(stages.index("validation"), stages.index("repair"))
            self.assertLess(stages.index("repair"), stages.index("cost"))
            self.assertNotIn("current_routes", stages)
            self.assertNotIn("ready", stages, "ready must be published only by the snapshot transaction")
            self.assertFalse(self.route_calls)
            self.assertFalse(self.tool.calls)
            self.assertEqual(result.snapshot.preview.calculation.current_routes, "pending")
            self.assertEqual(result.initial_route_day_ids, tuple(day.day_id for day in result.snapshot.preview.draft.days))
            self.assertFalse(result.snapshot.preview.facts.routes)
        finally:
            await result.route_context.aclose()

    async def test_places_are_ready_and_exportable_while_transport_waits(self):
        accepted, status, envelope = await self.ready()
        self.assertTrue(status["preview_ready"])
        self.assertEqual(status["stage"], "ready")
        self.assertEqual(status["store_version"], 1)
        self.assertEqual(envelope.preview.calculation.current_routes, "pending")
        self.assertEqual(envelope.preview.calculation.alternative_backfill, "not_required")
        self.assertFalse(self.tool.calls)
        context = self.previews.route_contexts[accepted["plan_id"]]
        await self.assert_readable_and_saveable(accepted, envelope)
        job = await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "completed")
        updated = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(updated.plan_revision, envelope.plan_revision)
        self.assertEqual(updated.preview.draft.days, envelope.preview.draft.days)
        self.assertEqual(updated.preview.calculation.current_routes, "completed")
        self.assertFalse(updated.preview.calculation.pending_leg_ids)
        self.assertIs(self.previews.route_contexts[accepted["plan_id"]], context)
        with self.task_repo.connection() as connection:
            route_spans = connection.execute(
                "SELECT task_id FROM spans WHERE name='planning.v4.current_routes'",
            ).fetchall()
        self.assertEqual({row["task_id"] for row in route_spans}, {accepted["task_id"]})

    async def test_failed_transport_preserves_places_rounds_and_generation_success(self):
        self.failure = "timeout"
        accepted, _, envelope = await self.ready()
        with self.assertLogs("app.services.planning_v4_preview_edit_service", level="WARNING") as logs:
            job = await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)
        self.assertNotIn("sensitive-provider-response", "\n".join(logs.output))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "ROUTE_RECALCULATION_TIMEOUT")
        updated = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(updated.preview.draft.days, envelope.preview.draft.days)
        self.assertEqual(updated.preview.validation.repair_states, envelope.preview.validation.repair_states)
        self.assertEqual(updated.preview.calculation.current_routes, "failed")
        self.assertFalse(updated.preview.calculation.pending_leg_ids)
        self.assertFalse(any(mode.status == "pending" for leg in updated.preview.draft.legs for mode in leg.mode_results))
        await self.assert_readable_and_saveable(accepted, updated)

    async def test_premature_traffic_terminal_cannot_leave_pending_preview(self):
        accepted, _, envelope = await self.ready()
        with self.assertRaises(PreviewIdentityConflict):
            self.repository.publish_route_job(
                envelope.plan_id, envelope.preview.calculation.calculation_id,
                PreviewSnapshot(preview=envelope.preview, cost=envelope.cost), status="completed",
            )
        unchanged = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(unchanged, envelope)
        job = await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "completed")

    async def test_edit_during_initial_traffic_supersedes_late_result(self):
        accepted, _, original = await self.ready()
        day = original.preview.draft.days[1]
        removed = day.activities[0]
        command = EditCommand.model_validate({
            "plan_id": original.plan_id, "day_id": day.day_id, "base_revision": original.plan_revision,
            "command_id": "edit-before-initial-routes",
            "operations": [{"type": "remove_activity", "day_id": day.day_id, "activity_id": removed.activity_id}],
        })
        edited = await self.coordinator.edit(
            accepted["task_id"], accepted["access_token"], command, expected_store_version=original.store_version,
        )
        self.assertEqual(edited.envelope.plan_revision, original.plan_revision + 1)
        await self.assert_readable_and_saveable(accepted, edited.envelope)
        await self.finish_routes(accepted, edited.envelope.preview.calculation.calculation_id)
        old_job = self.repository.get_route_job_by_plan(original.plan_id, original.preview.calculation.calculation_id)
        self.assertEqual(old_job.status, "superseded")
        final = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(final.plan_revision, edited.envelope.plan_revision)
        self.assertNotIn(removed.activity_id, {activity.activity_id for day in final.preview.draft.days for activity in day.activities})

    async def test_route_identity_rejection_does_not_replace_place_snapshot(self):
        self.failure = "identity"
        accepted, _, original = await self.ready()
        job = await self.finish_routes(accepted, original.preview.calculation.calculation_id)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "ROUTE_INPUT_INVALID")
        final = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(final.preview.draft.days, original.preview.draft.days)
        self.assertEqual(final.plan_id, original.plan_id)
        self.assertFalse(final.preview.facts.routes)
        await self.assert_readable_and_saveable(accepted, final)

    async def test_initial_route_registration_and_ready_snapshot_are_atomic(self):
        with patch.object(self.repository, "_insert_route_job", side_effect=RuntimeError("storage unavailable")):
            accepted = await self.previews.submit(self.request)
            status = await self.wait_status(accepted, "failed")
        self.assertFalse(status["preview_ready"])
        self.assertIsNone(self.repository.get_preview(accepted["access_token"]))
        self.assertFalse(self.repository.list_pending_route_plans())
        self.assertFalse(self.route_calls)

    async def test_cancellation_during_snapshot_transaction_uses_confirmed_commit(self):
        entered, release = threading.Event(), threading.Event()
        complete = self.repository.complete_job
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test transaction gate expired")
            return complete(*args, **kwargs)
        with patch.object(self.repository, "complete_job", side_effect=blocked):
            accepted = await self.previews.submit(self.request)
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            generation = next(task for task in self.tasks.background if not task.done())
            generation.cancel()
            release.set()
            await asyncio.wait_for(generation, 2)
        status = await self.wait_status(accepted)
        self.assertTrue(status["preview_ready"])
        envelope = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        await self.assert_readable_and_saveable(accepted, envelope)

    async def test_cancellation_after_commit_does_not_turn_ready_task_into_failure(self):
        entered = asyncio.Event()
        async def blocked(record):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.coordinator, "schedule_for_record", side_effect=blocked):
            accepted = await self.previews.submit(self.request)
            await asyncio.wait_for(entered.wait(), 2)
            generation = next(task for task in self.tasks.background if not task.done())
            generation.cancel()
            await asyncio.wait_for(generation, 2)
        status = await self.wait_status(accepted)
        self.assertTrue(status["preview_ready"])
        envelope = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        await self.assert_readable_and_saveable(accepted, envelope)
        # 同进程读状态可恢复尚未启动的相邻交通，无需再次生成地点。
        job = await self.coordinator.get_job(
            accepted["task_id"], accepted["access_token"], envelope.preview.calculation.calculation_id,
        )
        self.assertIn(job.job.status, {"pending", "running"})
        await asyncio.wait_for(self.route_started.wait(), 2)
        await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)

    async def test_scheduling_failure_is_terminal_for_traffic_and_keeps_generation_ready(self):
        with patch.object(self.coordinator, "schedule_for_record", side_effect=RuntimeError("sensitive-provider-response")):
            accepted = await self.previews.submit(self.request)
            await self.wait_status(accepted)
            for _ in range(200):
                envelope = await self.previews.preview(accepted["task_id"], accepted["access_token"])
                if envelope.preview.calculation.current_routes == "failed":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(envelope.preview.calculation.current_routes, "failed")
        job = self.repository.get_route_job_by_plan(accepted["plan_id"], envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "ROUTE_SCHEDULING_FAILED")
        await self.assert_readable_and_saveable(accepted, envelope)
        self.assertFalse(self.route_calls)

    async def test_traffic_initialization_error_does_not_block_places(self):
        def unavailable():
            raise RuntimeError("sensitive-provider-response")
        self.pipeline.current_route_service_factory = unavailable
        accepted, _, envelope = await self.ready()
        await self.assert_readable_and_saveable(accepted, envelope)
        job = await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "completed")
        self.assertIn(envelope.plan_id, self.previews.route_contexts)

    async def test_place_technical_retry_remains_separate_from_transport(self):
        daily = self.pipeline.daily_plan_adapter_factory(120)
        original = daily.revise_day
        count = [0]
        async def recover(*args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                raise TimeoutError("sensitive-model-response")
            return await original(*args, **kwargs)
        daily.revise_day = recover
        self.pipeline.daily_plan_adapter_factory = lambda _: daily
        accepted, _, envelope = await self.ready()
        self.assertEqual(count[0], 3)
        self.assertEqual([attempt.round_number for attempt in self.tracker["attempts"]], [1, 1, 1])
        self.assertEqual([state.completed_rounds for state in envelope.preview.validation.repair_states], [0, 1, 1])
        self.assertFalse(self.tool.calls)
        await self.assert_readable_and_saveable(accepted, envelope)

    async def test_missing_weather_and_cost_do_not_block_place_delivery(self):
        facts = make_facts().model_copy(update={"weather": ()})
        with patch("test_day_repair_diagnostics.make_facts", return_value=facts):
            pipeline, request, executor = offline_pipeline({}, self.tracker)
        self.unused_executor.close()
        self.unused_executor = executor
        pipeline.current_route_service_factory = lambda: self.routes
        self.previews.pipeline = pipeline
        self.request = request
        accepted, _, envelope = await self.ready()
        self.assertFalse(envelope.preview.facts.weather)
        self.assertTrue(all(day.status == "all_unknown" for day in envelope.cost.days))
        self.assertFalse(self.tool.calls)
        await self.assert_readable_and_saveable(accepted, envelope)

    async def test_recovery_notice_survives_independent_transport_update(self):
        daily = self.pipeline.daily_plan_adapter_factory(120)
        original = daily.generate
        async def generate(*args, **kwargs):
            result = await original(*args, **kwargs)
            day = result.draft.days[0]
            result.recovery_notices = (DailyPlanRecoveryNotice(
                day_id=day.day_id, slot_id=day.activities[0].slot_id,
                activity_index=0, reason_code="candidate_reference_invalid",
            ),)
            return result
        daily.generate = generate
        self.pipeline.daily_plan_adapter_factory = lambda _: daily
        accepted, _, envelope = await self.ready()
        before = tuple(issue for issue in envelope.preview.validation.issues if issue.code == "INITIAL_ACTIVITY_OMITTED")
        self.assertEqual(len(before), 1)
        await self.finish_routes(accepted, envelope.preview.calculation.calculation_id)
        final = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        after = tuple(issue for issue in final.preview.validation.issues if issue.code == "INITIAL_ACTIVITY_OMITTED")
        self.assertEqual(before, after)
        self.assertIn(before[0].issue_id, final.preview.validation.days[0].issue_ids)

    async def test_exhausted_place_technical_recovery_delivers_editable_missing_draft(self):
        daily = self.pipeline.daily_plan_adapter_factory(120)
        count = [0]
        async def unavailable(*args, **kwargs):
            count[0] += 1
            raise TimeoutError("sensitive-model-response")
        daily.revise_day = unavailable
        self.pipeline.daily_plan_adapter_factory = lambda _: daily
        accepted, _, envelope = await self.ready()
        self.assertEqual(count[0], 6, "each of two incomplete dates has at most 2 technical retries")
        self.assertTrue(any(issue.triggers_repair for issue in envelope.preview.validation.issues))
        stopped = [state for state in envelope.preview.validation.repair_states if state.status == "stopped"]
        self.assertEqual(len(stopped), 2)
        self.assertTrue(all(state.completed_rounds == 0 and state.stop_reason == "technical_error" for state in stopped))
        await self.assert_readable_and_saveable(accepted, envelope)


if __name__ == "__main__":
    unittest.main()
