"""单日修订诊断：真实管线/路线缓存/SQLite，模型和地图均为离线替身。"""
import asyncio
from contextlib import nullcontext
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.models.planning_v4 import (
    DayRevision, DraftActivity, DraftDay, LodgingRef, PlanDraft, FactStore,
    ProposalActivity, ProposalDay,
)
from app.services.current_route_service import CurrentRouteService
from app.services.day_repair_service import DayRepairService
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.observation_service import ObservationService
from app.services.planning_dispatch_service import PlanningDispatchResult
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.task_repository import TaskRepository
from test_current_route_service import FakeMapTool
from test_daily_plan_generator import make_candidates, make_facts, make_input


def offline_pipeline(durations, tracker):
    seed = make_input(lodging_query="外滩酒店")
    request = seed.request.model_copy(update={
        "arrival_at": datetime.fromisoformat("2026-10-10T16:00:00+08:00"),
        "departure_at": datetime.fromisoformat("2026-10-12T18:30:00+08:00"),
    })
    facts = make_facts()
    candidates = tuple(c.model_copy(update={"slot_ids": ()}) for c in make_candidates(specified_lodging=True))

    class Interpreter:
        def interpret(self, *_args, **_kwargs):
            return seed.requirements

    class Dispatch:
        async def run_async(self, context):
            return PlanningDispatchResult(
                request_id=context.request_id, run_id=context.run_id,
                context_revision=context.context_revision, base_revision=context.base_revision,
                action="plan", stop_reason="plan", reason="offline",
                fact_store=facts, candidate_refs=candidates,
            )

    class Daily:
        async def generate(self, planning_input, *_args, source_run_id, plan_id, **_kwargs):
            days = []
            for index, day in enumerate(planning_input.days):
                periods = tuple(s.period for s in day.slots if s.eligibility == "required") if index == 0 else ("morning", "afternoon") if index == 1 else ("afternoon",)
                activities = tuple(DraftActivity(
                    activity_id=f"initial-{index}-{period}",
                    slot_id=next(s.slot_id for s in day.slots if s.period == period),
                    kind=("meal" if period in {"breakfast", "lunch", "dinner"} else "sightseeing") if index == 0 else "free_time",
                    candidate_ref=("candidate-meal" if period in {"breakfast", "lunch", "dinner"} else "candidate-bund") if index == 0 else None,
                    poi_id=("poi-meal" if period in {"breakfast", "lunch", "dinner"} else "poi-bund") if index == 0 else None,
                    place_fact_id=("fact-meal" if period in {"breakfast", "lunch", "dinner"} else "fact-bund") if index == 0 else None,
                ) for period in periods)
                days.append(DraftDay(
                    day_id=day.day_id, day_date=day.day_date, activities=activities,
                    unfilled_slot_ids=tuple(s.slot_id for s in day.slots if s.eligibility != "skipped"
                                            and not any(a.kind != "free_time" and a.slot_id == s.slot_id for a in activities)),
                    lodging=LodgingRef(candidate_ref="candidate-hotel", poi_id="poi-hotel",
                                       place_fact_id="fact-hotel", selection_source="user"),
                ))
            draft = PlanDraft(plan_id=plan_id, request_id=request.request_id,
                              source_run_id=source_run_id, revision=1,
                              rule_version=planning_input.rules.rule_version, days=tuple(days))
            tracker["initial"] = draft
            return SimpleNamespace(draft=draft)

        async def revise_day(self, planning_input, draft, report, day_id, issues, _assignments,
                             *_args, preserve_activity_ids, **_kwargs):
            day = next(d for d in planning_input.days if d.day_id == day_id)
            activities = []
            for slot in day.slots:
                if slot.eligibility == "skipped":
                    continue
                meal = slot.period in {"breakfast", "lunch", "dinner"}
                activities.append(ProposalActivity(
                    proposal_ref=f"repair-{slot.period}", slot_id=slot.slot_id,
                    kind="meal" if meal else "sightseeing",
                    candidate_ref="candidate-meal" if meal else "candidate-bund",
                ))
            return DayRevision(
                day_id=day_id, base_revision=draft.revision, revision=draft.revision + 1,
                based_on_report_revision=report.based_on_revision,
                replacement_day=ProposalDay(day_id=day_id, day_date=day.day_date, activities=tuple(activities)),
                preserve_activity_ids=preserve_activity_ids,
                source_issue_ids=tuple(i.issue_id for i in issues),
            )

    class TrackingRepair(DayRepairService):
        async def run_async(self, *args, **kwargs):
            result = await super().run_async(*args, **kwargs)
            tracker["attempts"] = result.attempts
            return result

    tool = FakeMapTool(delay=0)
    tool.durations = durations
    executor = MCPToolExecutor(tool)
    pipeline = PlanningV4Pipeline(
        requirement_interpreter_factory=lambda _: Interpreter(),
        dispatch_service_factory=lambda _: Dispatch(),
        daily_plan_adapter_factory=lambda _: Daily(),
        current_route_service_factory=lambda: CurrentRouteService(tool_executor=executor, tool_timeout_seconds=2),
        day_repair_service_factory=TrackingRepair,
    )
    return pipeline, request, executor


