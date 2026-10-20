"""S05：地点缺项、日级额度、技术恢复与有效安排保留的禁网回归。"""
import unittest

from app.models.planning_v4 import DayRevision, RepairState, ValidationIssue
from app.services.day_repair_service import (
    DayRepairCollectionResult, DayRepairError, DayRepairRecalculation,
    DayRepairService, _collection_scope,
)
from app.services.planning_v4_validation import is_required_place_gap, validate_daily_plan
from test_day_repair_service import (
    TARGET_DAY, add_dinner_candidate, proposal_day_from_current, setup_missing_dinner,
)
from test_planning_v4_validation import make_filled_draft, make_input, requirement


async def unexpected(_):
    raise AssertionError("adapter must not be called")


async def recalculate(request):
    return DayRepairRecalculation(
        draft=request.draft, facts=request.facts,
        report=validate_daily_plan(
            request.planning_input, request.draft, request.facts,
            request.candidates, repair_states=request.repair_states,
        ),
    )


def ready_case():
    planning_input, facts, candidates, draft, report, slot = setup_missing_dinner()
    facts, candidates = add_dinner_candidate(facts, candidates, slot.slot_id)
    return planning_input, facts, candidates, draft, report, slot


def revision_for(request, *, fill=True):
    candidate = next(c for c in request.candidates if c.candidate_id == "candidate-dinner-repair")
    return DayRevision(
        day_id=request.day.day_id,
        base_revision=request.draft.revision,
        revision=request.draft.revision + 1,
        based_on_report_revision=request.report.based_on_revision,
        preserve_activity_ids=request.preserve_activity_ids,
        source_issue_ids=tuple(issue.issue_id for issue in request.issues),
        replacement_day=proposal_day_from_current(
            request.day, added_candidate=candidate if fill else None,
            added_slot=request.issues[0].target_id if fill else None,
        ),
    )


class SimplifiedDayRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_slots_with_unplanned_requirement_do_not_search_or_revise(self):
        _, facts, candidates, _, _, _ = setup_missing_dinner()
        candidates = tuple(c.model_copy(update={"slot_ids": ()}) for c in candidates)
        planning_input = make_input(requirements=(requirement("must-bund", "must_visit", "外滩"),))
        draft = make_filled_draft(planning_input, candidates)
        report = validate_daily_plan(planning_input, draft, facts, candidates)
        self.assertEqual(report.requirements, ())
        self.assertFalse(any(issue.code == "must_visit_unplanned" for issue in report.issues))
        # 即使上游错误地把旧交通冲突标为可补查，业务入口也不能接受。
        stale = ValidationIssue(
            issue_id="stale-return", code="return_deadline_conflict",
            category="known_conflict", severity="error", scope="day",
            day_id=TARGET_DAY, message="旧交通冲突", triggers_repair=True,
            based_on_revision=draft.revision,
        )
        report = report.model_copy(update={"issues": (*report.issues, stale)})
        result = await DayRepairService(
            collect=unexpected, revise=unexpected, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.draft, draft)
        self.assertFalse(result.attempts)
        self.assertTrue(all(state.completed_rounds == 0 for state in result.repair_states))

    async def test_existing_candidates_fill_only_target_date_and_preserve_valid_activities(self):
        planning_input, facts, candidates, draft, report, slot = ready_case()
        requests = []

        async def revise(request):
            requests.append(request)
            self.assertEqual(request.modifiable_activity_ids, ())
            return revision_for(request)

        result = await DayRepairService(
            collect=unexpected, revise=revise, recalculate=recalculate,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.draft.revision, draft.revision + 1)
        for before, after in zip(draft.days, result.draft.days, strict=True):
            if before.day_id != TARGET_DAY:
                self.assertEqual(after, before)
            else:
                valid = tuple(activity for activity in before.activities if activity.kind != "free_time")
                preserved = tuple(activity for activity in after.activities if activity.activity_id in requests[0].preserve_activity_ids)
                self.assertEqual(preserved, valid)
                self.assertTrue(any(a.kind == "meal" and a.slot_id == slot.slot_id for a in after.activities))
                self.assertFalse(any(a.kind == "free_time" and a.slot_id == slot.slot_id for a in after.activities))
        self.assertEqual(next(s for s in result.repair_states if s.day_id == TARGET_DAY).completed_rounds, 1)
        self.assertTrue(all(s.completed_rounds == 0 for s in result.repair_states if s.day_id != TARGET_DAY))

    async def test_two_technical_retries_leave_all_three_business_rounds(self):
        planning_input, facts, candidates, draft, report, _ = ready_case()
        rounds = []

        async def revise(request):
            rounds.append(request.round_number)
            if len(rounds) <= 2:
                raise DayRepairError("MODEL_CALL_FAILED")
            return revision_for(request, fill=False)

        result = await DayRepairService(
            collect=unexpected, revise=revise, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        state = next(s for s in result.repair_states if s.day_id == TARGET_DAY)
        self.assertEqual(rounds, [1, 1, 1, 2, 3])
        self.assertEqual(state.completed_rounds, 3)
        self.assertEqual(state.stop_reason, "round_limit")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.draft, draft)
        self.assertEqual([a.outcome for a in result.attempts], ["technical_error"] * 2 + ["no_progress"] * 3)
        self.assertEqual(len({a.round_id for a in result.attempts}), 5)

    async def test_collection_timeout_without_usable_candidates_does_not_revise_or_consume(self):
        planning_input, facts, candidates, draft, report, _ = setup_missing_dinner()
        rounds = []

        async def collect(request):
            rounds.append(request.round_number)
            return DayRepairCollectionResult(
                stop_reason="deadline", fact_store=request.fact_store,
                candidates=request.candidates, area_hints=request.area_hints,
                task_results=request.task_results,
            )

        result = await DayRepairService(
            collect=collect, revise=unexpected, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        state = next(s for s in result.repair_states if s.day_id == TARGET_DAY)
        self.assertEqual(rounds, [1, 1, 1])
        self.assertEqual(state.completed_rounds, 0)
        self.assertEqual(state.stop_reason, "technical_error")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.draft, draft)

    async def test_invalid_recalculation_is_technical_recovery_then_one_business_round(self):
        planning_input, facts, candidates, draft, report, _ = ready_case()
        rounds = []
        recalculations = 0

        async def revise(request):
            rounds.append(request.round_number)
            return revision_for(request)

        async def flaky_recalculate(request):
            nonlocal recalculations
            recalculations += 1
            result = await recalculate(request)
            if recalculations <= 2:
                return result.model_copy(update={"draft": draft})
            return result

        result = await DayRepairService(
            collect=unexpected, revise=revise, recalculate=flaky_recalculate,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        self.assertEqual(rounds, [1, 1, 1])
        self.assertEqual(result.status, "complete")
        self.assertEqual(next(s for s in result.repair_states if s.day_id == TARGET_DAY).completed_rounds, 1)
        self.assertEqual([a.outcome for a in result.attempts], ["technical_error", "technical_error", "applied"])

    async def test_global_deadline_in_revision_or_recalculation_does_not_consume(self):
        for stage in ("revision", "recalculation"):
            with self.subTest(stage=stage):
                planning_input, facts, candidates, draft, report, _ = ready_case()

                async def fail(_):
                    raise DayRepairError("GLOBAL_TECHNICAL_DEADLINE", global_deadline=True)

                async def revise(request):
                    return revision_for(request)

                result = await DayRepairService(
                    collect=unexpected,
                    revise=fail if stage == "revision" else revise,
                    recalculate=fail,
                ).run_async(planning_input, draft, facts, candidates, (), (), report)
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.draft, draft)
                self.assertEqual(next(s for s in result.repair_states if s.day_id == TARGET_DAY).completed_rounds, 0)

    async def test_interrupted_attempt_preserves_used_business_budget(self):
        planning_input, facts, candidates, draft, report, _ = ready_case()
        report = report.model_copy(update={"repair_states": (RepairState(
            day_id=TARGET_DAY, completed_rounds=1,
            active_round_id="interrupted", status="active",
        ),)})
        result = await DayRepairService(
            collect=unexpected, revise=unexpected, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        state = next(s for s in result.repair_states if s.day_id == TARGET_DAY)
        self.assertEqual(state.completed_rounds, 1)
        self.assertEqual(state.stop_reason, "deadline")
        self.assertEqual(result.draft, draft)

    async def test_exhausted_date_does_not_start_a_fourth_round(self):
        planning_input, facts, candidates, draft, report, _ = ready_case()
        report = report.model_copy(update={"repair_states": (RepairState(
            day_id=TARGET_DAY, completed_rounds=3, status="stopped", stop_reason="round_limit",
        ),)})
        result = await DayRepairService(
            collect=unexpected, revise=unexpected, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), report)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.draft, draft)
        self.assertFalse(result.attempts)


