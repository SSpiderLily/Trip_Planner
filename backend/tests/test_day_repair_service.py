"""F09b 按日补查与修订状态机离线测试。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
import time
import unittest

from pydantic import ValidationError

from app.agents.daily_plan_generator import DailyPlanGenerator
from app.agents.execution import PlanningError
from app.models.planning_v4 import (
    CandidateRef,
    DayRevision,
    DraftActivity,
    DraftDay,
    FactStore,
    ProposalActivity,
    ProposalDay,
    RepairState,
    ValidationIssue,
    ValidationReport,
)
from app.models.planning_v4.facts import FactSource, MapCoordinate, PlaceFact
from app.services.day_repair_service import (
    DayRepairCollectionRequest,
    DayRepairCollectionResult,
    DayRepairError,
    DayRepairInputError,
    DayRepairRecalculation,
    DayRepairRecalculationRequest,
    DayRepairService,
    DayRevisionRequest,
    IssueDayAssignment,
    apply_day_revision,
    assign_repair_issues,
)
from app.services.llm_service import LLMCompletion
from app.services.planning_v4_timing import calculate_plan_timing
from app.services.planning_v4_validation import validate_daily_plan
from app.services.planning_v4_validator import ContractValidationError, validate_day_revision
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter
from test_planning_v4_validation import make_facts_and_candidates, make_filled_draft, make_input, requirement


TARGET_DAY = "day-2026-10-11"


def setup_missing_dinner(*, include_placeholder=True):
    planning_input = make_input()
    facts, candidates = make_facts_and_candidates()
    constraint_by_day = {day.day_id: day for day in planning_input.days}
    missing_slot = next(slot for slot in constraint_by_day[TARGET_DAY].slots if slot.period == "dinner")
    meal_slot_ids = tuple(
        slot.slot_id
        for day in planning_input.days
        for slot in day.slots
        if slot.period in {"breakfast", "lunch", "dinner"} and slot.slot_id != missing_slot.slot_id
    )
    sightseeing_slot_ids = tuple(
        slot.slot_id for day in planning_input.days for slot in day.slots
        if slot.period in {"morning", "afternoon", "evening"}
    )
    candidates = tuple(
        candidate.model_copy(update={
            "slot_ids": meal_slot_ids if candidate.category == "meal" else sightseeing_slot_ids,
        })
        for candidate in candidates
    )
    source = facts.sources[0]
    places = tuple(place.model_copy(update={
        "city": "上海市",
        "type_name": "风景名胜区" if place.poi_id == "poi-sight" else "中餐厅",
        "typecode": "110000" if place.poi_id == "poi-sight" else "050000",
    }) for place in facts.places)
    facts = FactStore(sources=facts.sources, places=places)
    draft = make_filled_draft(planning_input, candidates)
    old_day = next(day for day in draft.days if day.day_id == TARGET_DAY)
    activities = tuple(item for item in old_day.activities if item.slot_id != missing_slot.slot_id)
    if include_placeholder:
        activities += (DraftActivity(
            activity_id="free-dinner-placeholder",
            slot_id=missing_slot.slot_id,
            kind="free_time",
            duration_s=1800,
            duration_source="model",
        ),)
    target_day = old_day.model_copy(update={"activities": activities})
    draft = draft.model_copy(update={
        "days": tuple(target_day if day.day_id == TARGET_DAY else day for day in draft.days),
    })
    timing = calculate_plan_timing(planning_input, draft, facts)
    report = validate_daily_plan(planning_input, draft, facts, candidates, timing)
    return planning_input, facts, candidates, draft, timing, report, missing_slot


def add_dinner_candidate(facts, candidates, slot_id):
    source = FactSource(
        tool_call_id="repair-dinner-source",
        tool_name="offline_fixture",
        api_version="v4",
        queried_at=datetime.fromisoformat("2026-10-09T10:00:00+08:00"),
        effective_params={"date": "2026-10-11", "slot_id": slot_id},
    )
    place = PlaceFact(
        fact_id="fact-dinner-repair",
        poi_id="poi-dinner-repair",
        name="补查餐馆",
        city="上海市",
        type_name="中餐厅",
        typecode="050000",
        location=MapCoordinate(lng=121.48, lat=31.23),
        source_refs=(source.tool_call_id,),
    )
    candidate = CandidateRef(
        candidate_id="candidate-dinner-repair",
        category="meal",
        poi_id=place.poi_id,
        place_fact_id=place.fact_id,
        slot_ids=(slot_id,),
    )
    return (
        FactStore(
            sources=(*facts.sources, source),
            places=(*facts.places, place),
        ),
        (*candidates, candidate),
    )


def proposal_day_from_current(day, *, added_candidate=None, added_slot=None, change_existing=False):
    activities = []
    for activity in day.activities:
        if activity.kind == "free_time" and activity.slot_id == added_slot:
            # Intentionally include the replaceable placeholder; application must remove
            # it when the new real activity occupies that same slot.
            pass
        slot_id = activity.slot_id
        recommendation_reason = activity.recommendation_reason
        if change_existing and activity.kind == "meal" and activity.slot_id != added_slot:
            recommendation_reason = "模型试图改写已有有效活动"
        activities.append(ProposalActivity(
            proposal_ref=activity.activity_id,
            existing_activity_id=activity.activity_id,
            candidate_ref=activity.candidate_ref,
            slot_id=slot_id,
            kind=activity.kind,
            duration_s=activity.duration_s,
            requirement_ids=activity.requirement_ids,
            recommendation_reason=recommendation_reason,
        ))
    if added_candidate is not None and added_slot is not None:
        activities.append(ProposalActivity(
            proposal_ref="proposal-new-dinner",
            candidate_ref=added_candidate.candidate_id,
            slot_id=added_slot,
            kind="meal",
            duration_s=3600,
        ))
    return ProposalDay(
        day_id=day.day_id,
        day_date=day.day_date,
        theme=day.theme,
        activities=tuple(activities),
        unfilled_slot_ids=(),
        lodging=None,
        weather_advice=day.weather_advice,
    )


class DayRepairServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recalculation_must_bind_timing_and_preserve_proposed_target_day(self):
        planning_input, facts, candidates, draft, _, _, _ = setup_missing_dinner()
        revision = draft.revision + 1
        proposed = draft.model_copy(update={
            "revision": revision,
            "base_revision": draft.revision,
            "legs": tuple(
                leg.model_copy(update={"plan_revision": revision})
                for leg in draft.legs
            ),
        })
        timing = calculate_plan_timing(planning_input, proposed, facts)
        report = validate_daily_plan(
            planning_input, proposed, facts, candidates, timing,
        )

        stale_timing = timing.model_copy(update={"based_on_revision": draft.revision})
        with self.assertRaisesRegex(DayRepairInputError, "timing"):
            DayRepairService._validate_recalculation(
                draft, proposed,
                DayRepairRecalculation(
                    draft=proposed, facts=facts,
                    timing=stale_timing, report=report,
                ),
                TARGET_DAY,
            )

        target = next(day for day in proposed.days if day.day_id == TARGET_DAY)
        changed_activity = target.activities[0].model_copy(update={
            "recommendation_reason": "回调擅自改写已校验活动",
        })
        changed_target = target.model_copy(update={
            "activities": (changed_activity, *target.activities[1:]),
        })
        changed_draft = proposed.model_copy(update={
            "days": tuple(
                changed_target if day.day_id == TARGET_DAY else day
                for day in proposed.days
            ),
        })
        changed_timing = calculate_plan_timing(planning_input, changed_draft, facts)
        changed_report = validate_daily_plan(
            planning_input, changed_draft, facts, candidates, changed_timing,
        )
        with self.assertRaisesRegex(DayRepairInputError, "target day"):
            DayRepairService._validate_recalculation(
                draft, proposed,
                DayRepairRecalculation(
                    draft=changed_draft, facts=facts,
                    timing=changed_timing, report=changed_report,
                ),
                TARGET_DAY,
            )

    async def test_run_binding_rejects_wrong_rules_and_missing_source_run(self):
        planning_input, facts, candidates, draft, timing, report, _ = setup_missing_dinner()
        with self.assertRaisesRegex(DayRepairInputError, "rule_version"):
            DayRepairService._validate_run_binding(
                planning_input,
                draft.model_copy(update={"rule_version": "unbound-rules"}),
                timing,
                report,
            )
        with self.assertRaisesRegex(DayRepairInputError, "source_run_id"):
            DayRepairService._validate_run_binding(
                planning_input,
                draft.model_copy(update={"source_run_id": "  "}),
                timing,
                report,
            )

    async def test_missing_dinner_repairs_only_target_day_and_replaces_free_time(self):
        planning_input, facts, candidates, draft, timing, report, missing_slot = setup_missing_dinner()
        seen_collect: list[DayRepairCollectionRequest] = []
        seen_revision: list[DayRevisionRequest] = []

        async def collect(request):
            seen_collect.append(request)
            next_facts, next_candidates = add_dinner_candidate(
                request.fact_store, request.candidates, missing_slot.slot_id,
            )
            return DayRepairCollectionResult(
                stop_reason="target_met",
                fact_store=next_facts,
                candidates=next_candidates,
                area_hints=request.area_hints,
                task_results=request.task_results,
            )

        async def revise(request):
            seen_revision.append(request)
            new_candidate = next(item for item in request.candidates if item.candidate_id == "candidate-dinner-repair")
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(
                    request.day,
                    added_candidate=new_candidate,
                    added_slot=missing_slot.slot_id,
                ),
                preserve_activity_ids=request.preserve_activity_ids,
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def recalculate(request):
            next_timing = calculate_plan_timing(request.planning_input, request.draft, request.facts)
            next_report = validate_daily_plan(
                request.planning_input, request.draft, request.facts,
                request.candidates, next_timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft, facts=request.facts,
                timing=next_timing, report=next_report,
            )

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=recalculate,
            id_factory=lambda: "repair-round-1",
        ).run_async(
            planning_input, draft, facts, candidates, (), (), timing, report,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.draft.revision, draft.revision + 1)
        self.assertEqual(len(seen_collect), 1)
        self.assertEqual(seen_collect[0].day.day_id, TARGET_DAY)
        self.assertEqual(len(seen_collect[0].targets), 1)
        target = seen_collect[0].targets[0]
        self.assertEqual(target.day_id, TARGET_DAY)
        self.assertEqual(target.role, "dining")
        self.assertEqual(target.issue_ids, (seen_collect[0].issues[0].issue_id,))
        self.assertEqual(target.slot_ids, (missing_slot.slot_id,))
        self.assertEqual(target.requirement_ids, ())
        self.assertEqual(len(seen_revision), 1)
        self.assertEqual(seen_revision[0].day.day_id, TARGET_DAY)
        old_target = next(day for day in draft.days if day.day_id == TARGET_DAY)
        new_target = next(day for day in result.draft.days if day.day_id == TARGET_DAY)
        old_real_ids = {item.activity_id for item in old_target.activities if item.kind != "free_time"}
        new_real_ids = {item.activity_id for item in new_target.activities if item.kind != "free_time"}
        self.assertTrue(old_real_ids.issubset(new_real_ids))
        self.assertEqual(len([item for item in new_target.activities if item.slot_id == missing_slot.slot_id]), 1)
        self.assertFalse(any(item.kind == "free_time" for item in new_target.activities))
        for old_day in draft.days:
            if old_day.day_id == TARGET_DAY:
                continue
            self.assertEqual(
                old_day.model_dump(mode="json"),
                next(day for day in result.draft.days if day.day_id == old_day.day_id).model_dump(mode="json"),
            )
        self.assertEqual(result.repair_states[1].completed_rounds, 1)
        self.assertEqual(result.repair_states[1].stop_reason, "complete")

    async def test_route_timeout_keeps_revised_meal_and_continues_to_next_date(self):
        planning_input, facts, candidates, draft, timing, report, first_missing = setup_missing_dinner()
        second_day = planning_input.days[2]
        second_missing = next(slot for slot in second_day.slots if slot.period == "dinner")
        second_draft_day = next(day for day in draft.days if day.day_id == second_day.day_id)
        second_activities = tuple(
            activity for activity in second_draft_day.activities
            if activity.slot_id != second_missing.slot_id
        ) + (DraftActivity(
            activity_id="second-free-dinner",
            slot_id=second_missing.slot_id,
            kind="free_time",
            duration_s=1800,
            duration_source="model",
        ),)
        draft = draft.model_copy(update={
            "days": tuple(
                second_draft_day.model_copy(update={"activities": second_activities})
                if day.day_id == second_day.day_id else day
                for day in draft.days
            ),
        })
        timing = calculate_plan_timing(planning_input, draft, facts)
        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)

        source = FactSource(
            tool_call_id="two-date-meal-source",
            tool_name="offline_fixture",
            api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-09T10:00:00+08:00"),
            effective_params={"query": "补查餐馆"},
        )
        place = PlaceFact(
            fact_id="two-date-meal-fact",
            poi_id="two-date-meal-poi",
            name="补查餐馆",
            city="上海市",
            type_name="中餐厅",
            typecode="050000",
            location=MapCoordinate(lng=121.48, lat=31.23),
            source_refs=(source.tool_call_id,),
        )
        candidate = CandidateRef(
            candidate_id="two-date-meal-candidate",
            category="meal",
            poi_id=place.poi_id,
            place_fact_id=place.fact_id,
            slot_ids=(first_missing.slot_id, second_missing.slot_id),
        )
        recalc_days = []
        fallback_days = []

        async def collect(request):
            return DayRepairCollectionResult(
                stop_reason="target_met",
                fact_store=FactStore(
                    sources=(*request.fact_store.sources, source),
                    places=(*request.fact_store.places, place),
                ),
                candidates=(*request.candidates, candidate),
                area_hints=request.area_hints,
                task_results=request.task_results,
            )

        async def revise(request):
            target_constraint = next(
                day for day in planning_input.days if day.day_id == request.day.day_id
            )
            target_slot = next(
                slot.slot_id for slot in target_constraint.slots if slot.period == "dinner"
            )
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(
                    request.day,
                    added_candidate=candidate,
                    added_slot=target_slot,
                ),
                preserve_activity_ids=request.preserve_activity_ids,
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def recalculate(request):
            recalc_days.append(request.day_id)
            if request.day_id == planning_input.days[1].day_id:
                fallback_days.append(request.day_id)
                recalculated = PlanningV4Pipeline._failed_day_route_recalculation(
                    request, None, error_category="timeout",
                )
                self.assertEqual(
                    tuple(
                        day for day in recalculated.draft.days
                        if day.day_id != request.day_id
                    ),
                    tuple(
                        day for day in request.draft.days
                        if day.day_id != request.day_id
                    ),
                )
                return recalculated
            next_timing = calculate_plan_timing(
                request.planning_input, request.draft, request.facts,
            )
            next_report = validate_daily_plan(
                request.planning_input, request.draft, request.facts,
                request.candidates, next_timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft, facts=request.facts,
                timing=next_timing, report=next_report,
            )

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=recalculate,
            id_factory=lambda: f"route-timeout-{len(recalc_days) + 1}",
        ).run_async(
            planning_input, draft, facts, candidates, (), (), timing, report,
        )

        self.assertEqual(recalc_days, [planning_input.days[1].day_id, planning_input.days[2].day_id])
        self.assertEqual(fallback_days, [planning_input.days[1].day_id])
        self.assertEqual(result.status, "complete")
        self.assertTrue(any(
            activity.candidate_ref == candidate.candidate_id
            for activity in result.draft.days[1].activities
        ))
        self.assertTrue(any(
            activity.candidate_ref == candidate.candidate_id
            for activity in result.draft.days[2].activities
        ))
        failed_day_legs = [
            leg for leg in result.draft.legs if leg.day_id == planning_input.days[1].day_id
        ]
        self.assertTrue(failed_day_legs)
        self.assertTrue(any(mode.status == "error" for leg in failed_day_legs for mode in leg.mode_results))
        self.assertTrue(all(
            mode.status == "error" and mode.error_category == "timeout"
            for leg in failed_day_legs for mode in leg.mode_results
            if mode.status == "error"
        ))

    async def test_no_progress_collection_still_attempts_revision_three_times(self):
        planning_input, facts, candidates, draft, timing, report, missing_slot = setup_missing_dinner()
        collected = 0
        revised = 0

        async def collect(request):
            nonlocal collected
            collected += 1
            return DayRepairCollectionResult(
                stop_reason="no_progress",
                fact_store=request.fact_store,
                candidates=request.candidates,
                area_hints=request.area_hints,
                task_results=request.task_results,
            )

        async def revise(request):
            nonlocal revised
            revised += 1
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(request.day),
                preserve_activity_ids=request.preserve_activity_ids,
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def recalculate(request):
            next_timing = calculate_plan_timing(request.planning_input, request.draft, request.facts)
            next_report = validate_daily_plan(
                request.planning_input, request.draft, request.facts,
                request.candidates, next_timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft, facts=request.facts,
                timing=next_timing, report=next_report,
            )

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=recalculate,
            id_factory=lambda: f"no-progress-{revised + 1}",
        ).run_async(planning_input, draft, facts, candidates, (), (), timing, report)

        self.assertEqual(collected, 3)
        self.assertEqual(revised, 3)
        self.assertEqual([attempt.outcome for attempt in result.attempts], ["no_progress"] * 3)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.stop_reason, "round_limit")
        target_state = next(state for state in result.repair_states if state.day_id == TARGET_DAY)
        self.assertEqual(target_state.completed_rounds, 3)
        self.assertEqual(target_state.stop_reason, "round_limit")
        self.assertEqual(result.draft.revision, draft.revision)

    async def test_invalid_revision_counts_attempt_but_never_changes_draft(self):
        planning_input, facts, candidates, draft, timing, report, _ = setup_missing_dinner()
        revised = 0

        async def collect(request):
            return DayRepairCollectionResult.empty(
                request.fact_store, request.candidates,
                request.area_hints, request.task_results,
            )

        async def revise(request):
            nonlocal revised
            revised += 1
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(request.day),
                preserve_activity_ids=(),
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def unexpected_recalculation(_):
            raise AssertionError("invalid revisions must not reach recalculation")

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=unexpected_recalculation,
            id_factory=lambda: f"invalid-{revised + 1}",
        ).run_async(planning_input, draft, facts, candidates, (), (), timing, report)

        self.assertEqual(revised, 3)
        self.assertEqual([attempt.outcome for attempt in result.attempts], ["invalid_revision"] * 3)
        self.assertEqual(result.draft.model_dump(mode="json"), draft.model_dump(mode="json"))
        target_state = next(state for state in result.repair_states if state.day_id == TARGET_DAY)
        self.assertEqual(target_state.completed_rounds, 3)
        self.assertEqual(target_state.status, "stopped")
        self.assertEqual(target_state.stop_reason, "round_limit")

    async def test_deadline_partial_verified_facts_still_reach_day_reviser(self):
        planning_input, facts, candidates, draft, timing, report, missing_slot = setup_missing_dinner()
        revise_stops: list[str] = []

        async def collect(request):
            next_facts, next_candidates = add_dinner_candidate(
                request.fact_store, request.candidates, missing_slot.slot_id,
            )
            return DayRepairCollectionResult(
                stop_reason="deadline",
                fact_store=next_facts,
                candidates=next_candidates,
                area_hints=request.area_hints,
                task_results=request.task_results,
            )

        async def revise(request):
            revise_stops.append(request.collection_stop_reason)
            new_candidate = next(item for item in request.candidates if item.candidate_id == "candidate-dinner-repair")
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(
                    request.day, added_candidate=new_candidate, added_slot=missing_slot.slot_id,
                ),
                preserve_activity_ids=request.preserve_activity_ids,
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def recalculate(request):
            next_timing = calculate_plan_timing(request.planning_input, request.draft, request.facts)
            next_report = validate_daily_plan(
                request.planning_input, request.draft, request.facts,
                request.candidates, next_timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft, facts=request.facts,
                timing=next_timing, report=next_report,
            )

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=recalculate,
            id_factory=lambda: "deadline-partial-round",
        ).run_async(planning_input, draft, facts, candidates, (), (), timing, report)

        self.assertEqual(revise_stops, ["deadline"])
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.attempts[0].collection_stop_reason, "deadline")
        self.assertEqual(result.draft.revision, draft.revision + 1)

    async def test_single_day_model_failures_stop_at_three_and_other_days_continue(self):
        planning_input, facts, candidates, draft, timing, report, first_missing_slot = setup_missing_dinner()
        last_day_id = planning_input.days[-1].day_id
        last_day_constraint = planning_input.days[-1]
        second_missing_slot = next(slot for slot in last_day_constraint.slots if slot.period == "dinner")
        last_day = next(day for day in draft.days if day.day_id == last_day_id)
        last_day = last_day.model_copy(update={
            "activities": tuple(item for item in last_day.activities if item.slot_id != second_missing_slot.slot_id),
        })
        draft = draft.model_copy(update={
            "days": tuple(last_day if item.day_id == last_day_id else item for item in draft.days),
        })
        timing = calculate_plan_timing(planning_input, draft, facts)
        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)
        failed_day_calls = 0
        successful_day_calls = 0

        async def collect(request):
            return DayRepairCollectionResult(
                stop_reason="no_progress",
                fact_store=request.fact_store,
                candidates=request.candidates,
                area_hints=request.area_hints,
                task_results=request.task_results,
            )

        async def revise(request):
            nonlocal failed_day_calls, successful_day_calls
            if request.day.day_id == TARGET_DAY:
                failed_day_calls += 1
                raise DayRepairError("MODEL_TIMEOUT", deadline=True)
            successful_day_calls += 1
            meal_candidate = next(item for item in request.candidates if item.category == "meal")
            return DayRevision(
                day_id=request.day.day_id,
                base_revision=request.draft.revision,
                revision=request.draft.revision + 1,
                based_on_report_revision=request.report.based_on_revision,
                replacement_day=proposal_day_from_current(
                    request.day,
                    added_candidate=meal_candidate,
                    added_slot=second_missing_slot.slot_id,
                ),
                preserve_activity_ids=request.preserve_activity_ids,
                source_issue_ids=tuple(issue.issue_id for issue in request.issues),
            )

        async def recalculate(request):
            next_timing = calculate_plan_timing(request.planning_input, request.draft, request.facts)
            next_report = validate_daily_plan(
                request.planning_input, request.draft, request.facts,
                request.candidates, next_timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft, facts=request.facts,
                timing=next_timing, report=next_report,
            )

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=recalculate,
            id_factory=lambda: f"multi-day-{failed_day_calls}-{successful_day_calls}",
        ).run_async(planning_input, draft, facts, candidates, (), (), timing, report)

        self.assertEqual(failed_day_calls, 3)
        self.assertEqual(successful_day_calls, 1)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.draft.revision, draft.revision + 1)
        self.assertEqual(
            sum(attempt.outcome == "technical_error" for attempt in result.attempts), 3,
        )
        self.assertTrue(any(attempt.day_id == last_day_id and attempt.outcome == "applied" for attempt in result.attempts))
        first_day = next(day for day in result.draft.days if day.day_id == TARGET_DAY)
        self.assertEqual(first_day.model_dump(mode="json"), next(day for day in draft.days if day.day_id == TARGET_DAY).model_dump(mode="json"))
        self.assertEqual(next(state for state in result.repair_states if state.day_id == TARGET_DAY).stop_reason, "round_limit")

    async def test_global_deadline_stops_before_starting_any_adapter(self):
        planning_input, facts, candidates, draft, timing, report, _ = setup_missing_dinner()
        calls = 0

        async def unexpected(_):
            nonlocal calls
            calls += 1
            raise AssertionError("expired global deadline must stop all callbacks")

        result = await DayRepairService(
            collect=unexpected, revise=unexpected, recalculate=unexpected,
        ).run_async(
            planning_input, draft, facts, candidates, (), (), timing, report,
            global_deadline_monotonic=time.monotonic() - 1,
        )

        self.assertEqual(calls, 0)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.stop_reason, "deadline")
        self.assertEqual(result.draft.revision, draft.revision)
        self.assertEqual(result.attempts, ())

    async def test_global_deadline_during_active_collection_counts_that_round(self):
        planning_input, facts, candidates, draft, timing, report, _ = setup_missing_dinner()
        deadline = time.monotonic() + 60
        revised = 0

        async def collect(_):
            nonlocal deadline
            deadline = time.monotonic() - 1
            raise DayRepairError("GLOBAL_TECHNICAL_DEADLINE", global_deadline=True)

        async def revise(_):
            nonlocal revised
            revised += 1
            raise AssertionError("global deadline must stop before day revision")

        result = await DayRepairService(
            collect=collect, revise=revise, recalculate=revise,
            id_factory=lambda: "global-deadline-round",
        ).run_async(
            planning_input, draft, facts, candidates, (), (), timing, report,
            global_deadline_monotonic=deadline,
        )

        state = next(item for item in result.repair_states if item.day_id == TARGET_DAY)
        self.assertEqual(revised, 0)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.stop_reason, "deadline")
        self.assertEqual(result.draft.revision, draft.revision)
        self.assertEqual(state.completed_rounds, 1)
        self.assertEqual(state.stop_reason, "deadline")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].outcome, "deadline")

    async def test_unknown_information_never_calls_repair_adapters(self):
        planning_input, facts, candidates, draft, timing, report, _ = setup_missing_dinner(include_placeholder=False)
        # Fill the missing dinner so only non-triggering unknown information remains.
        all_meal_slot_ids = tuple(
            slot.slot_id for day in planning_input.days for slot in day.slots
            if slot.period in {"breakfast", "lunch", "dinner"}
        )
        candidates = tuple(
            item.model_copy(update={"slot_ids": all_meal_slot_ids}) if item.category == "meal" else item
            for item in candidates
        )
        target = next(day for day in draft.days if day.day_id == TARGET_DAY)
        dinner = next(slot for slot in planning_input.days[1].slots if slot.period == "dinner")
        meal = next(item for item in candidates if item.category == "meal")
        target = target.model_copy(update={
            "activities": (*target.activities, DraftActivity(
                activity_id="late-dinner", candidate_ref=meal.candidate_id,
                slot_id=dinner.slot_id, kind="meal", poi_id=meal.poi_id,
                place_fact_id=meal.place_fact_id, duration_s=3600,
                duration_source="model",
            )),
        })
        draft = draft.model_copy(update={"days": tuple(target if item.day_id == TARGET_DAY else item for item in draft.days)})
        timing = calculate_plan_timing(planning_input, draft, facts)
        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)
        calls = 0

        async def unexpected(_):
            nonlocal calls
            calls += 1
            raise AssertionError("unknown information must not trigger a repair")

        result = await DayRepairService(
            collect=unexpected, revise=unexpected, recalculate=unexpected,
        ).run_async(planning_input, draft, facts, candidates, (), (), timing, report)
        self.assertEqual(calls, 0)
        self.assertEqual(result.status, "complete")


def day_revision_model_case():
    planning_input, facts, candidates, draft, _, report, missing_slot = setup_missing_dinner()
    target = next(day for day in draft.days if day.day_id == TARGET_DAY)
    new_source = FactSource(
        tool_call_id="revision-model-source", tool_name="offline_fixture", api_version="v4",
        queried_at=datetime.fromisoformat("2026-10-09T11:00:00+08:00"), effective_params={},
    )
    new_place = PlaceFact(
        fact_id="revision-model-fact", poi_id="revision-model-poi", name="新餐馆",
        city="上海市", type_name="中餐厅", typecode="050000",
        location=MapCoordinate(lng=121.48, lat=31.23), source_refs=(new_source.tool_call_id,),
    )
    dinner_candidate = CandidateRef(
        candidate_id="revision-model-candidate", category="meal",
        poi_id=new_place.poi_id, place_fact_id=new_place.fact_id,
        slot_ids=(missing_slot.slot_id,),
    )
    facts = FactStore(sources=(*facts.sources, new_source), places=(*facts.places, new_place))
    candidates = (*candidates, dinner_candidate)
    issue = next(issue for issue in report.issues if issue.code == "missing_meal" and issue.day_id == TARGET_DAY)
    preserve = tuple(item.activity_id for item in target.activities if item.kind != "free_time")
    activities = [ProposalActivity(
        proposal_ref=item.activity_id,
        existing_activity_id=item.activity_id,
        candidate_ref=item.candidate_ref,
        slot_id=item.slot_id,
        kind=item.kind,
        duration_s=item.duration_s,
        requirement_ids=item.requirement_ids,
        recommendation_reason=item.recommendation_reason,
    ).model_dump(mode="json") for item in target.activities if item.kind != "free_time"]
    activities.append(ProposalActivity(
        proposal_ref="new-dinner", candidate_ref=dinner_candidate.candidate_id,
        slot_id=missing_slot.slot_id, kind="meal", duration_s=3600,
    ).model_dump(mode="json"))
    response = json.dumps({"replacement_day": {
        "day_id": target.day_id,
        "day_date": target.day_date.isoformat(),
        "activities": activities,
        "unfilled_slot_ids": [],
    }}, ensure_ascii=False)
    assignment = IssueDayAssignment(
        issue_id=issue.issue_id, assigned_day_id=TARGET_DAY, reason="issue_day",
    )
    return {
        "planning_input": planning_input,
        "draft": draft,
        "report": report,
        "issue": issue,
        "assignment": assignment,
        "candidates": candidates,
        "facts": facts,
        "preserve": preserve,
        "response": response,
        "missing_slot": missing_slot,
    }


def invoke_day_revision(fake, case):
    return DailyPlanGenerator(llm=fake).revise_day(
        case["planning_input"], case["draft"], case["report"], TARGET_DAY,
        (case["issue"],), (case["assignment"],),
        case["candidates"], case["facts"], (), (),
        preserve_activity_ids=case["preserve"],
        modifiable_activity_ids=(),
        removable_activity_ids=("free-dinner-placeholder",),
    )


def build_bound_day_revision(case, *, preserve_activity_ids, modifiable_activity_ids=(), response=None):
    class FakeLLM:
        def invoke_with_metadata(self, _messages, **_kwargs):
            return LLMCompletion(response or case["response"], None, "stop")

    return DailyPlanGenerator(llm=FakeLLM()).revise_day(
        case["planning_input"], case["draft"], case["report"], TARGET_DAY,
        (case["issue"],), (case["assignment"],),
        case["candidates"], case["facts"], (), (),
        preserve_activity_ids=preserve_activity_ids,
        modifiable_activity_ids=modifiable_activity_ids,
        removable_activity_ids=("free-dinner-placeholder",),
    ).revision


def narrow_user_activity_search_scope(case):
    target = next(day for day in case["draft"].days if day.day_id == TARGET_DAY)
    user_activity = next(activity for activity in target.activities if activity.kind != "free_time")
    original_candidate = next(
        candidate for candidate in case["candidates"]
        if candidate.candidate_id == user_activity.candidate_ref
    )
    other_slot = next(
        slot for day in case["planning_input"].days for slot in day.slots
        if slot.eligibility != "skipped" and slot.slot_id != user_activity.slot_id
    )
    narrowed_candidate = original_candidate.model_copy(update={
        "candidate_id": "user-activity-search-scope",
        "slot_ids": (other_slot.slot_id,),
    })
    user_day = target.model_copy(update={
        "activities": tuple(
            activity.model_copy(update={
                "activity_source": "user",
                "candidate_ref": narrowed_candidate.candidate_id,
            }) if activity.activity_id == user_activity.activity_id else activity
            for activity in target.activities
        ),
    })
    case["draft"] = case["draft"].model_copy(update={
        "days": tuple(user_day if day.day_id == TARGET_DAY else day for day in case["draft"].days),
    })
    case["candidates"] = (*case["candidates"], narrowed_candidate)
    response_payload = json.loads(case["response"])
    proposal = next(
        activity for activity in response_payload["replacement_day"]["activities"]
        if activity.get("existing_activity_id") == user_activity.activity_id
    )
    proposal["candidate_ref"] = narrowed_candidate.candidate_id
    case["response"] = json.dumps(response_payload, ensure_ascii=False)
    return case, user_activity, narrowed_candidate


def day_revision_from_response(case, *, preserve_activity_ids, response):
    payload = json.loads(response)
    return DayRevision(
        day_id=TARGET_DAY,
        base_revision=case["draft"].revision,
        revision=case["draft"].revision + 1,
        based_on_report_revision=case["report"].based_on_revision,
        replacement_day=ProposalDay.model_validate(payload["replacement_day"]),
        preserve_activity_ids=preserve_activity_ids,
        source_issue_ids=(case["issue"].issue_id,),
    )


class DayScopedRevisionModelTests(unittest.TestCase):
    def test_real_adapter_execution_is_unwrapped_before_day_repair_service(self):
        case = day_revision_model_case()

        class FixedLLM:
            def invoke_with_metadata(self, _messages, **_kwargs):
                return LLMCompletion(case["response"], None, "stop")

        adapter = DailyPlanGeneratorAdapter(DailyPlanGenerator(llm=FixedLLM()))

        async def collect(_request):
            return DayRepairCollectionResult.empty(
                case["facts"], case["candidates"], (), (),
            )

        async def revise(request):
            execution = await adapter.revise_day(
                request.planning_input,
                request.draft,
                request.report,
                request.day.day_id,
                request.issues,
                request.assignments,
                request.candidates,
                request.fact_store,
                request.area_hints,
                request.task_results,
                preserve_activity_ids=request.preserve_activity_ids,
                modifiable_activity_ids=request.modifiable_activity_ids,
                removable_activity_ids=request.removable_activity_ids,
                source_run_id=request.draft.source_run_id,
                round_id=request.round_id,
                round_number=request.round_number,
                timeout_seconds=30,
            )
            return PlanningV4Pipeline._normalize_day_revision_execution(execution)

        async def recalculate(request):
            timing = calculate_plan_timing(
                request.planning_input, request.draft, request.facts,
            )
            report = validate_daily_plan(
                request.planning_input,
                request.draft,
                request.facts,
                request.candidates,
                timing,
                repair_states=request.repair_states,
            )
            return DayRepairRecalculation(
                draft=request.draft,
                facts=request.facts,
                timing=timing,
                report=report,
            )

        result = asyncio.run(DayRepairService(
            collect=collect,
            revise=revise,
            recalculate=recalculate,
        ).run_async(
            case["planning_input"],
            case["draft"],
            case["facts"],
            case["candidates"],
            (),
            (),
            calculate_plan_timing(case["planning_input"], case["draft"], case["facts"]),
            case["report"],
        ))

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.draft.revision, case["draft"].revision + 1)
        target = next(day for day in result.draft.days if day.day_id == TARGET_DAY)
        self.assertTrue(any(activity.slot_id == case["missing_slot"].slot_id for activity in target.activities))
        self.assertEqual(result.attempts[0].outcome, "applied")

    def test_day_revision_result_rejects_unrecognized_or_malformed_adapter_values(self):
        from app.agents.daily_plan_generator import DailyDayRevisionExecution

        with self.assertRaisesRegex(DayRepairError, "DAY_REVISION_RESULT_INVALID"):
            PlanningV4Pipeline._normalize_day_revision_execution(object())
        malformed_execution = DailyDayRevisionExecution(
            revision=object(), usage=None, model_calls=1, format_repairs=0, elapsed_seconds=0,
        )
        with self.assertRaisesRegex(DayRepairError, "DAY_REVISION_RESULT_INVALID"):
            PlanningV4Pipeline._normalize_day_revision_execution(malformed_execution)

    def test_revise_day_sends_one_date_and_returns_a_bound_day_revision(self):
        planning_input, facts, candidates, draft, timing, report, missing_slot = setup_missing_dinner()
        target = next(day for day in draft.days if day.day_id == TARGET_DAY)
        source, places = facts.sources[0], facts.places
        new_source = FactSource(
            tool_call_id="revision-model-source", tool_name="offline_fixture", api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-09T11:00:00+08:00"), effective_params={},
        )
        new_place = PlaceFact(
            fact_id="revision-model-fact", poi_id="revision-model-poi", name="新餐馆",
            city="上海市", type_name="中餐厅", typecode="050000",
            location=MapCoordinate(lng=121.48, lat=31.23), source_refs=(new_source.tool_call_id,),
        )
        dinner_candidate = CandidateRef(
            candidate_id="revision-model-candidate", category="meal",
            poi_id=new_place.poi_id, place_fact_id=new_place.fact_id,
            slot_ids=(missing_slot.slot_id,),
        )
        facts = FactStore(sources=(*facts.sources, new_source), places=(*places, new_place))
        candidates = (*candidates, dinner_candidate)
        issue = next(issue for issue in report.issues if issue.code == "missing_meal" and issue.day_id == TARGET_DAY)
        preserve = tuple(item.activity_id for item in target.activities if item.kind != "free_time")
        output_activities = [ProposalActivity(
            proposal_ref=item.activity_id,
            existing_activity_id=item.activity_id,
            candidate_ref=item.candidate_ref,
            slot_id=item.slot_id,
            kind=item.kind,
            duration_s=item.duration_s,
            requirement_ids=item.requirement_ids,
            recommendation_reason=item.recommendation_reason,
        ).model_dump(mode="json") for item in target.activities if item.kind != "free_time"]
        output_activities.append(ProposalActivity(
            proposal_ref="new-dinner", candidate_ref=dinner_candidate.candidate_id,
            slot_id=missing_slot.slot_id, kind="meal", duration_s=3600,
        ).model_dump(mode="json"))
        response = json.dumps({"replacement_day": {
            "day_id": target.day_id,
            "day_date": target.day_date.isoformat(),
            "activities": output_activities,
            "unfilled_slot_ids": [],
        }}, ensure_ascii=False)

        class FakeLLM:
            messages = None
            kwargs = None

            def invoke_with_metadata(self, messages, **kwargs):
                self.messages = messages
                self.kwargs = kwargs
                return LLMCompletion(response, None, "stop")

        llm = FakeLLM()
        execution = DailyPlanGenerator(llm=llm).revise_day(
            planning_input, draft, report, TARGET_DAY, (issue,),
            (IssueDayAssignment(issue_id=issue.issue_id, assigned_day_id=TARGET_DAY, reason="issue_day"),),
            candidates, facts, (), (),
            preserve_activity_ids=preserve,
            modifiable_activity_ids=(),
            removable_activity_ids=("free-dinner-placeholder",),
            timeout_seconds=17.0,
        )
        payload = json.loads(llm.messages[-1]["content"])
        self.assertEqual(len(payload["planning_input"]["days"]), 1)
        self.assertEqual(payload["planning_input"]["days"][0]["day_id"], TARGET_DAY)
        self.assertEqual(payload["target_date_only"]["day_id"], TARGET_DAY)
        self.assertEqual(execution.revision.day_id, TARGET_DAY)
        self.assertEqual(execution.revision.based_on_report_revision, report.based_on_revision)
        self.assertEqual(execution.revision.preserve_activity_ids, preserve)
        self.assertAlmostEqual(llm.kwargs["request_timeout"], 17.0, places=1)

    def test_day_revision_preserves_user_source_only_for_unchanged_existing_activities(self):
        case, user_activity, user_candidate = narrow_user_activity_search_scope(
            day_revision_model_case(),
        )
        revision = build_bound_day_revision(
            case, preserve_activity_ids=case["preserve"],
        )
        revised, changed = apply_day_revision(
            revision,
            case["draft"], case["report"], case["planning_input"],
            case["candidates"], case["facts"],
            expected_day_id=TARGET_DAY,
            expected_source_issue_ids=(case["issue"].issue_id,),
            preserve_activity_ids=case["preserve"],
            removable_activity_ids=("free-dinner-placeholder",),
        )
        revised_day = next(day for day in revised.days if day.day_id == TARGET_DAY)
        revised_user_activity = next(
            activity for activity in revised_day.activities
            if activity.activity_id == user_activity.activity_id
        )
        generated_dinner = next(
            activity for activity in revised_day.activities
            if activity.slot_id == case["missing_slot"].slot_id
        )
        self.assertTrue(changed)
        self.assertEqual(revised_user_activity.activity_source, "user")
        self.assertEqual(revised_user_activity.candidate_ref, user_candidate.candidate_id)
        self.assertEqual(generated_dinner.activity_source, "model")

        with self.assertRaises(ValidationError):
            ProposalActivity(
                proposal_ref="model-forged-user", candidate_ref="revision-model-candidate",
                slot_id=case["missing_slot"].slot_id, kind="meal", activity_source="user",
            )

    def test_user_search_scope_exception_requires_exact_preserved_activity_binding(self):
        base_case, user_activity, user_candidate = narrow_user_activity_search_scope(
            day_revision_model_case(),
        )
        payload = json.loads(base_case["response"])
        proposal = next(
            activity for activity in payload["replacement_day"]["activities"]
            if activity.get("existing_activity_id") == user_activity.activity_id
        )
        other_sightseeing_slot = next(
            slot for slot in base_case["planning_input"].days
            if slot.day_id == TARGET_DAY
            for slot in slot.slots
            if slot.eligibility != "skipped"
            and slot.period in {"morning", "afternoon", "evening"}
            and slot.slot_id != user_activity.slot_id
        )
        alternate_candidate = user_candidate.model_copy(update={
            "candidate_id": "alternate-user-activity-candidate",
            "slot_ids": (user_activity.slot_id,),
        })
        base_case["candidates"] = (*base_case["candidates"], alternate_candidate)

        changed_kind = "meal" if user_activity.kind == "sightseeing" else "sightseeing"
        invalid_variants = (
            ("changed slot", {"slot_id": other_sightseeing_slot.slot_id}, base_case["preserve"]),
            ("changed candidate", {"candidate_ref": alternate_candidate.candidate_id}, base_case["preserve"]),
            ("changed kind", {"kind": changed_kind}, base_case["preserve"]),
            ("not preserved", {}, tuple(
                activity_id for activity_id in base_case["preserve"]
                if activity_id != user_activity.activity_id
            )),
        )
        for label, changes, preserve in invalid_variants:
            with self.subTest(label=label):
                invalid_payload = json.loads(json.dumps(payload))
                invalid_proposal = next(
                    activity for activity in invalid_payload["replacement_day"]["activities"]
                    if activity.get("existing_activity_id") == user_activity.activity_id
                )
                invalid_proposal.update(changes)
                revision = day_revision_from_response(
                    base_case,
                    preserve_activity_ids=preserve,
                    response=json.dumps(invalid_payload, ensure_ascii=False),
                )
                with self.assertRaises(ContractValidationError) as raised:
                    validate_day_revision(
                        revision,
                        base_case["draft"], base_case["report"], base_case["planning_input"],
                        base_case["candidates"], base_case["facts"],
                    )
                codes = {issue.code for issue in raised.exception.issues}
                self.assertTrue(
                    {"invalid_revision_candidate_slot", "preserved_activity_identity_changed"} & codes,
                    codes,
                )

    def test_model_change_to_user_activity_reclassifies_it_as_model_sourced(self):
        case = day_revision_model_case()
        target = next(day for day in case["draft"].days if day.day_id == TARGET_DAY)
        user_activity = next(activity for activity in target.activities if activity.kind != "free_time")
        user_day = target.model_copy(update={
            "activities": tuple(
                activity.model_copy(update={"activity_source": "user"})
                if activity.activity_id == user_activity.activity_id else activity
                for activity in target.activities
            ),
        })
        case["draft"] = case["draft"].model_copy(update={
            "days": tuple(user_day if day.day_id == TARGET_DAY else day for day in case["draft"].days),
        })
        preserve = tuple(
            activity_id for activity_id in case["preserve"]
            if activity_id != user_activity.activity_id
        )
        response_payload = json.loads(case["response"])
        existing = next(
            activity for activity in response_payload["replacement_day"]["activities"]
            if activity.get("existing_activity_id") == user_activity.activity_id
        )
        existing["duration_s"] = user_activity.duration_s + 600
        revision = build_bound_day_revision(
            case,
            preserve_activity_ids=preserve,
            modifiable_activity_ids=(user_activity.activity_id,),
            response=json.dumps(response_payload, ensure_ascii=False),
        )
        revised, _ = apply_day_revision(
            revision,
            case["draft"], case["report"], case["planning_input"],
            case["candidates"], case["facts"],
            expected_day_id=TARGET_DAY,
            expected_source_issue_ids=(case["issue"].issue_id,),
            preserve_activity_ids=preserve,
            allowed_change_activity_ids=(user_activity.activity_id,),
            removable_activity_ids=("free-dinner-placeholder",),
        )
        revised_day = next(day for day in revised.days if day.day_id == TARGET_DAY)
        modified = next(
            activity for activity in revised_day.activities
            if activity.activity_id == user_activity.activity_id
        )
        self.assertEqual(modified.duration_s, user_activity.duration_s + 600)
        self.assertEqual(modified.activity_source, "model")

    def test_revise_day_invalid_output_retains_completed_call_usage(self):
        case = day_revision_model_case()
        usage = {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            def invoke_with_metadata(self, _messages, **_kwargs):
                self.calls += 1
                return LLMCompletion("not-json", usage, "stop")

        llm = FakeLLM()
        with self.assertRaises(PlanningError) as raised:
            invoke_day_revision(llm, case)

        self.assertEqual(raised.exception.code, "PLAN_REVISION_OUTPUT_INVALID")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(raised.exception.model_calls, 2)
        self.assertEqual(raised.exception.format_repairs, 1)
        self.assertEqual(raised.exception.usage, {
            "input_tokens": 240,
            "output_tokens": 120,
            "total_tokens": 360,
        })

    def test_revise_day_reference_failure_retains_successful_call_usage(self):
        case = day_revision_model_case()
        payload = json.loads(case["response"])
        new_activity = next(
            activity for activity in payload["replacement_day"]["activities"]
            if activity["proposal_ref"] == "new-dinner"
        )
        new_activity["candidate_ref"] = "missing-candidate"
        invalid_references = json.dumps(payload, ensure_ascii=False)
        usage = {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}

        class FakeLLM:
            def __init__(self):
                self.calls = 0

            def invoke_with_metadata(self, _messages, **_kwargs):
                self.calls += 1
                return LLMCompletion(invalid_references, usage, "stop")

        llm = FakeLLM()
        with self.assertRaises(PlanningError) as raised:
            invoke_day_revision(llm, case)

        self.assertEqual(raised.exception.code, "PLAN_REVISION_REFERENCES_INVALID")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(raised.exception.model_calls, 1)
        self.assertEqual(raised.exception.format_repairs, 0)
        self.assertEqual(raised.exception.usage["total_tokens"], 180)

    def test_revise_day_second_invoke_failure_keeps_cumulative_unknown_usage(self):
        case = day_revision_model_case()
        first_usage = {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}

        class FakeLLM:
            def __init__(self):
                self.responses = [
                    LLMCompletion("not-json", first_usage, "stop"),
                    RuntimeError("provider unavailable"),
                ]
                self.calls = 0

            def invoke_with_metadata(self, _messages, **_kwargs):
                self.calls += 1
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        llm = FakeLLM()
        with self.assertRaises(PlanningError) as raised:
            invoke_day_revision(llm, case)

        self.assertEqual(raised.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(llm.calls, 2)
        self.assertEqual(raised.exception.model_calls, 2)
        self.assertEqual(raised.exception.format_repairs, 1)
        self.assertIsNone(raised.exception.usage)


class IssueAssignmentTests(unittest.TestCase):
    def test_plan_level_must_visit_uses_candidate_applicable_date(self):
        requirement_value = requirement("must-visit-waterfront", "must_visit", "外滩")
        planning_input = make_input(requirements=(requirement_value,))
        facts, candidates = make_facts_and_candidates()
        day2 = planning_input.days[1]
        afternoon = next(slot for slot in day2.slots if slot.period == "afternoon")
        sightseeing = next(item for item in candidates if item.category == "sightseeing")
        candidates = (sightseeing.model_copy(update={
            "requirement_ids": (requirement_value.requirement_id,),
            "slot_ids": (afternoon.slot_id,),
        }), next(item for item in candidates if item.category == "meal"))
        source = facts.sources[0]
        places = tuple(item.model_copy(update={"city": "上海市", "typecode": "110000" if item.poi_id == sightseeing.poi_id else "050000"}) for item in facts.places)
        facts = FactStore(sources=(source,), places=places)
        draft = make_filled_draft(planning_input, candidates)
        report = ValidationReport(
            plan_id=draft.plan_id,
            based_on_revision=draft.revision,
            issues=(ValidationIssue(
                issue_id="issue-must-visit",
                code="must_visit_unplanned",
                category="missing_arrangement",
                severity="error",
                scope="plan",
                target_id=requirement_value.requirement_id,
                message="必去地点尚未安排。",
                triggers_repair=True,
                based_on_revision=draft.revision,
            ),),
        )
        assignment = next(item for item in assign_repair_issues(planning_input, report, facts, candidates) if item.issue_id in {issue.issue_id for issue in report.issues if issue.code == "must_visit_unplanned"})
        self.assertEqual(assignment.assigned_day_id, day2.day_id)
        self.assertEqual(assignment.reason, "candidate_applicable_day")
        self.assertEqual(assignment.candidate_id, sightseeing.candidate_id)


if __name__ == "__main__":
    unittest.main()
