"""上海三日：区域/偏好引用错误到补查中断的离线组合回归。"""
import asyncio
from copy import deepcopy
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from app.agents.execution import PlanningError
from app.agents.master_decider import MasterDecider
from app.models.planning_v4 import AreaHint, DayRevision, RepairState
from app.services.day_repair_service import DayRepairRecalculation, DayRepairService
from app.services.observation_service import ObservationService
from app.services.planning_dispatch_service import PlanningDispatchResult, PlanningDispatchService
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.planning_v4_validation import validate_daily_plan
from app.services.task_diagnostics import build_task_report
from app.services.task_repository import TaskRepository
from test_master_decider import DATES, FakeLLM, make_context, place_fact, task_spec
from test_day_repair_service import add_dinner_candidate, proposal_day_from_current, setup_missing_dinner


def reference_case(kind):
    facts, place = place_fact()
    area = AreaHint(area_hint_id="area-bund", label="外滩区域", anchor_poi_ids=(place.poi_id,),
                    center=place.location, basis_fact_ids=(place.fact_id,), basis="verified_place_facts")
    context = make_context(facts=facts, areas=(area,))
    good = {"action": "delegate", "reason": "查询景点", "tasks": [
        task_spec("repairable", DATES[0], "afternoon"), task_spec("untouched", DATES[1], "morning"),
    ]}
    if kind in {"unknown_area", "area_dependency"}:
        good["tasks"][0]["area_refs"] = [area.area_hint_id]
    else:
        good["tasks"][0]["preference_refs"] = ["城市漫步"]
    if kind == "area_dependency":
        good["tasks"][0]["dependencies"] = [{"kind": "area_hint", "reference": area.area_hint_id}]
    bad = deepcopy(good)
    if kind == "unknown_area":
        bad["tasks"][0]["area_refs"] = ["typo-area"]
    elif kind == "area_dependency":
        bad["tasks"][0]["area_refs"] = []
    elif kind == "duplicate_preference":
        bad["tasks"][0]["preference_refs"] *= 2
    else:
        bad["tasks"][0]["preference_refs"] = ["模型改写后的偏好"]
    return context, bad, good


def decider_for(replies):
    llm = FakeLLM([json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in replies])
    ids = iter(range(1000))
    return MasterDecider(llm=llm, request_timeout_seconds=2, id_factory=lambda: f"allocated-{next(ids)}"), llm


class ReferenceDecisionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline only"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_each_reference_condition_gets_one_specific_repair_and_keeps_other_task(self):
        for kind in ("unknown_area", "area_dependency", "duplicate_preference", "unknown_preference"):
            with self.subTest(condition=kind):
                context, bad, good = reference_case(kind)
                good["reason"] = "纠正引用后继续查询"
                decider, llm = decider_for([bad, good])
                result = decider.run(context)
                self.assertEqual(result.model_calls, 2)
                self.assertEqual(result.reference_repairs, 1)
                self.assertEqual(result.format_repairs, 0)
                self.assertEqual(len(result.decision.tasks), 2)
                feedback = json.loads(llm.calls[1][0][-1]["content"])
                self.assertEqual(feedback["type"], "repair_task_references")
                self.assertTrue(feedback["errors"])
                self.assertTrue(all(e["path"].startswith("tasks[0].") for e in feedback["errors"]))
                self.assertEqual(feedback["original_decision"]["tasks"][1], good["tasks"][1])
                self.assertIn("area-bund", feedback["allowed_area_refs"])
                self.assertIn("城市漫步", feedback["allowed_preference_refs"])

    def test_repair_merges_reference_fields_and_keeps_unaffected_task(self):
        context, bad, good = reference_case("unknown_area")
        changed = deepcopy(good)
        changed["tasks"][1]["objective"] = "改成另一天的其他目标"
        decider, llm = decider_for([bad, changed])
        result = decider.run(context)
        self.assertEqual(result.decision.tasks[1].subtask.objective, good["tasks"][1]["objective"])
        self.assertEqual(len(llm.calls), 2)

    def test_correcting_reference_does_not_bypass_fact_source_validation(self):
        context, bad, good = reference_case("unknown_area")
        good["areas"] = [{"local_ref": "made-up-area", "label": "未核实区域", "basis_fact_ids": ["nonexistent"]}]
        good["tasks"][0]["area_refs"] = ["made-up-area"]
        decider, llm = decider_for([bad, good])
        with self.assertRaises(PlanningError) as caught:
            decider.run(context)
        self.assertEqual(caught.exception.code, "MASTER_TASK_AREA_UNKNOWN")
        self.assertEqual(len(llm.calls), 2)
        decider, llm = decider_for([bad, bad, good])
        with self.assertRaises(PlanningError) as caught:
            decider.run(context)
        self.assertEqual(caught.exception.code, "MASTER_TASK_AREA_UNKNOWN")
        self.assertEqual(len(llm.calls), 2)

    def test_reference_and_format_repairs_share_one_extra_call(self):
        context, bad, good = reference_case("unknown_preference")
        for replies in (["not json", bad, good], [bad, "not json", good]):
            with self.subTest(replies=replies[:2]):
                decider, llm = decider_for(replies)
                with self.assertRaises(PlanningError):
                    decider.run(context)
                self.assertEqual(len(llm.calls), 2)

    def test_area_alias_repair_updates_meal_binding_but_preserves_real_anchor(self):
        context, _, _ = reference_case("unknown_area")
        slot_id = f"slot-{DATES[1].isoformat()}-lunch"
        good = {"action": "delegate", "reason": "查午餐", "tasks": [{
            **task_spec("dining", DATES[1], "lunch", role="dining", target=1),
            "area_refs": ["area-bund"], "dependencies": [{"kind": "area_hint", "reference": "area-bund"}],
            "meal_areas": [{"slot_id": slot_id, "area_ref": "area-bund", "anchor_poi_id": "poi-bund"}],
        }]}
        bad = deepcopy(good)
        bad["tasks"][0]["area_refs"] = ["typo"]
        bad["tasks"][0]["dependencies"][0]["reference"] = "typo"
        bad["tasks"][0]["meal_areas"][0]["area_ref"] = "typo"
        good["tasks"][0]["meal_areas"][0]["anchor_poi_id"] = "model-tried-to-change-anchor"
        decider, _ = decider_for([bad, good])
        result = decider.run(context)
        meal = result.decision.tasks[0].meal_areas[0]
        self.assertEqual(meal.area_hint_id, "area-bund")
        self.assertEqual(meal.anchor_poi_id, "poi-bund")

    def test_missing_repair_target_is_rejected_without_third_call(self):
        context, bad, good = reference_case("unknown_preference")
        good["tasks"].pop(0)
        decider, llm = decider_for([bad, good])
        with self.assertRaises(PlanningError) as caught:
            decider.run(context)
        self.assertEqual(caught.exception.code, "MASTER_REFERENCE_REPAIR_TARGET_INVALID")
        self.assertEqual(len(llm.calls), 2)

    def test_feedback_details_are_logged_but_original_decision_is_not(self):
        context, bad, good = reference_case("unknown_preference")
        bad["tasks"][0]["preference_refs"] = ["private-model-only-marker"]
        decider, _ = decider_for([bad, good])
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "tasks.sqlite3")
            repo.initialize(); repo.create("offline-reference", {})
            with ObservationService(repo).task("offline-reference"):
                decider.run(context)
            report = build_task_report(repo.path, "offline-reference")
        event = next(e for e in report["events"] if e["name"] == "master.decision.reference_repair")
        self.assertEqual(event["diagnostics"][0]["path"], "tasks[0].preference_refs")
        self.assertNotIn("private-model-only-marker", json.dumps(report))
        self.assertNotIn("original_decision", json.dumps(report))