class SimplifiedValidationTests(unittest.TestCase):
    def test_only_required_place_gaps_can_trigger_business_repair(self):
        planning_input, _, _, _, report, slot = ready_case()
        gap = next(i for i in report.issues if i.code == "missing_meal")
        self.assertTrue(is_required_place_gap(planning_input, gap))
        for updates in (
            {"code": "must_visit_unplanned", "scope": "plan", "target_id": "req"},
            {"code": "opening_hours_conflict", "category": "known_conflict"},
            {"code": "missing_sightseeing"}, {"category": "unknown_info"},
            {"day_id": "other"}, {"target_id": "absent"},
        ):
            with self.subTest(updates=updates):
                self.assertFalse(is_required_place_gap(planning_input, gap.model_copy(update=updates)))
        for eligibility in ("skipped", "conditional"):
            day = next(d for d in planning_input.days if d.day_id == TARGET_DAY)
            changed = day.model_copy(update={"slots": tuple(
                s.model_copy(update={"eligibility": eligibility}) if s.slot_id == slot.slot_id else s
                for s in day.slots
            )})
            changed_input = planning_input.model_copy(update={"days": tuple(
                changed if d.day_id == TARGET_DAY else d for d in planning_input.days
            )})
            self.assertFalse(is_required_place_gap(changed_input, gap))

    def test_collection_requires_same_city_sourced_category_correct_candidate(self):
        planning_input, facts, candidates, draft, report, slot = ready_case()
        day = next(d for d in draft.days if d.day_id == TARGET_DAY)
        constraint = next(d for d in planning_input.days if d.day_id == TARGET_DAY)
        gaps = tuple(i for i in report.issues if is_required_place_gap(planning_input, i))
        self.assertEqual(_collection_scope(planning_input, constraint, day, gaps, facts, candidates), (False, ()))
        dinner = next(p for p in facts.places if p.fact_id == "fact-dinner-repair")
        for updates in (
            {"city": "杭州"}, {"city": "上海新区"}, {"city": None},
            {"typecode": "110000", "type_name": "风景名胜区"},
            {"source_refs": ("absent",)}, {"location": None}, {"poi_id": "wrong"},
        ):
            with self.subTest(updates=updates):
                changed_facts = facts.model_copy(update={"places": tuple(
                    dinner.model_copy(update=updates) if p.fact_id == dinner.fact_id else p
                    for p in facts.places
                )})
                needs, targets = _collection_scope(planning_input, constraint, day, gaps, changed_facts, candidates)
                self.assertTrue(needs)
                self.assertEqual(targets[0].slot_ids, (slot.slot_id,))
                self.assertEqual(targets[0].role, "dining")
                self.assertEqual(targets[0].requirement_ids, ())

    def test_cross_day_repeat_is_feedback_without_search_or_automatic_deletion(self):
        planning_input, facts, candidates, _, _, _ = setup_missing_dinner()
        candidates = tuple(c.model_copy(update={"slot_ids": ()}) for c in candidates)
        draft = make_filled_draft(planning_input, candidates)
        before = draft.model_dump_json()
        report = validate_daily_plan(planning_input, draft, facts, candidates)
        repeats = [i for i in report.issues if i.code == "cross_day_sightseeing_repeat"]
        self.assertTrue(repeats)
        self.assertTrue(all(not i.triggers_repair for i in repeats))
        self.assertEqual(draft.model_dump_json(), before)
        explained = draft.model_copy(update={"days": tuple(day.model_copy(update={"activities": tuple(
            activity.model_copy(update={"recommendation_reason": "用户要求全天游玩，跨天再访"})
            if activity.kind == "sightseeing" else activity
            for activity in day.activities
        )}) for day in draft.days)})
        report = validate_daily_plan(planning_input, explained, facts, candidates)
        self.assertFalse(any(i.code == "cross_day_sightseeing_repeat" for i in report.issues))
        self.assertTrue(all(day.completeness == "complete" for day in report.days))


if __name__ == "__main__":
    unittest.main()