class RepairDiagnosticsIntegrationTests(unittest.TestCase):
    def run_case(self, durations):
        tracker = {}
        pipeline, request, executor = offline_pipeline(durations, tracker)
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "observation.sqlite3")
            repo.initialize()
            repo.create("diagnostic-task", {})
            observer = ObservationService(repo)
            try:
                with observer.task("diagnostic-task"), patch(
                    "socket.socket.connect", side_effect=AssertionError("network prohibited"),
                ), patch("socket.create_connection", side_effect=AssertionError("network prohibited")), patch(
                    "app.services.hotel_selection_service.HotelSelectionService.verify_stays",
                    return_value=(FactStore(), ()),
                ):
                    result = asyncio.run(pipeline.run(
                        request, task_id="diagnostic-task", plan_id="diagnostic-plan", progress=lambda _: None,
                    ))
                with repo.connection() as connection:
                    rows = [dict(row) for row in connection.execute("SELECT * FROM spans ORDER BY started_at")]
            finally:
                executor.close()
        return result, tracker, rows

    def test_non_minute_durations_preserve_valid_day_revisions(self):
        result, tracker, rows = self.run_case({"walking": 601, "bicycling": 301, "transit": 901})
        self.assertEqual([len(d.activities) for d in result.snapshot.preview.draft.days], [2, 6, 4])
        self.assertEqual([a.outcome for a in tracker["attempts"]], ["applied", "applied"])
        self.assertFalse(any(r["status"] == "failed" for r in rows))
        summary = json.loads(next(r for r in rows if r["name"] == "planning.v4.run")["output_data"])
        self.assertEqual(summary["days"][1]["meal_count"], 3)
        self.assertEqual(summary["days"][1]["sightseeing_count"], 3)

    def test_route_query_failure_cannot_reject_place_revisions_or_consume_rounds(self):
        good, tracker, rows = self.run_case({"walking": 600, "bicycling": 300, "transit": 900})
        with patch.object(CurrentRouteService, "run_async", side_effect=RuntimeError("sensitive-provider-response")) as query:
            independent, independent_tracker, independent_rows = self.run_case({"walking": 601, "bicycling": 301, "transit": 901})
        query.assert_not_called()
        self.assertEqual([len(d.activities) for d in independent.snapshot.preview.draft.days], [2, 6, 4])
        self.assertEqual([a.outcome for a in independent_tracker["attempts"]], ["applied", "applied"])
        rounds = [r for r in independent_rows if r["name"] == "repair.round_result"]
        self.assertEqual(len(rounds), 2)
        validations = [r for r in independent_rows if r["name"] == "repair.place_validation"]
        self.assertEqual(len(validations), 2)
        for row in validations:
            metadata = json.loads(row["input_data"])
            self.assertEqual(metadata["round_number"], 1)
            self.assertTrue(metadata["round_id"])
            self.assertNotEqual(row["status"], "failed")
        self.assertFalse(any(r["name"] == "repair.route_validation" for r in independent_rows))