class ReferenceCollectionRecoveryTests(unittest.TestCase):
    def run_case(self, code_kind, *, include_candidate=True, corrupt=False, completed_rounds=0,
                 forged_error=False, source_code=None, revise_failures=0):
        planning_input, facts, candidates, draft, report, missing_slot = setup_missing_dinner()
        if completed_rounds:
            report = report.model_copy(update={"repair_states": (RepairState(
                day_id=missing_slot.day_id, completed_rounds=completed_rounds, status="open",
            ),)})
        next_facts, next_candidates = add_dinner_candidate(facts, candidates, missing_slot.slot_id)
        if not include_candidate:
            next_facts, next_candidates = facts, candidates
        if corrupt:
            next_candidates = (*candidates, next_candidates[-1].model_copy(update={"place_fact_id": "missing-fact"}))
        seen = {"collect": 0, "revise": 0, "llms": []}

        class Dispatch:
            async def run_async(self, context):
                if forged_error or source_code:
                    return PlanningDispatchResult(
                        request_id=context.request_id, run_id=context.run_id,
                        context_revision=context.context_revision, base_revision=context.base_revision,
                        action="delegate", stop_reason="technical_error", reason="offline",
                        fact_store=next_facts, candidate_refs=next_candidates,
                        error={"code": source_code or "MASTER_TASK_AREA_UNKNOWN", "step": "master_decision", "message": "offline"},
                    )
                bad = {"action": "delegate", "reason": "补查晚餐", "tasks": [{
                    "local_ref": "dinner", "role": "dining", "objective": "补查餐馆",
                    "day_ids": [missing_slot.day_id], "slot_ids": [missing_slot.slot_id],
                    **({"area_refs": ["unknown-area"]} if code_kind == "area" else {"preference_refs": ["unknown-preference"]}),
                }]}
                decider, llm = decider_for([bad, bad])
                seen["llms"].append(llm)
                return await PlanningDispatchService(decider=decider).run_async(context.model_copy(update={
                    "fact_store": next_facts, "candidate_refs": next_candidates,
                }))

        pipeline = PlanningV4Pipeline(dispatch_service_factory=lambda _: Dispatch())
        revisions = [1]

        async def collect(request):
            seen["collect"] += 1
            return await pipeline._collect_for_repair(request, task_id=draft.source_run_id,
                deadline=time.monotonic() + 20, collection_revision=revisions, cancel_event=Event())

        async def revise(request):
            seen["revise"] += 1
            if seen["revise"] <= revise_failures:
                from app.services.day_repair_service import DayRepairError
                raise DayRepairError("MODEL_CALL_FAILED")
            candidate = next(c for c in request.candidates if c.candidate_id == "candidate-dinner-repair")
            return DayRevision(day_id=request.day.day_id, base_revision=request.draft.revision,
                revision=request.draft.revision + 1, based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(request.day, added_candidate=candidate, added_slot=missing_slot.slot_id),
                preserve_activity_ids=request.preserve_activity_ids, source_issue_ids=tuple(i.issue_id for i in request.issues))

        async def recalculate(request):
            return DayRepairRecalculation(draft=request.draft, facts=request.facts,
                report=validate_daily_plan(request.planning_input, request.draft, request.facts, request.candidates,
                                            repair_states=request.repair_states))

        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "observations.sqlite3")
            repo.initialize(); repo.create("offline-reference", {})
            with ObservationService(repo).task("offline-reference"), patch("socket.socket.connect", side_effect=AssertionError("offline only")):
                result = asyncio.run(DayRepairService(collect=collect, revise=revise, recalculate=recalculate).run_async(
                    planning_input, draft, facts, candidates, (), (), report))
            diagnosis = build_task_report(repo.path, "offline-reference")
            with closing(sqlite3.connect(repo.path)) as connection:
                seen["recovery_logs"] = [json.loads(row[0]) for row in connection.execute(
                    "select output_data from spans where name='repair.technical_recovery' order by started_at")]
        return result, seen, diagnosis, draft

    def test_exhausted_reference_repair_can_still_use_verified_dinner_candidate(self):
        for kind in ("area", "preference"):
            with self.subTest(kind=kind):
                result, seen, diagnosis, initial = self.run_case(kind)
                self.assertEqual((seen["collect"], seen["revise"]), (1, 1))
                self.assertEqual(result.status, "complete")
                self.assertEqual([a.outcome for a in result.attempts], ["applied"])
                self.assertEqual(len(seen["llms"][0].calls), 2)
                self.assertEqual(result.draft.days[0], initial.days[0])
                self.assertEqual(result.draft.days[2], initial.days[2])
                self.assertTrue(any(e["name"] == "repair.collection_recovery" for e in diagnosis["events"]))

    def test_no_usable_candidate_does_not_call_revision_and_does_not_loop_forever(self):
        result, seen, _, initial = self.run_case("preference", include_candidate=False)
        self.assertEqual((seen["collect"], seen["revise"]), (3, 0))
        self.assertEqual(result.draft, initial)
        self.assertEqual(len(result.attempts), 3)

    def test_last_round_uses_verified_candidate_before_stopping(self):
        result, seen, _, _ = self.run_case("preference", completed_rounds=2)
        self.assertEqual(seen["revise"], 1)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.attempts[0].round_number, 3)

    def test_corrupt_snapshot_is_not_recovered(self):
        result, seen, _, initial = self.run_case("area", corrupt=True)
        self.assertEqual(seen["revise"], 0)
        self.assertEqual(result.status, "technical_error")
        self.assertEqual(result.draft, initial)

    def test_error_code_alone_cannot_authorize_an_unverified_snapshot(self):
        result, seen, _, initial = self.run_case("area", corrupt=True, forged_error=True)
        self.assertEqual(seen["revise"], 0)
        self.assertEqual(result.status, "technical_error")
        self.assertEqual(result.error_code, "CANDIDATE_SOURCE_INVALID")
        self.assertEqual(result.draft, initial)

    def test_provider_failure_with_new_candidate_continues_last_business_round(self):
        result, seen, diagnosis, initial = self.run_case("area", completed_rounds=2, source_code="MASTER_LLM_FAILED")
        self.assertEqual((seen["collect"], seen["revise"]), (1, 1))
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.draft.revision, initial.revision + 1)
        self.assertEqual(result.attempts[0].round_number, 3)
        self.assertTrue(any(e["name"] == "repair.collection_recovery" for e in diagnosis["events"]))

    def test_two_revision_transport_errors_do_not_consume_remaining_business_round(self):
        result, seen, diagnosis, initial = self.run_case(
            "area", completed_rounds=2, source_code="MASTER_LLM_FAILED", revise_failures=2)
        self.assertEqual((seen["collect"], seen["revise"]), (1, 3))
        self.assertEqual(result.status, "complete")
        self.assertEqual([a.round_number for a in result.attempts], [3, 3, 3])
        self.assertEqual([a.outcome for a in result.attempts], ["technical_error", "technical_error", "applied"])
        self.assertEqual(result.draft.revision, initial.revision + 1)
        self.assertEqual(sum(e["name"] == "repair.technical_recovery" for e in diagnosis["events"]), 2)
        self.assertEqual([log["technical_failure_count"] for log in seen["recovery_logs"]], [1, 2])
        self.assertTrue(all(log["completed_business_rounds"] == 2 and not log["business_round_consumed"]
                            and log["next_business_round"] == 3 for log in seen["recovery_logs"]))

    def test_provider_failure_without_new_coverage_stops_with_business_budget_intact(self):
        result, seen, diagnosis, initial = self.run_case(
            "area", include_candidate=False, completed_rounds=2, source_code="MASTER_LLM_FAILED")
        self.assertEqual((seen["collect"], seen["revise"]), (3, 0))
        self.assertEqual(result.draft, initial)
        state = next(s for s in result.repair_states if s.day_id == "day-2026-10-11")
        self.assertEqual(state.completed_rounds, 2)
        self.assertEqual(state.stop_reason, "technical_error")
        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(sum(e["name"] == "repair.technical_recovery" for e in diagnosis["events"]), 3)
        self.assertEqual([log["outcome"] for log in seen["recovery_logs"]], ["retry", "retry", "exhausted"])
        self.assertIsNone(seen["recovery_logs"][-1]["next_business_round"])


if __name__ == "__main__":
    unittest.main()
