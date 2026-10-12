"""S08：实际相邻边查询、零查询选择和新草稿持久化的禁网回归。"""
import asyncio
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.models.planning_v4 import EditCommand, PreviewCalculation
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.current_route_service import CurrentRouteError, CurrentRouteService
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.observation_service import ObservationService
from app.services.planning_v4_preview_edit_service import PlanningV4PreviewEditService
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import PreviewPipelineResult, PreviewTaskService
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskError, TaskService
from test_current_route_service import FakeMapTool, TOOL_BY_MODE
from test_planning_v4_edit_service import _prepare_preview, _with_recalculated_report
from test_preview_task_service import request_body, snapshot_for
from test_simplified_routes import request_pairs


class SimplifiedEditPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        for target in ("socket.socket.connect", "socket.create_connection"):
            item = patch(target, side_effect=AssertionError("network prohibited"))
            item.start()
            self.addCleanup(item.stop)
        class TwoOptionTool(FakeMapTool):
            def __call__(tool, name, arguments):
                raw = super().__call__(name, arguments)
                if name == TOOL_BY_MODE["transit"]:
                    raw["route"]["transits"].append({"duration": "650", "segments": []})
                return raw

        self.tool = TwoOptionTool(delay=0)
        self.executor = MCPToolExecutor(self.tool)
        self.route_calls = []
        self.route_release = asyncio.Event()
        self.route_release.set()
        self.route_started = asyncio.Event()
        self.failure = None
        owner = self

        class ScopedRoutes(CurrentRouteService):
            async def run_async(service, *args, **kwargs):
                owner.route_calls.append(kwargs)
                if kwargs.get("only_leg_ids") is not None:
                    owner.route_started.set()
                    await owner.route_release.wait()
                    if owner.failure == "timeout":
                        raise TimeoutError("sensitive-response")
                result = await super().run_async(*args, **kwargs)
                if owner.failure == "untouched" and kwargs.get("only_leg_ids"):
                    untouched = next(leg for leg in result.draft.legs
                                     if leg.leg_id not in kwargs["only_leg_ids"])
                    draft = result.draft.model_copy(update={"legs": tuple(
                        leg.model_copy(update={"preferred_mode": "walking"})
                        if leg.leg_id == untouched.leg_id else leg for leg in result.draft.legs
                    )})
                    return result.model_copy(update={"draft": draft})
                return result

        self.routes = ScopedRoutes(tool_executor=self.executor, tool_timeout_seconds=1)

        class CompletedPipeline:
            calls = 0
            async def run(service, request, *, task_id, plan_id, progress):
                service.calls += 1
                base = _prepare_preview(snapshot_for(plan_id).preview)
                result = await owner.routes.run_async(base.planning_input, base.draft, base.facts)
                complete = _with_recalculated_report(base, draft=result.draft, facts=result.fact_store)
                complete = complete.model_copy(update={"calculation": PreviewCalculation(
                    based_on_revision=complete.draft.revision,
                    calculation_id="completed-fixture", current_routes="completed",
                    alternative_backfill="not_required",
                )})
                return PreviewPipelineResult(snapshot=PlanningV4PreviewEditService._snapshot_with_calculations(complete))

        self.pipeline = CompletedPipeline()
        self.task_repo = TaskRepository(Path(self.temp.name) / "tasks.sqlite3")
        self.task_repo.initialize()
        self.repository = PreviewRepository(Path(self.temp.name) / "previews.sqlite3")
        self.repository.initialize()
        self.tasks = TaskService(self.task_repo, ObservationService(self.task_repo), lambda: None)
        self.tasks.preview_completion_resolver = self.repository.ready_task_completion
        self.tasks.preview_completion_batch_resolver = self.repository.ready_task_completions
        self.previews = PreviewTaskService(self.tasks, self.repository, self.pipeline)
        self.coordinator = PlanningV4PreviewEditService(self.previews, self.repository, route_service=self.routes)
        self.previews.edit_coordinator = self.coordinator
        self.accepted = await self.previews.submit(request_body())
        for _ in range(200):
            status = await self.previews.status(self.task_id, self.token)
            if status["status"] == "ready":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(status["status"], "ready")
        self.original = await self.previews.preview(self.task_id, self.token)
        self.tool.calls.clear()
        self.route_calls.clear()

    @property
    def task_id(self):
        return self.accepted["task_id"]

    @property
    def token(self):
        return self.accepted["access_token"]

    async def asyncTearDown(self):
        self.route_release.set()
        await self.tasks.close()
        await self.previews.close()
        self.executor.close()
        self.temp.cleanup()

    def command(self, operations, *, envelope=None, command_id="s08-command"):
        envelope = envelope or self.original
        return EditCommand.model_validate({
            "plan_id": envelope.plan_id, "day_id": "synthetic-day-2",
            "base_revision": envelope.plan_revision, "command_id": command_id,
            "operations": [dict(day_id="synthetic-day-2", **operation) for operation in operations],
        })

    async def edit(self, operations, *, envelope=None, command_id="s08-command"):
        envelope = envelope or self.original
        return await self.coordinator.edit(self.task_id, self.token,
            self.command(operations, envelope=envelope, command_id=command_id),
            expected_store_version=envelope.store_version)

    async def finish(self, response):
        if response.job is not None:
            for _ in range(200):
                job = self.repository.get_route_job_by_plan(response.envelope.plan_id, response.job.calculation_id)
                if job.status in {"completed", "failed", "superseded"}:
                    break
                await asyncio.sleep(0.01)
            self.assertIn(job.status, {"completed", "failed", "superseded"})
        return await self.previews.preview(self.task_id, self.token)

    def remove_breakfast(self):
        return [{"type": "remove_activity", "activity_id": "synthetic-activity-2-breakfast"}]

    def assert_retained(self, before, after, pending_ids):
        old = {leg.leg_id: leg for leg in before.preview.draft.legs}
        for leg in after.preview.draft.legs:
            if leg.leg_id in old and leg.leg_id not in pending_ids:
                self.assertEqual(leg, old[leg.leg_id].model_copy(update={"plan_revision": after.plan_revision}))
        self.assertEqual(after.preview.draft.days[0], before.preview.draft.days[0])
        self.assertEqual(after.preview.draft.days[2], before.preview.draft.days[2])
        self.assertNotIn("timing", after.model_dump())

    def assert_exact_queries(self, accepted_edit):
        pending = set(accepted_edit.envelope.preview.calculation.pending_leg_ids)
        legs = [leg for leg in accepted_edit.envelope.preview.draft.legs if leg.leg_id in pending]
        places = {place.poi_id: place for place in accepted_edit.envelope.preview.facts.places}
        expected = Counter()
        for leg in legs:
            origin, destination = (places[endpoint.poi_id].location for endpoint in (leg.from_endpoint, leg.to_endpoint))
            for mode in TOOL_BY_MODE:
                expected[(mode, f"{origin.lng},{origin.lat}", f"{destination.lng},{destination.lat}")] += 1
        self.assertEqual(request_pairs(self.tool.calls), expected)
        self.assertEqual({leg_id for call in self.route_calls for leg_id in call["only_leg_ids"]}, pending)

    async def test_delete_queries_new_neighbor_and_retains_existing_routes_with_empty_cache(self):
        response = await self.edit(self.remove_breakfast())
        self.assertEqual(len(response.envelope.preview.calculation.pending_leg_ids), 1)
        final = await self.finish(response)
        self.assert_exact_queries(response)
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)
        self.assertEqual(final.preview.calculation.current_routes, "completed")

    async def test_add_queries_only_two_new_neighbors(self):
        response = await self.edit([{"type": "add_activity", "client_ref": "added-yuyuan",
                                    "candidate_ref": "synthetic-cand-yuyuan", "slot_id": "synthetic-2-morning"}])
        self.assertEqual(len(response.envelope.preview.calculation.pending_leg_ids), 2)
        final = await self.finish(response)
        self.assert_exact_queries(response)
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)

    async def test_reorder_queries_only_changed_directed_neighbors(self):
        day = self.original.preview.draft.days[1]
        order = [activity.activity_id for activity in day.activities]
        order[-2:] = reversed(order[-2:])
        response = await self.edit([{"type": "reorder_activities", "activity_ids": order}])
        self.assertEqual(len(response.envelope.preview.calculation.pending_leg_ids), 2)
        final = await self.finish(response)
        self.assert_exact_queries(response)
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)

    async def test_switch_available_mode_has_no_worker_or_query_and_preserves_places(self):
        leg = next(leg for leg in self.original.preview.draft.legs if leg.day_id == "synthetic-day-2")
        option_id = next(mode.option_ids[0] for mode in leg.mode_results if mode.mode == "walking")
        response = await self.edit([{"type": "select_leg_mode", "leg_id": leg.leg_id,
                                    "mode": "walking", "option_id": option_id}])
        self.assertIsNone(response.job)
        self.assertFalse(self.route_calls or self.tool.calls)
        self.assertEqual(response.envelope.preview.draft.days, self.original.preview.draft.days)
        selected = next(item for item in response.envelope.preview.draft.legs if item.leg_id == leg.leg_id)
        self.assertEqual((selected.selected_mode, selected.selected_option_id, selected.selection_source),
                         ("walking", option_id, "user"))
        self.assertEqual(response.envelope.preview.facts, self.original.preview.facts)

    async def test_switch_second_option_in_same_mode_is_effective_and_zero_query(self):
        leg = next(leg for leg in self.original.preview.draft.legs if leg.day_id == "synthetic-day-2")
        result = next(mode for mode in leg.mode_results if mode.mode == "transit")
        # 两个公交方案中精确选择第二条，不能退回首条或最快方案。
        self.assertGreater(len(result.option_ids), 1)
        first = await self.edit([{"type": "select_leg_mode", "leg_id": leg.leg_id,
                                 "mode": "transit", "option_id": result.option_ids[0]}])
        second = await self.edit([{"type": "select_leg_mode", "leg_id": leg.leg_id,
                                  "mode": "transit", "option_id": result.option_ids[1]}],
                                 envelope=first.envelope, command_id="second-option")
        selected = next(item for item in second.envelope.preview.draft.legs if item.leg_id == leg.leg_id)
        self.assertEqual(selected.selected_option_id, result.option_ids[1])
        self.assertFalse(self.route_calls or self.tool.calls)

    async def test_timeout_keeps_untouched_same_day_selection_and_saveable_places(self):
        self.failure = "timeout"
        response = await self.edit(self.remove_breakfast())
        with self.assertLogs("app.services.planning_v4_preview_edit_service", level="WARNING") as logs:
            final = await self.finish(response)
        self.assertNotIn("sensitive-response", "\n".join(logs.output))
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)
        self.assertEqual(final.preview.calculation.current_routes, "failed")
        self.assertFalse(final.preview.calculation.pending_leg_ids)
        saved = await self.previews.save_final(self.task_id, self.token, save_id="failed-routes",
                                              source_store_version=final.store_version)
        self.assertTrue(saved.restorable)

    async def test_worker_rejects_changes_to_unqueried_routes(self):
        self.failure = "untouched"
        response = await self.edit(self.remove_breakfast())
        final = await self.finish(response)
        self.assertEqual(final.preview.calculation.current_routes, "failed")
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)

    async def test_old_store_and_revision_conflict_and_same_command_replay_do_not_query(self):
        self.route_release.clear()
        response = await self.edit(self.remove_breakfast())
        replay = await self.edit(self.remove_breakfast())
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt_store_version, response.receipt_store_version)
        with self.assertRaises(TaskError) as stale:
            await self.edit(self.remove_breakfast(), command_id="stale-other-id")
        self.assertEqual(stale.exception.code, "PREVIEW_VERSION_CONFLICT")
        with self.assertRaises(TaskError) as reused:
            await self.edit([{"type": "remove_activity", "activity_id": "synthetic-activity-2-evening"}])
        self.assertEqual(reused.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertFalse(self.tool.calls)
        self.route_release.set()
        await self.finish(response)
        self.assertEqual(len(self.route_calls), 1)

    async def test_refresh_and_restart_recover_exact_pending_edges_without_place_regeneration(self):
        self.route_release.clear()
        response = await self.edit(self.remove_breakfast())
        await asyncio.wait_for(self.route_started.wait(), 2)
        refreshed = await self.previews.preview(self.task_id, self.token)
        self.assertEqual(refreshed, response.envelope)
        await self.coordinator.close()
        reopened = PreviewRepository(self.repository.path)
        reopened.initialize()
        reopened.recover_jobs()
        self.repository = self.previews.repository = reopened
        self.coordinator = PlanningV4PreviewEditService(self.previews, reopened, route_service=self.routes)
        self.previews.edit_coordinator = self.coordinator
        self.route_calls.clear()
        self.route_release.set()
        await self.coordinator.recover_pending_jobs()
        final = await self.finish(response)
        self.assert_exact_queries(response)
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)
        self.assertEqual(self.pipeline.calls, 1)

    async def test_pending_save_restore_only_resumes_saved_pending_edges_and_rejects_late_result(self):
        self.route_release.clear()
        response = await self.edit(self.remove_breakfast())
        await asyncio.wait_for(self.route_started.wait(), 2)
        saved = await self.previews.save_final(self.task_id, self.token, save_id="pending-s08",
                                              source_store_version=response.envelope.store_version)
        self.assertTrue(saved.restorable)
        restored = await self.previews.restore_final(self.task_id, self.token, restore_id="restore-s08",
            expected_store_version=response.envelope.store_version,
            expected_plan_revision=response.envelope.plan_revision, expected_save_id="pending-s08")
        self.assertEqual(restored.envelope.preview.calculation.pending_leg_ids,
                         response.envelope.preview.calculation.pending_leg_ids)
        self.assertEqual(restored.envelope.preview.draft.days, response.envelope.preview.draft.days)
        self.route_release.set()
        calc_id = restored.envelope.preview.calculation.calculation_id
        for _ in range(200):
            job = self.repository.get_route_job_by_plan(restored.envelope.plan_id, calc_id)
            if job.status == "completed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "completed")
        old = self.repository.get_route_job_by_plan(response.envelope.plan_id, response.job.calculation_id)
        self.assertEqual(old.status, "superseded")
        final = await self.previews.preview(self.task_id, self.token)
        self.assertEqual(final.plan_revision, restored.envelope.plan_revision)
        self.assert_retained(response.envelope, final, response.envelope.preview.calculation.pending_leg_ids)
        self.assertEqual(self.pipeline.calls, 1)

    async def test_failed_final_save_preserves_previous_save_and_receipt(self):
        await self.previews.save_final(self.task_id, self.token, save_id="previous", source_store_version=1)
        previous = self.repository.read_final(self.token)
        response = await self.edit(self.remove_breakfast())
        final = await self.finish(response)
        with self.repository._write_transaction() as connection:
            connection.execute("""CREATE TRIGGER fail_final BEFORE UPDATE ON preview_finals
                BEGIN SELECT RAISE(ABORT, 'offline storage failure'); END""")
        with self.assertRaises(TaskError) as error:
            await self.previews.save_final(self.task_id, self.token, save_id="failed",
                                          source_store_version=final.store_version)
        self.assertEqual(error.exception.code, "PREVIEW_STORAGE_UNAVAILABLE")
        self.assertEqual(self.repository.read_final(self.token), previous)
        with self.repository._read_connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM preview_mutations WHERE mutation_id='failed'").fetchone())

    async def test_complete_saved_snapshot_restores_without_queries_after_reopen(self):
        await self.previews.save_final(self.task_id, self.token, save_id="complete", source_store_version=1)
        response = await self.edit(self.remove_breakfast())
        final = await self.finish(response)
        self.tool.calls.clear()
        self.route_calls.clear()
        restored = await self.previews.restore_final(self.task_id, self.token, restore_id="restore-complete",
            expected_store_version=final.store_version, expected_plan_revision=final.plan_revision,
            expected_save_id="complete")
        self.assertEqual(restored.envelope.preview.draft.days, self.original.preview.draft.days)
        self.assertEqual(restored.envelope.preview.calculation.current_routes, "completed")
        self.assertFalse(self.route_calls or self.tool.calls)
        reopened = PreviewRepository(self.repository.path)
        reopened.initialize()
        self.assertEqual(reopened.get_preview(self.token).snapshot,
                         PreviewSnapshot(preview=restored.envelope.preview, cost=restored.envelope.cost))
        self.assertEqual(self.pipeline.calls, 1)

    async def test_unsupported_or_corrupt_draft_fails_read_edit_and_recovery_without_queries(self):
        self.route_release.clear()
        response = await self.edit(self.remove_breakfast())
        await asyncio.wait_for(self.route_started.wait(), 2)
        await self.coordinator.close()
        with self.repository._write_transaction() as connection:
            row = connection.execute("SELECT preview_json FROM preview_drafts").fetchone()
            value = json.loads(row[0])
            value["draft"]["days"][0]["activities"][0]["duration_s"] = 3600
            unsupported = json.dumps(value)
            connection.execute("UPDATE preview_drafts SET preview_json=?", (unsupported,))
        self.repository.recover_jobs()
        job = self.repository.get_route_job_by_plan(response.envelope.plan_id, response.job.calculation_id)
        self.assertEqual((job.status, job.error_code), ("failed", "PREVIEW_CONTRACT_UNSUPPORTED"))
        with self.assertRaises(TaskError) as error:
            await self.previews.preview(self.task_id, self.token)
        self.assertEqual(error.exception.code, "PREVIEW_CONTRACT_UNSUPPORTED")
        self.coordinator = PlanningV4PreviewEditService(self.previews, self.repository, route_service=self.routes)
        self.previews.edit_coordinator = self.coordinator
        with self.assertRaises(TaskError) as error:
            await self.edit(self.remove_breakfast(), envelope=response.envelope, command_id="unsupported-edit")
        self.assertEqual(error.exception.code, "PREVIEW_CONTRACT_UNSUPPORTED")
        self.assertFalse(self.tool.calls)
        self.assertEqual(self.pipeline.calls, 1)
        with self.repository._read_connection() as connection:
            self.assertEqual(connection.execute("SELECT preview_json FROM preview_drafts").fetchone()[0], unsupported)

    async def test_broken_saved_source_closure_is_explicit_and_cannot_replace_draft(self):
        await self.previews.save_final(self.task_id, self.token, save_id="corrupt-final", source_store_version=1)
        with self.repository._write_transaction() as connection:
            value = json.loads(connection.execute("SELECT preview_json FROM preview_finals").fetchone()[0])
            value["draft"]["days"][0]["activities"][0]["candidate_ref"] = "missing-candidate"
            connection.execute("UPDATE preview_finals SET preview_json=?", (json.dumps(value),))
        for operation in (
            lambda: self.previews.final_state(self.task_id, self.token),
            lambda: self.previews.restore_final(self.task_id, self.token, restore_id="corrupt-restore",
                expected_store_version=1, expected_plan_revision=1, expected_save_id="corrupt-final"),
        ):
            with self.assertRaises(TaskError) as error:
                await operation()
            self.assertEqual(error.exception.code, "PREVIEW_CONTRACT_UNSUPPORTED")
        self.assertEqual(await self.previews.preview(self.task_id, self.token), self.original)
        self.assertFalse(self.tool.calls)

    async def test_route_scope_rejects_removed_unknown_duplicate_and_other_day_leg_ids(self):
        preview = self.original.preview
        leg = next(leg for leg in preview.draft.legs if leg.day_id == "synthetic-day-2")
        other = next(leg for leg in preview.draft.legs if leg.day_id == "synthetic-day-1")
        for ids in (("missing",), (leg.leg_id, leg.leg_id), (other.leg_id,)):
            with self.assertRaisesRegex(CurrentRouteError, "requested_live_leg_scope_invalid"):
                await self.routes.run_async(preview.planning_input, preview.draft, preview.facts,
                                            only_day_id=leg.day_id, only_leg_ids=ids)
        self.assertFalse(self.tool.calls)

    async def test_broken_draft_source_cannot_overwrite_previous_final_on_save(self):
        await self.previews.save_final(self.task_id, self.token, save_id="valid-final", source_store_version=1)
        previous = self.repository.read_final(self.token)
        with self.repository._write_transaction() as connection:
            value = json.loads(connection.execute("SELECT preview_json FROM preview_drafts").fetchone()[0])
            value["draft"]["days"][0]["activities"][0]["candidate_ref"] = "broken"
            connection.execute("UPDATE preview_drafts SET preview_json=?,store_version=2", (json.dumps(value),))
        with self.assertRaises(TaskError) as error:
            await self.previews.save_final(self.task_id, self.token, save_id="corrupt-save", source_store_version=2)
        self.assertEqual(error.exception.code, "PREVIEW_CONTRACT_UNSUPPORTED")
        self.assertEqual(self.repository.read_final(self.token), previous)

    async def test_query_new_neighbor_does_not_retry_or_clear_retained_provider_failure(self):
        preview = self.original.preview
        # 模拟上一轮已结束的某条步行查询失败；公交有效方案仍可用。
        retained = next(leg for leg in preview.draft.legs if leg.day_id == "synthetic-day-2"
                        and leg.to_endpoint.reference_id == "synthetic-activity-2-evening")
        damaged = retained.model_copy(update={"mode_results": tuple(
            mode.model_copy(update={"status": "error", "route_fact_id": None, "option_ids": (),
                                    "error_category": "provider_error"}) if mode.mode == "walking" else mode
            for mode in retained.mode_results
        )})
        draft = preview.draft.model_copy(update={"legs": tuple(
            damaged if leg.leg_id == retained.leg_id else leg for leg in preview.draft.legs
        )})
        failed = preview.model_copy(update={"draft": draft, "calculation": preview.calculation.model_copy(update={"current_routes": "failed"})})
        snapshot = PlanningV4PreviewEditService._snapshot_with_calculations(failed)
        with self.repository._write_transaction() as connection:
            connection.execute("UPDATE preview_drafts SET preview_json=?,cost_json=?",
                               (snapshot.preview.model_dump_json(), snapshot.cost.model_dump_json()))
        self.original = await self.previews.preview(self.task_id, self.token)
        response = await self.edit(self.remove_breakfast())
        final = await self.finish(response)
        self.assert_exact_queries(response)
        self.assert_retained(self.original, final, response.envelope.preview.calculation.pending_leg_ids)
        job = self.repository.get_route_job_by_plan(final.plan_id, response.job.calculation_id)
        self.assertEqual((job.status, final.preview.calculation.current_routes), ("failed", "failed"))

    async def test_unsupported_pending_source_is_rejected_on_claim_without_recovery_loop(self):
        # 只暂停本测试的调度，编辑本身仍按正常事务写入。
        with patch.object(self.coordinator, "_ensure_worker", return_value=None):
            response = await self.edit(self.remove_breakfast())
        with self.repository._write_transaction() as connection:
            connection.execute("UPDATE preview_drafts SET cost_json='invalid-json'")
        self.assertIsNone(self.repository.claim_route_job(response.envelope.plan_id))
        self.assertFalse(self.repository.has_pending_route_job(response.envelope.plan_id))
        self.assertFalse(self.tool.calls)


if __name__ == "__main__":
    unittest.main()