class RepairObservationTests(unittest.TestCase):
    def observe_error(self, error, *, broken_store=False):
        from app.services.planning_diagnostics import repair_stage
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "observation.sqlite3")
            repo.initialize()
            repo.create("diagnostic-task", {})
            observer = ObservationService(repo)
            with observer.task("diagnostic-task"):
                with patch.object(repo, "begin_span", side_effect=OSError("private-store-detail")) if broken_store else nullcontext():
                    with self.assertRaises(type(error)) as raised:
                        with repair_stage("apply_revision", {"round_number": 1}):
                            raise error
            self.assertIs(raised.exception, error, "观测不得替换业务异常或取消信号")
            with repo.connection() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM spans")]
            if broken_store:
                self.assertIn("diagnostic-task", observer.incomplete)
            return rows

    def test_reference_diagnostics_are_bounded_and_hide_values_and_unknown_keys(self):
        from app.services.planning_v4_validator import ContractValidationError, ReferenceIssue
        errors = tuple(ReferenceIssue(
            "dangling_candidate", "revision.replacement_day.private_model_key", "private-model-value",
        ) for _ in range(10))
        rows = self.observe_error(ContractValidationError(errors))
        record = json.loads(rows[0]["output_data"])
        self.assertEqual(record["issue_count"], 10)
        self.assertTrue(record["diagnostics_truncated"])
        self.assertEqual(len(record["diagnostics"]), 8)
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual(record["diagnostics"][0]["path"], "revision.replacement_day.field")

    def test_schema_error_does_not_store_input_or_unknown_field_name(self):
        from pydantic import ValidationError
        try:
            ProposalActivity.model_validate({"private_model_key": "private-model-value"})
        except ValidationError as error:
            rows = self.observe_error(error)
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual(json.loads(rows[0]["output_data"])["error_code"], "REPAIR_SCHEMA_INVALID")

    def test_acceptance_condition_is_specific_and_unexpected_exception_is_safe(self):
        from app.services.day_repair_service import DayRepairInputError
        rows = self.observe_error(DayRepairInputError("recalculation modified non-target route legs"))
        diagnostic = json.loads(rows[0]["output_data"])["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "recalculation_other_legs_changed")
        error = RuntimeError("private-model-value")
        error.code = "private-exception-code"
        rows = self.observe_error(error)
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual(json.loads(rows[0]["error"])["code"], "REPAIR_STAGE_FAILED")

    def test_observation_failure_and_cancellation_keep_original_exception(self):
        self.assertEqual(self.observe_error(ValueError("private-model-value"), broken_store=True), [])
        rows = self.observe_error(asyncio.CancelledError())
        self.assertEqual(rows[0]["status"], "failed")

    def test_collection_failure_is_recorded_without_claiming_revision_was_attempted(self):
        from app.services.day_repair_service import DayRepairCollectionResult
        from test_day_repair_service import setup_missing_dinner
        planning_input, facts, candidates, draft, report, _ = setup_missing_dinner()

        async def collect(_request):
            return DayRepairCollectionResult(
                stop_reason="technical_error", error_code="MASTER_TASK_PREFERENCE_INVALID",
                fact_store=facts, candidates=candidates,
            )

        async def must_not_run(_request):
            self.fail("采集技术错误后，现行流程不会进入模型修订或交通重算")

        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "observation.sqlite3")
            repo.initialize()
            repo.create("diagnostic-task", {})
            with ObservationService(repo).task("diagnostic-task"):
                service = DayRepairService(collect=collect, revise=must_not_run, recalculate=must_not_run)
                result = asyncio.run(service.run_async(
                    planning_input, draft, facts, candidates, (), (), report,
                ))
            with repo.connection() as connection:
                rows = [dict(row) for row in connection.execute("SELECT * FROM spans")]
        self.assertEqual(result.draft, draft)
        rounds = [json.loads(r["output_data"]) for r in rows if r["name"] == "repair.round_result"]
        self.assertEqual(len(rounds), 3)
        self.assertTrue(all(r["error_code"] == "MASTER_TASK_PREFERENCE_INVALID" and not r["accepted"] for r in rounds))
        self.assertFalse(any(r["name"] == "repair.revise" for r in rows))


if __name__ == "__main__":
    unittest.main()
