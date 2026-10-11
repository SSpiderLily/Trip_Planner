"""planning_v4 每日完整性与冲突报告的离线测试。"""
from datetime import date, datetime
import asyncio
import unittest

from app.models.planning_v4.facts import FactSource, FactStore, MapCoordinate, PlaceFact
from app.models.planning_v4.input import (
    DayConstraint,
    HardRequirement,
    PlanningInput,
    RequirementSource,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)
from app.models.planning_v4.plan import (
    DaySummary,
    DraftActivity,
    DraftDay,
    ItineraryPreview,
    PlanDraft,
    PreviewCalculation,
    RepairState,
)
from app.services.current_route_service import CurrentRouteService
from app.services.planning_v4_timing import (
    ActivityTiming,
    DayTimingResult,
    PlanTimingResult,
    ReturnTiming,
)
from app.models.planning_v4.tasks import CandidateRef
from app.services.planning_v4_timing import calculate_plan_timing
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan
from app.services.planning_v4_validator import ContractValidationError, validate_preview


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
PERIODS = tuple(window["period"] for window in WINDOWS)


def make_input(*, requirements=(), departure="2026-10-12T23:00:00+08:00"):
    request = TripRequest(
        request_id="validation-request",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
        departure_at=datetime.fromisoformat(departure),
        timezone="Asia/Shanghai",
        departure_place_query="虹桥站",
        remarks="外滩；不要去酒吧；安排本帮菜",
    )
    summary = RequirementSummary(
        requirements_id="validation-requirements",
        request_id=request.request_id,
        hard_requirements=tuple(requirements),
    )
    rules = RuleSet(
        rule_version="validation-rules-v1",
        windows=WINDOWS,
        meal_default_s=3600,
        sightseeing_default_s=3600,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    days = []
    for offset in range(3):
        current = date(2026, 10, 10 + offset)
        day_id = f"day-{current.isoformat()}"
        days.append(DayConstraint(
            day_id=day_id,
            day_date=current,
            available_start=datetime.fromisoformat(f"{current.isoformat()}T08:00:00+08:00"),
            slots=tuple(SlotConstraint(
                slot_id=f"slot-{current.isoformat()}-{period}",
                day_id=day_id,
                period=period,
                eligibility="required",
            ) for period in PERIODS),
        ))
    return PlanningInput(
        request=request,
        requirements=summary,
        rules=rules,
        days=tuple(days),
    )


def make_facts_and_candidates():
    source = FactSource(
        tool_call_id="fixture-source",
        tool_name="offline_fixture",
        api_version="v4",
        queried_at=datetime.fromisoformat("2026-10-09T08:00:00+08:00"),
        effective_params={},
    )
    places = (
        PlaceFact(
            fact_id="fact-sight",
            poi_id="poi-sight",
            name="外滩",
            location=MapCoordinate(lng=121.49, lat=31.24),
            source_refs=(source.tool_call_id,),
        ),
        PlaceFact(
            fact_id="fact-meal",
            poi_id="poi-meal",
            name="测试餐馆",
            location=MapCoordinate(lng=121.50, lat=31.24),
            source_refs=(source.tool_call_id,),
        ),
    )
    candidates = (
        CandidateRef(
            candidate_id="candidate-sight",
            category="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
        ),
        CandidateRef(
            candidate_id="candidate-meal",
            category="meal",
            poi_id="poi-meal",
            place_fact_id="fact-meal",
        ),
    )
    return FactStore(sources=(source,), places=places), candidates


def make_filled_draft(planning_input, candidates, *, unfilled_by_day=None):
    candidate_by_category = {candidate.category: candidate for candidate in candidates}
    days = []
    for constraint in planning_input.days:
        activities = []
        for slot in constraint.slots:
            kind = "meal" if slot.period in {"breakfast", "lunch", "dinner"} else "sightseeing"
            candidate = candidate_by_category[kind]
            activities.append(DraftActivity(
                activity_id=f"activity-{constraint.day_id}-{slot.period}",
                candidate_ref=candidate.candidate_id,
                slot_id=slot.slot_id,
                kind=kind,
                poi_id=candidate.poi_id,
                place_fact_id=candidate.place_fact_id,
                duration_s=3600,
                duration_source="model",
            ))
        days.append(DraftDay(
            day_id=constraint.day_id,
            day_date=constraint.day_date,
            activities=tuple(activities),
            unfilled_slot_ids=(unfilled_by_day or {}).get(constraint.day_id, ()),
        ))
    return PlanDraft(
        plan_id="validation-plan",
        request_id=planning_input.request.request_id,
        source_run_id="validation-run",
        revision=2,
        base_revision=1,
        rule_version=planning_input.rules.rule_version,
        days=tuple(days),
    )


def make_draft_with_activities(planning_input, activities_by_day):
    days = []
    for constraint in planning_input.days:
        activities = tuple(activities_by_day.get(constraint.day_id, ()))
        days.append(DraftDay(
            day_id=constraint.day_id,
            day_date=constraint.day_date,
            activities=activities,
        ))
    return PlanDraft(
        plan_id="validation-plan",
        request_id=planning_input.request.request_id,
        source_run_id="validation-run",
        revision=2,
        base_revision=1,
        rule_version=planning_input.rules.rule_version,
        days=tuple(days),
    )


def requirement(requirement_id, kind, quote):
    return HardRequirement(
        requirement_id=requirement_id,
        kind=kind,
        text=quote,
        source=RequirementSource(field="remarks", quote=quote),
    )


class PlanningV4ValidationTests(unittest.TestCase):
    def setUp(self):
        self.planning_input = make_input()
        self.facts, self.candidates = make_facts_and_candidates()

    def test_full_three_day_coverage_is_complete_even_when_cost_weather_and_timing_are_unknown(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        before = (self.planning_input.model_dump_json(), draft.model_dump_json(), self.facts.model_dump_json())

        report = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

        after = (self.planning_input.model_dump_json(), draft.model_dump_json(), self.facts.model_dump_json())
        self.assertEqual(before, after)
        self.assertEqual([day.completeness for day in report.days], ["complete"] * 3)
        self.assertEqual([day.feasibility for day in report.days], ["unverified"] * 3)
        self.assertTrue(any(issue.code == "cost_unavailable" for issue in report.issues))
        self.assertTrue(any(issue.code == "weather_unavailable" for issue in report.issues))
        self.assertTrue(all(not issue.triggers_repair for issue in report.issues if issue.category == "unknown_info"))
        self.assertEqual(report.repair_states, ())

    def test_missing_meal_and_sightseeing_are_derived_from_activities_not_unfilled_ids(self):
        constraint = self.planning_input.days[1]
        morning = next(slot for slot in constraint.slots if slot.period == "morning")
        lunch = next(slot for slot in constraint.slots if slot.period == "lunch")
        free_time = DraftActivity(
            activity_id="free-morning",
            slot_id=morning.slot_id,
            kind="free_time",
            duration_s=1800,
            duration_source="model",
        )
        draft = make_draft_with_activities(self.planning_input, {constraint.day_id: (free_time,)})

        report = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

        by_target = {(issue.day_id, issue.target_id): issue for issue in report.issues}
        self.assertEqual(by_target[(constraint.day_id, morning.slot_id)].code, "missing_sightseeing")
        self.assertEqual(by_target[(constraint.day_id, lunch.slot_id)].code, "missing_meal")
        self.assertTrue(by_target[(constraint.day_id, morning.slot_id)].triggers_repair)
        self.assertEqual(report.days[1].completeness, "incomplete")

    def test_conditional_missing_slot_is_unknown_and_does_not_trigger_repair(self):
        first_day = self.planning_input.days[0]
        slots = tuple(
            slot.model_copy(update={"eligibility": "conditional", "condition_reason": "到达后实际可用时刻未核实。"})
            if slot.period == "breakfast" else slot
            for slot in first_day.slots
        )
        planning_input = self.planning_input.model_copy(update={
            "days": (first_day.model_copy(update={"slots": slots}), *self.planning_input.days[1:]),
        })
        activities = [
            activity for activity in make_filled_draft(planning_input, self.candidates).days[0].activities
            if activity.slot_id != slots[0].slot_id
        ]
        draft = make_draft_with_activities(planning_input, {first_day.day_id: tuple(activities)})

        report = validate_daily_plan(planning_input, draft, self.facts, self.candidates)

        issue = next(item for item in report.issues if item.target_id == slots[0].slot_id)
        self.assertEqual(issue.category, "unknown_info")
        self.assertFalse(issue.triggers_repair)
        self.assertEqual(report.days[0].completeness, "conditional")

    def test_arrival_and_departure_conditional_slots_remain_non_repairable(self):
        first, middle, last = self.planning_input.days
        first_slots = tuple(
            slot.model_copy(update={"eligibility": "conditional", "condition_reason": "到达后的实际可用时刻未核实。"})
            if slot.period == "breakfast" else slot
            for slot in first.slots
        )
        last_slots = tuple(
            slot.model_copy(update={"eligibility": "conditional", "condition_reason": "返程地点或交通耗时未核实。"})
            if slot.period == "dinner" else slot
            for slot in last.slots
        )
        planning_input = self.planning_input.model_copy(update={
            "days": (
                first.model_copy(update={"slots": first_slots}),
                middle,
                last.model_copy(update={"slots": last_slots}),
            ),
        })
        complete = make_filled_draft(planning_input, self.candidates)
        activities_by_day = {
            day.day_id: tuple(
                activity for activity in day.activities
                if activity.slot_id not in {
                    f"slot-{first.day_date.isoformat()}-breakfast",
                    f"slot-{last.day_date.isoformat()}-dinner",
                }
            )
            for day in complete.days
        }
        draft = make_draft_with_activities(planning_input, activities_by_day)

        report = validate_daily_plan(planning_input, draft, self.facts, self.candidates)

        self.assertEqual(report.days[0].completeness, "conditional")
        self.assertEqual(report.days[-1].completeness, "conditional")
        unresolved = [issue for issue in report.issues if issue.code == "conditional_slot_unresolved"]
        self.assertEqual({issue.day_id for issue in unresolved}, {first.day_id, last.day_id})
        self.assertTrue(all(issue.category == "unknown_info" and not issue.triggers_repair for issue in unresolved))

    def test_stale_unfilled_metadata_does_not_hide_real_coverage(self):
        draft = make_filled_draft(
            self.planning_input,
            self.candidates,
            unfilled_by_day={
                day.day_id: tuple(slot.slot_id for slot in day.slots)
                for day in self.planning_input.days
            },
        )

        report = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

        self.assertEqual([day.completeness for day in report.days], ["complete"] * 3)
        self.assertFalse(any(issue.category == "missing_arrangement" for issue in report.issues))

    def test_repeated_poi_in_distinct_activities_is_allowed(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        first_day = draft.days[0]
        repeated = first_day.activities[1].model_copy(update={"activity_id": "activity-repeated-poi"})
        updated_day = first_day.model_copy(update={"activities": (*first_day.activities, repeated)})
        draft = draft.model_copy(update={"days": (updated_day, *draft.days[1:])})

        report = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

        self.assertEqual(report.days[0].completeness, "complete")

    def test_must_visit_requires_candidate_binding_and_matching_scheduled_activity(self):
        must_visit = requirement("req-bund", "must_visit", "外滩")
        planning_input = make_input(requirements=(must_visit,))
        facts, candidates = make_facts_and_candidates()
        sight = candidates[0].model_copy(update={"requirement_ids": ("req-bund",)})
        candidates = (sight, candidates[1])
        draft = make_filled_draft(planning_input, candidates)
        first_day = draft.days[0]
        activities = tuple(
            activity.model_copy(update={"requirement_ids": ("req-bund",)})
            if activity.candidate_ref == sight.candidate_id else activity
            for activity in first_day.activities
        )
        draft = draft.model_copy(update={
            "days": (first_day.model_copy(update={"activities": activities}), *draft.days[1:]),
        })

        report = validate_daily_plan(planning_input, draft, facts, candidates)

        result = report.requirements[0]
        self.assertEqual(result.status, "satisfied")
        self.assertTrue(result.activity_ids)
        self.assertTrue(result.place_fact_ids)

    def test_must_visit_stays_satisfied_when_another_candidate_is_also_bound(self):
        must_visit = requirement("req-bund", "must_visit", "外滩")
        planning_input = make_input(requirements=(must_visit,))
        facts, candidates = make_facts_and_candidates()
        alternate_place = facts.places[1].model_copy(update={
            "fact_id": "fact-sight-alternate",
            "poi_id": "poi-sight-alternate",
            "name": "备用必去候选",
        })
        facts = facts.model_copy(update={"places": (*facts.places, alternate_place)})
        bound = candidates[0].model_copy(update={"requirement_ids": ("req-bund",)})
        alternate = CandidateRef(
            candidate_id="candidate-sight-alternate",
            category="sightseeing",
            poi_id=alternate_place.poi_id,
            place_fact_id=alternate_place.fact_id,
            requirement_ids=("req-bund",),
        )
        candidates = (alternate, candidates[1], bound)
        draft = make_filled_draft(planning_input, candidates)
        first_day = draft.days[0]
        activities = tuple(
            activity.model_copy(update={"requirement_ids": ("req-bund",)})
            if activity.candidate_ref == bound.candidate_id else activity
            for activity in first_day.activities
        )
        draft = draft.model_copy(update={
            "days": (first_day.model_copy(update={"activities": activities}), *draft.days[1:]),
        })

        report = validate_daily_plan(planning_input, draft, facts, candidates)

        self.assertEqual(report.requirements[0].status, "satisfied")

    def test_dangling_candidate_requirement_and_slot_references_are_rejected(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        candidate = self.candidates[0].model_copy(update={
            "requirement_ids": ("unknown-requirement",),
            "slot_ids": ("unknown-slot",),
        })

        with self.assertRaises(ContractValidationError) as raised:
            validate_daily_plan(self.planning_input, draft, self.facts, (candidate, self.candidates[1]))

        self.assertEqual(
            {issue.code for issue in raised.exception.issues},
            {"dangling_candidate_requirement", "dangling_candidate_slot"},
        )

    def test_requirement_tag_on_meal_does_not_satisfy_must_visit(self):
        must_visit = requirement("req-bund", "must_visit", "外滩")
        planning_input = make_input(requirements=(must_visit,))
        facts, candidates = make_facts_and_candidates()
        meal = candidates[1].model_copy(update={"requirement_ids": ("req-bund",)})
        candidates = (candidates[0], meal)
        draft = make_filled_draft(planning_input, candidates)
        first_day = draft.days[0]
        activities = tuple(
            activity.model_copy(update={"requirement_ids": ("req-bund",)})
            if activity.candidate_ref == meal.candidate_id else activity
            for activity in first_day.activities
        )
        draft = draft.model_copy(update={
            "days": (first_day.model_copy(update={"activities": activities}), *draft.days[1:]),
        })

        report = validate_daily_plan(planning_input, draft, facts, candidates)

        self.assertEqual(report.requirements[0].status, "unsatisfied")
        issue = next(item for item in report.issues if item.code == "must_visit_unplanned")
        self.assertEqual(issue.category, "missing_arrangement")
        self.assertTrue(issue.triggers_repair)

    def test_must_avoid_and_generic_must_include_remain_unverified(self):
        requirements = (
            requirement("req-avoid", "must_avoid", "不要去酒吧"),
            requirement("req-food", "must_include", "安排本帮菜"),
        )
        planning_input = make_input(requirements=requirements)
        facts, candidates = make_facts_and_candidates()
        draft = make_filled_draft(planning_input, candidates)

        report = validate_daily_plan(planning_input, draft, facts, candidates)

        self.assertEqual([item.status for item in report.requirements], ["unverified", "unverified"])
        issues = [item for item in report.issues if item.code == "requirement_unverified"]
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(not item.triggers_repair for item in issues))

    def test_verified_short_departure_day_is_not_required_without_activities(self):
        planning_input = make_input(departure="2026-10-12T10:00:00+08:00")
        facts, candidates = make_facts_and_candidates()
        draft = make_draft_with_activities(planning_input, {})
        timing = calculate_plan_timing(planning_input, draft, facts)

        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)

        last_day = report.days[-1]
        self.assertEqual(last_day.completeness, "not_required")
        self.assertFalse(any(
            issue.day_id == last_day.day_id and issue.category == "missing_arrangement"
            for issue in report.issues
        ))

    def test_verified_short_departure_day_with_activity_reports_conflict_without_deleting_it(self):
        planning_input = make_input(departure="2026-10-12T10:00:00+08:00")
        facts, candidates = make_facts_and_candidates()
        slot = next(slot for slot in planning_input.days[-1].slots if slot.period == "morning")
        activity = DraftActivity(
            activity_id="departure-activity",
            candidate_ref="candidate-sight",
            slot_id=slot.slot_id,
            kind="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
            duration_s=3600,
            duration_source="model",
        )
        draft = make_draft_with_activities(planning_input, {planning_input.days[-1].day_id: (activity,)})
        before = draft.model_dump_json()
        timing = calculate_plan_timing(planning_input, draft, facts)

        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)

        issue = next(item for item in report.issues if item.code == "departure_day_below_minimum_with_activities")
        self.assertEqual(issue.category, "known_conflict")
        self.assertTrue(issue.triggers_repair)
        self.assertEqual(report.days[-1].completeness, "not_required")
        self.assertEqual(draft.model_dump_json(), before)
        self.assertEqual(len(draft.days[-1].activities), 1)

    def test_unknown_return_route_does_not_become_a_conflict(self):
        facts, candidates = self.facts, self.candidates
        last_day = self.planning_input.days[-1]
        evening = next(slot for slot in last_day.slots if slot.period == "evening")
        activity = DraftActivity(
            activity_id="last-activity",
            candidate_ref="candidate-sight",
            slot_id=evening.slot_id,
            kind="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
            duration_s=3600,
            duration_source="model",
        )
        draft = make_draft_with_activities(self.planning_input, {last_day.day_id: (activity,)})
        timing = calculate_plan_timing(self.planning_input, draft, facts)

        report = validate_daily_plan(self.planning_input, draft, facts, candidates, timing)

        self.assertEqual(timing.days[-1].return_timing.status, "unverified")
        self.assertFalse(any(issue.category == "known_conflict" for issue in report.issues))
        self.assertTrue(any(
            issue.code == "return_timing_unverified" and not issue.triggers_repair
            for issue in report.issues
        ))

    def test_raw_opening_hours_are_not_treated_as_a_proven_conflict(self):
        facts, candidates = self.facts, self.candidates
        places = tuple(
            place.model_copy(update={"opening_hours_raw": "每天 08:00-17:00"})
            if place.fact_id == "fact-sight" else place
            for place in facts.places
        )
        facts = facts.model_copy(update={"places": places})
        first_day = self.planning_input.days[0]
        slot = next(slot for slot in first_day.slots if slot.period == "evening")
        activity = DraftActivity(
            activity_id="sight-evening",
            candidate_ref="candidate-sight",
            slot_id=slot.slot_id,
            kind="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
            duration_s=3600,
            duration_source="model",
        )
        draft = make_draft_with_activities(self.planning_input, {first_day.day_id: (activity,)})

        report = validate_daily_plan(self.planning_input, draft, facts, candidates)

        opening = next(issue for issue in report.issues if issue.code == "opening_hours_unverified")
        self.assertEqual(opening.category, "unknown_info")
        self.assertFalse(opening.triggers_repair)
        self.assertFalse(any(issue.category == "known_conflict" for issue in report.issues))

    def test_f07_return_lower_bound_is_reported_as_lower_bound(self):
        last_day = self.planning_input.days[-1]
        slot = next(slot for slot in last_day.slots if slot.period == "evening")
        activity = DraftActivity(
            activity_id="last-activity",
            candidate_ref="candidate-sight",
            slot_id=slot.slot_id,
            kind="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
            duration_s=3600,
            duration_source="model",
        )
        draft = make_draft_with_activities(self.planning_input, {last_day.day_id: (activity,)})
        timing_days = []
        for constraint in self.planning_input.days:
            is_departure = constraint.day_id == last_day.day_id
            return_timing = ReturnTiming(
                status="conflict" if is_departure else "not_applicable",
                departure_at=self.planning_input.request.departure_at if is_departure else None,
                buffer_s=3600,
                overrun_s=300 if is_departure else None,
                overrun_is_lower_bound=is_departure,
            )
            activity_timings = (
                ActivityTiming(
                    day_id=constraint.day_id,
                    activity_id=activity.activity_id,
                    duration_s=activity.duration_s,
                    duration_source=activity.duration_source,
                    status="estimated",
                ),
            ) if is_departure else ()
            timing_days.append(DayTimingResult(
                day_id=constraint.day_id,
                activities=activity_timings,
                summary=DaySummary(
                    day_id=constraint.day_id,
                    known_stay_s=activity.duration_s if is_departure else 0,
                    known_travel_s=0,
                    feasibility="conflict" if is_departure else "verified",
                ),
                scheduling_decision="conditional" if is_departure else "scheduled",
                return_timing=return_timing,
            ))
        timing = PlanTimingResult(
            plan_id=draft.plan_id,
            based_on_revision=draft.revision,
            days=tuple(timing_days),
        )

        report = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates, timing)

        issue = next(item for item in report.issues if item.code == "return_deadline_conflict")
        self.assertEqual(issue.category, "known_conflict")
        self.assertTrue(issue.triggers_repair)
        self.assertIn("下界", issue.message)
        self.assertIn("至少超出", issue.message)
        self.assertEqual(report.days[-1].feasibility, "conflict")

    def test_timing_from_another_revision_is_rejected(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        timing = calculate_plan_timing(self.planning_input, draft, self.facts)
        wrong_timing = timing.model_copy(update={"based_on_revision": draft.revision - 1})

        with self.assertRaisesRegex(ValueError, "当前 PlanDraft"):
            validate_daily_plan(self.planning_input, draft, self.facts, self.candidates, wrong_timing)

    def test_timing_summary_must_reference_its_own_day(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        timing = calculate_plan_timing(self.planning_input, draft, self.facts)
        first = timing.days[0]
        bad_first = first.model_copy(update={
            "summary": first.summary.model_copy(update={"day_id": "wrong-day"}),
        })
        timing = timing.model_copy(update={"days": (bad_first, *timing.days[1:])})

        with self.assertRaisesRegex(ValueError, "每日摘要必须引用所属日期"):
            validate_daily_plan(self.planning_input, draft, self.facts, self.candidates, timing)

    def test_issue_ids_are_stable_for_the_same_plan_revision(self):
        draft = make_filled_draft(self.planning_input, self.candidates)

        first = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)
        second = validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

        self.assertEqual([issue.issue_id for issue in first.issues], [issue.issue_id for issue in second.issues])

    def test_activity_with_broken_candidate_place_closure_is_rejected(self):
        draft = make_filled_draft(self.planning_input, self.candidates)
        first_day = draft.days[0]
        bad_activity = first_day.activities[1].model_copy(update={"place_fact_id": "fact-meal"})
        draft = draft.model_copy(update={
            "days": (first_day.model_copy(update={
                "activities": (first_day.activities[0], bad_activity, *first_day.activities[2:]),
            }), *draft.days[1:]),
        })

        with self.assertRaises(ContractValidationError):
            validate_daily_plan(self.planning_input, draft, self.facts, self.candidates)

    def test_summary_without_timing_keeps_route_time_unknown_and_cost_pending(self):
        draft = make_filled_draft(self.planning_input, self.candidates)

        summaries = build_day_summaries(self.planning_input, draft)

        self.assertEqual(len(summaries), 3)
        self.assertTrue(all(item.feasibility == "unverified" for item in summaries))
        self.assertTrue(all(item.cost_status == "aggregation_pending" for item in summaries))
        self.assertTrue(all(item.known_travel_s == 0 for item in summaries))

    def test_not_required_day_preview_uses_day_level_evidence_without_fake_slot_issues(self):
        planning_input = make_input(departure="2026-10-12T10:00:00+08:00")
        facts, candidates = make_facts_and_candidates()
        draft = make_draft_with_activities(planning_input, {})
        timing = calculate_plan_timing(planning_input, draft, facts)

        report = validate_daily_plan(planning_input, draft, facts, candidates, timing)

        last = report.days[-1]
        self.assertEqual(last.completeness, "not_required")
        self.assertFalse(any(
            issue.scope == "slot" and issue.day_id == last.day_id
            for issue in report.issues
        ))
        not_required_issue = next(
            issue for issue in report.issues
            if issue.day_id == last.day_id and issue.code == "departure_day_not_required"
        )
        self.assertEqual(not_required_issue.severity, "info")
        self.assertFalse(not_required_issue.triggers_repair)

        preview_draft = self._with_derived_unfilled_slots(planning_input, draft)
        preview_report = self._with_open_repair_states(planning_input, report)
        preview = ItineraryPreview(
            preview_id="not-required-preview",
            planning_input=planning_input,
            draft=preview_draft,
            facts=facts,
            candidates=candidates,
            validation=preview_report,
            day_summaries=build_day_summaries(planning_input, draft, timing),
            calculation=PreviewCalculation(
                based_on_revision=draft.revision,
                calculation_id="not-required-calculation",
                current_routes="completed",
                alternative_backfill="pending",
            ),
        )

        validate_preview(preview)

    def test_route_query_errors_flow_through_f07_f09_and_public_preview(self):
        planning_input = make_input()
        facts, candidates = make_facts_and_candidates()
        alternate_place = facts.places[1].model_copy(update={
            "fact_id": "fact-route-destination",
            "poi_id": "poi-route-destination",
            "name": "路线目标景点",
        })
        facts = facts.model_copy(update={"places": (*facts.places, alternate_place)})
        alternate_candidate = CandidateRef(
            candidate_id="candidate-route-destination",
            category="sightseeing",
            poi_id=alternate_place.poi_id,
            place_fact_id=alternate_place.fact_id,
        )
        candidates = (*candidates, alternate_candidate)
        constraint = planning_input.days[1]
        morning = next(slot for slot in constraint.slots if slot.period == "morning")
        afternoon = next(slot for slot in constraint.slots if slot.period == "afternoon")
        first = DraftActivity(
            activity_id="route-origin",
            candidate_ref="candidate-sight",
            slot_id=morning.slot_id,
            kind="sightseeing",
            poi_id="poi-sight",
            place_fact_id="fact-sight",
            duration_s=3600,
            duration_source="model",
        )
        second = DraftActivity(
            activity_id="route-destination",
            candidate_ref=alternate_candidate.candidate_id,
            slot_id=afternoon.slot_id,
            kind="sightseeing",
            poi_id=alternate_place.poi_id,
            place_fact_id=alternate_place.fact_id,
            duration_s=3600,
            duration_source="model",
        )
        draft = make_draft_with_activities(planning_input, {constraint.day_id: (first, second)})

        class FailingToolExecutor:
            def run(self, *_args, **_kwargs):
                raise TimeoutError("offline fixture")

        execution = asyncio.run(CurrentRouteService(
            tool_executor=FailingToolExecutor(),
            tool_timeout_seconds=1,
        ).run_async(planning_input, draft, facts))
        queried_leg = next(
            leg for leg in execution.draft.legs
            if any(result.status == "error" and result.route_fact_id is None for result in leg.mode_results)
        )
        self.assertTrue(all(
            result.error_category == "timeout"
            for result in queried_leg.mode_results if result.status == "error"
        ))

        report = validate_daily_plan(
            planning_input,
            execution.draft,
            execution.fact_store,
            candidates,
            execution.timing,
        )
        self.assertTrue(any(
            issue.code == "route_unverified"
            and issue.category == "unknown_info"
            and not issue.triggers_repair
            for issue in report.issues
        ))
        self.assertFalse(any(issue.category == "known_conflict" for issue in report.issues))

        preview_draft = self._with_derived_unfilled_slots(planning_input, execution.draft)
        preview = ItineraryPreview(
            preview_id="route-failure-preview",
            planning_input=planning_input,
            draft=preview_draft,
            facts=execution.fact_store,
            candidates=candidates,
            validation=self._with_open_repair_states(planning_input, report),
            day_summaries=build_day_summaries(planning_input, execution.draft, execution.timing),
            calculation=PreviewCalculation(
                based_on_revision=draft.revision,
                calculation_id="route-failure-calculation",
                current_routes="completed",
                alternative_backfill="pending",
            ),
        )

        validate_preview(preview)

    @staticmethod
    def _with_derived_unfilled_slots(planning_input, draft):
        constraints = {day.day_id: day for day in planning_input.days}
        days = []
        for day in draft.days:
            slots = constraints[day.day_id].slots
            occupied = {activity.slot_id for activity in day.activities}
            days.append(day.model_copy(update={
                "unfilled_slot_ids": tuple(
                    slot.slot_id for slot in slots
                    if slot.eligibility != "skipped" and slot.slot_id not in occupied
                ),
            }))
        return draft.model_copy(update={"days": tuple(days)})

    @staticmethod
    def _with_open_repair_states(planning_input, report):
        return report.model_copy(update={
            "repair_states": tuple(RepairState(
                day_id=day.day_id,
                completed_rounds=0,
                status="open",
            ) for day in planning_input.days),
        })


if __name__ == "__main__":
    unittest.main()
