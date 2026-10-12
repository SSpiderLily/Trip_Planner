"""初版候选引用与搜索时段：合成上海三日，禁止任何网络调用。"""
import json
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.agents.daily_plan_generator import DailyPlanGenerator
from app.agents.execution import PlanningError
from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter
from app.services.planning_dispatch_service import PlanningDispatchResult
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.planning_v4_validator import validate_preview
from app.services.task_diagnostics import build_task_report, render_task_report
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from app.services.planning_v4_validation import validate_daily_plan
from test_daily_plan_generator import (
    FakeLLM, RUN_ID, make_input, make_facts, make_candidates, make_areas, make_model_output,
)


class InitialCandidateRecoveryTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("offline only"))
        network.start()
        self.addCleanup(network.stop)
        self.planning_input = make_input()
        self.facts = make_facts()
        self.areas = make_areas()
        self.candidates = make_candidates()

    def generate(self, activities, patches=(), candidates=None, facts=None):
        llm = FakeLLM([make_model_output(self.planning_input, activities=activities), *patches])
        candidates = candidates if candidates is not None else self.candidates
        facts = facts if facts is not None else self.facts
        result = DailyPlanGenerator(llm=llm).generate(
            self.planning_input, candidates, facts, self.areas, (), source_run_id=RUN_ID,
        )
        validate_daily_plan(self.planning_input, result.draft, facts, candidates)
        return result, llm

    def test_search_slot_does_not_drop_same_poi_in_later_period_or_date(self):
        activities = {
            day_id: ({"proposal_ref": f"visit-{index}", "candidate_ref": "candidate-bund-alias",
                      "slot_id": slot_id, "kind": "sightseeing"},)
            for index, (day_id, slot_id) in enumerate((
                ("day-2026-10-11", "slot-2026-10-11-evening"),
                ("day-2026-10-12", "slot-2026-10-12-morning"),
            ))
        }
        result, llm = self.generate(activities)
        self.assertEqual([len(day.activities) for day in result.draft.days], [0, 1, 1])
        self.assertEqual(result.recovery_notices, ())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(self.candidates[1].slot_ids, ("slot-2026-10-11-morning",))

    def test_missing_reference_can_reuse_queried_sight_and_preserves_valid_slot_order(self):
        activities = {"day-2026-10-12": (
            {"proposal_ref": "good-meal", "candidate_ref": "candidate-meal",
             "slot_id": "slot-2026-10-12-lunch", "kind": "meal"},
            {"proposal_ref": "missing-ref", "candidate_ref": None,
             "slot_id": "slot-2026-10-12-afternoon", "kind": "sightseeing"},
        )}
        repair = json.dumps({"repairs": [{"repair_id": "repair-0", "action": "replace",
            "replacement": {"candidate_ref": "candidate-bund-alias", "kind": "sightseeing"}}]})
        result, llm = self.generate(activities, (repair,))
        self.assertEqual(len(result.draft.days[2].activities), 2)
        self.assertEqual([a.slot_id for a in result.draft.days[2].activities],
                         ["slot-2026-10-12-lunch", "slot-2026-10-12-afternoon"])
        self.assertTrue(all("duration_s" not in a.model_dump() for a in result.draft.days[2].activities))
        self.assertEqual(result.draft.days[2].activities[0].candidate_ref, "candidate-meal")
        self.assertEqual(result.recovery_notices, ())
        self.assertEqual(len(llm.calls), 2)
        target = json.loads(llm.calls[1][0][-1]["content"])["repair_targets"][0]
        self.assertIn("地点活动必须引用", target["condition"])

    def test_prompt_links_candidates_to_places_and_excludes_unselected_search_results(self):
        raw_only = self.facts.places[1].model_copy(update={
            "fact_id": "fact-not-selected", "poi_id": "poi-not-selected", "name": "未入选的查询结果",
        })
        facts = self.facts.model_copy(update={"places": (*self.facts.places, raw_only)})
        _, llm = self.generate({}, facts=facts)
        payload = json.loads(llm.calls[0][0][1]["content"])
        self.assertNotIn("fact-not-selected", {place["fact_id"] for place in payload["place_facts"]})
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["place"]["fact_id"], candidate["place_fact_id"])
        self.assertEqual(candidate["place"]["name"], self.facts.places[0].name)
        evening = next(row for row in payload["slot_candidate_options"] if row["slot_id"] == "slot-2026-10-11-evening")
        self.assertIn("candidate-bund-alias", evening["candidate_ids"])

    def test_missing_meal_reference_without_candidate_is_logged_as_no_candidate(self):
        candidates = tuple(c for c in self.candidates if c.category != "meal")
        activities = {"day-2026-10-11": ({"proposal_ref": "missing-meal-ref",
            "candidate_ref": None, "slot_id": "slot-2026-10-11-lunch", "kind": "meal"},)}
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "test.sqlite3")
            repository.initialize()
            repository.create("offline-missing-ref", {})
            with ObservationService(repository).task("offline-missing-ref"):
                result, llm = self.generate(activities, candidates=candidates)
            rows = repository.spans("offline-missing-ref")
            row = next(row for row in rows if row["name"] == "validation.daily_plan_local_repair")
            diagnostic = repository.span("offline-missing-ref", row["span_id"])["output_data"]["diagnostics"][0]
            report = build_task_report(repository.path, "offline-missing-ref")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.recovery_notices), 1)
        self.assertIn("slot-2026-10-11-lunch", result.draft.days[1].unfilled_slot_ids)
        self.assertEqual(diagnostic["eligible_candidate_count"], 0)
        self.assertEqual(diagnostic["outcome"], "no_verified_candidate")
        self.assertIn("可用候选 0；处理结果 no_verified_candidate", render_task_report(report))

    def test_invalid_reference_type_is_not_reported_as_missing(self):
        from app.agents.daily_plan_activity_repair import build_structural_snapshot
        payload = json.loads(make_model_output(self.planning_input, activities={"day-2026-10-11": (
            {"proposal_ref": "bad-type", "candidate_ref": ["private-untrusted-reference"],
             "slot_id": "slot-2026-10-11-morning", "kind": "sightseeing"},
        )}))
        snapshot = build_structural_snapshot(payload, self.planning_input)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.targets[0].reason_codes, ("candidate_reference_invalid",))

    def test_recovery_log_distinguishes_patch_outcomes_without_recording_raw_response(self):
        activities = {"day-2026-10-11": (
            {"proposal_ref": "secret-proposal", "candidate_ref": None,
             "slot_id": "slot-2026-10-11-morning", "kind": "sightseeing"},
        )}
        cases = (
            ("invalid_or_missing_patch", "secret-invalid-json"),
            ("model_left_unfilled", json.dumps({"repairs": [{"repair_id": "repair-0", "action": "leave_unfilled"}]})),
            ("patch_candidate_invalid", json.dumps({"repairs": [{"repair_id": "repair-0", "action": "replace",
                "replacement": {"candidate_ref": "secret-candidate", "kind": "sightseeing"}}]})),
            ("repair_call_unavailable", RuntimeError("secret-provider-error")),
            ("repaired", json.dumps({"repairs": [{"repair_id": "repair-0", "action": "replace",
                "replacement": {"candidate_ref": "candidate-bund", "kind": "sightseeing"}}]})),
        )
        for expected, response in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                repository = TaskRepository(Path(directory) / "test.sqlite3")
                repository.initialize()
                repository.create("offline-patch", {})
                with ObservationService(repository).task("offline-patch"):
                    result, llm = self.generate(activities, (response,))
                report = build_task_report(repository.path, "offline-patch")
                event = next(e for e in report["events"] if e["name"] == "validation.daily_plan_local_repair")
                detail = event["diagnostics"][0]
                self.assertEqual(detail["outcome"], expected)
                self.assertEqual(detail["eligible_candidate_count"], 2)
                self.assertEqual(bool(result.recovery_notices), expected != "repaired")
                self.assertEqual(len(llm.calls), 2)
                self.assertNotIn("secret-", json.dumps(report))

    def test_reused_sights_survive_actual_pipeline_preview_validation(self):
        from test_planning_v4_pipeline import FakeInterpreter, FakeRouteService
        tracker = {}
        facts, candidates, areas = self.facts, self.candidates, self.areas

        class Dispatch:
            async def run_async(self, context):
                return PlanningDispatchResult(
                    request_id=context.request_id, run_id=context.run_id,
                    context_revision=context.context_revision, base_revision=context.base_revision,
                    action="plan", stop_reason="plan", reason="offline fixture",
                    fact_store=facts, candidate_refs=candidates, area_hints=areas,
                )

        class Adapter:
            async def generate(self, planning_input, *args, **kwargs):
                activities = {"day-2026-10-11": (
                    {"proposal_ref": "morning", "candidate_ref": "candidate-bund-alias",
                     "slot_id": "slot-2026-10-11-morning", "kind": "sightseeing"},
                    {"proposal_ref": "evening", "candidate_ref": "candidate-bund-alias",
                     "slot_id": "slot-2026-10-11-evening", "kind": "sightseeing"},
                )}
                llm = FakeLLM([make_model_output(planning_input, activities=activities)])
                execution = await DailyPlanGeneratorAdapter(DailyPlanGenerator(llm=llm)).generate(
                    planning_input, *args, **kwargs,
                )
                tracker["execution"] = execution
                tracker["llm"] = llm
                return execution

            async def revise_day(self, *args, **kwargs):
                raise PlanningError("OFFLINE_REVISION_UNAVAILABLE", "合成修订暂不可用")

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: Dispatch(),
            daily_plan_adapter_factory=lambda _timeout: Adapter(),
            current_route_service_factory=lambda: FakeRouteService(tracker),
        )
        result = asyncio.run(pipeline.run(
            self.planning_input.request, task_id="offline-slot-task", plan_id="offline-slot-plan",
            progress=lambda _update: None,
        ))
        validate_preview(result.snapshot.preview)
        day = result.snapshot.preview.draft.days[1]
        self.assertEqual(day.activities, tracker["execution"].draft.days[1].activities)
        self.assertEqual(len(day.activities), 2)
        self.assertEqual(len(tracker["llm"].calls), 1)

    def test_day_repair_reuses_sight_without_collecting_or_rejecting_new_slot(self):
        from app.services.day_repair_service import _collection_scope, IssueDayAssignment
        initial, _ = self.generate({})
        report = validate_daily_plan(self.planning_input, initial.draft, self.facts, self.candidates)
        constraint, day = self.planning_input.days[2], initial.draft.days[2]
        issue = next(issue for issue in report.issues
                     if issue.code == "missing_sightseeing" and issue.target_id == "slot-2026-10-12-morning")
        needs_collection, targets = _collection_scope(
            self.planning_input, constraint, day, (issue,), self.facts, self.candidates,
        )
        self.assertFalse(needs_collection)
        self.assertEqual(targets, ())
        output = json.loads(make_model_output(self.planning_input, activities={day.day_id: (
            {"proposal_ref": "new-visit", "candidate_ref": "candidate-bund-alias",
             "slot_id": issue.target_id, "kind": "sightseeing"},
        )}))
        llm = FakeLLM([json.dumps({"replacement_day": output["days"][2]})])
        result = DailyPlanGenerator(llm=llm).revise_day(
            self.planning_input, initial.draft, report, day.day_id, (issue,),
            (IssueDayAssignment(issue_id=issue.issue_id, assigned_day_id=day.day_id, reason="issue_day"),),
            self.candidates, self.facts, self.areas, (), preserve_activity_ids=(),
        )
        self.assertEqual(result.revision.replacement_day.activities[0].candidate_ref, "candidate-bund-alias")
        payload = json.loads(llm.calls[0][0][-1]["content"])
        candidate = next(c for c in payload["candidates"] if c["candidate_id"] == "candidate-bund-alias")
        self.assertIn(issue.target_id, candidate["applicable_slot_ids"])
        self.assertEqual(len(llm.calls), 1)

    def test_dining_area_slot_binding_is_still_enforced(self):
        candidates = tuple(c.model_copy(update={"slot_ids": ("slot-2026-10-11-lunch",)})
                           if c.category == "meal" else c for c in self.candidates)
        activities = {"day-2026-10-12": ({"proposal_ref": "other-area-meal",
            "candidate_ref": "candidate-meal", "slot_id": "slot-2026-10-12-lunch", "kind": "meal"},)}
        result, llm = self.generate(activities, candidates=candidates)
        self.assertEqual(result.draft.days[2].activities, ())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result.recovery_notices[0].reason_code, "candidate_incompatible")


if __name__ == "__main__":
    unittest.main()
