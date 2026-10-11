"""planning_v4 每日完整性、已知冲突与未核实信息报告。"""
from __future__ import annotations

from hashlib import sha256
from typing import Iterable, Literal

from app.models.planning_v4.facts import FactStore
from app.models.planning_v4.input import PlanningInput
from app.models.planning_v4.plan import (
    DaySummary,
    DayValidation,
    DraftActivity,
    DraftDay,
    ItineraryPreview,
    LodgingProposal,
    PlanDraft,
    PlanProposal,
    PreviewCalculation,
    ProposalActivity,
    ProposalDay,
    RepairState,
    RequirementResult,
    ValidationIssue,
    ValidationReport,
)
from app.models.planning_v4.tasks import CandidateRef
from app.services.planning_v4_timing import PlanTimingResult
from app.services.planning_v4_validator import (
    ContractValidationError,
    ReferenceIssue,
    validate_plan_proposal,
    validate_preview,
)


_EXPECTED_KIND_BY_PERIOD = {
    "breakfast": "meal",
    "lunch": "meal",
    "dinner": "meal",
    "morning": "sightseeing",
    "afternoon": "sightseeing",
    "evening": "sightseeing",
}


def validate_daily_plan(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    candidates: tuple[CandidateRef, ...],
    timing: PlanTimingResult | None = None,
    *,
    repair_states: tuple[RepairState, ...] = (),
) -> ValidationReport:
    """只报告每日覆盖、硬性需求、时间冲突及未知信息，不修改草稿或轮次。"""
    _validate_timing_binding(planning_input, draft, timing)
    _validate_repair_states(planning_input, repair_states)

    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    place_by_id = {place.fact_id: place for place in facts.places}
    day_constraints = {day.day_id: day for day in planning_input.days}
    draft_days = {day.day_id: day for day in draft.days}

    _validate_draft_unfilled_references(draft, day_constraints)
    _validate_candidate_references(candidates, planning_input)
    _validate_activity_place_closure(draft, candidate_by_id, place_by_id)

    # 现有结构校验负责来源、候选类别、日期、时段和住宿引用。缺项元数据可能
    # 已过期，因此投影中的 unfilled_slot_ids 按实际活动重新计算。
    proposal = _proposal_from_draft(
        planning_input=planning_input,
        draft=draft,
        candidates=candidate_by_id,
    )
    validate_plan_proposal(proposal, planning_input, candidates, facts)

    issues: list[ValidationIssue] = []
    issues_by_day: dict[str, list[str]] = {day_id: [] for day_id in day_constraints}
    timing_by_day = {day.day_id: day for day in timing.days} if timing else {}

    def add_issue(
        *,
        code: str,
        category: Literal["missing_arrangement", "known_conflict", "unknown_info", "invalid_reference"],
        severity: Literal["info", "warning", "error"],
        scope: Literal["plan", "day", "slot", "activity", "leg"],
        day_id: str | None = None,
        target_id: str | None = None,
        message: str,
        evidence_refs: Iterable[str] = (),
        triggers_repair: bool = False,
    ) -> None:
        refs = tuple(dict.fromkeys(evidence_refs))
        issue_id = _issue_id(
            draft.plan_id,
            draft.revision,
            code,
            scope,
            day_id,
            target_id,
            refs,
        )
        if any(item.issue_id == issue_id for item in issues):
            return
        issue = ValidationIssue(
            issue_id=issue_id,
            code=code,
            category=category,
            severity=severity,
            scope=scope,
            day_id=day_id,
            target_id=target_id,
            message=message,
            evidence_refs=refs,
            triggers_repair=triggers_repair,
            based_on_revision=draft.revision,
        )
        issues.append(issue)
        if day_id is not None:
            issues_by_day[day_id].append(issue_id)

    requirement_results = _requirement_results(
        planning_input=planning_input,
        draft=draft,
        candidates=candidate_by_id,
        place_by_id=place_by_id,
    )
    for requirement, result in zip(
        planning_input.requirements.hard_requirements,
        requirement_results,
        strict=True,
    ):
        if result.status == "unsatisfied":
            add_issue(
                code="must_visit_unplanned",
                category="missing_arrangement",
                severity="error",
                scope="plan",
                target_id=requirement.requirement_id,
                message=f"必去要求“{requirement.text}”没有绑定到已核实并实际安排的地点活动。",
                evidence_refs=(*result.activity_ids, *result.place_fact_ids),
                triggers_repair=True,
            )
        elif result.status == "unverified":
            add_issue(
                code="requirement_unverified",
                category="unknown_info",
                severity="warning",
                scope="plan",
                target_id=requirement.requirement_id,
                message=f"无法仅根据当前结构化信息核实要求“{requirement.text}”是否落实。",
            )

    day_results: list[DayValidation] = []
    for constraint in planning_input.days:
        day = draft_days[constraint.day_id]
        day_timing = timing_by_day.get(day.day_id)
        short_day = bool(
            day_timing
            and day_timing.scheduling_decision == "not_scheduled_insufficient_time"
        )
        activities_by_slot: dict[str, list[DraftActivity]] = {}
        for activity in day.activities:
            activities_by_slot.setdefault(activity.slot_id, []).append(activity)

        required_missing = False
        conditional_missing = False
        required_count = 0
        for slot in constraint.slots:
            if slot.eligibility == "skipped":
                continue
            expected_kind = _EXPECTED_KIND_BY_PERIOD[slot.period]
            covered = any(
                activity.kind == expected_kind
                and activity.kind != "free_time"
                and _activity_has_verified_place(activity, candidate_by_id, place_by_id)
                for activity in activities_by_slot.get(slot.slot_id, ())
            )

            if slot.eligibility == "required":
                required_count += 1
                if not covered and not short_day:
                    required_missing = True
                    add_issue(
                        code="missing_meal" if expected_kind == "meal" else "missing_sightseeing",
                        category="missing_arrangement",
                        severity="error",
                        scope="slot",
                        day_id=day.day_id,
                        target_id=slot.slot_id,
                        message=(
                            f"{_period_label(slot.period)}尚未安排有已核实地点依据的"
                            f"{'餐馆' if expected_kind == 'meal' else '景点'}。"
                        ),
                        evidence_refs=(slot.slot_id,),
                        triggers_repair=True,
                    )
            elif not covered and not short_day:
                conditional_missing = True
                add_issue(
                    code="conditional_slot_unresolved",
                    category="unknown_info",
                    severity="warning",
                    scope="slot",
                    day_id=day.day_id,
                    target_id=slot.slot_id,
                    message=f"{_period_label(slot.period)}是否需要安排仍取决于：{slot.condition_reason}",
                    evidence_refs=(slot.slot_id,),
                )

        if short_day and day.activities:
            activity_ids = tuple(activity.activity_id for activity in day.activities)
            add_issue(
                code="departure_day_below_minimum_with_activities",
                category="known_conflict",
                severity="error",
                scope="day",
                day_id=day.day_id,
                message="F07 已证实离开日可用时间不足3小时；现有活动仍保留，但需要调整。",
                evidence_refs=activity_ids,
                triggers_repair=True,
            )
        if short_day:
            add_issue(
                code="departure_day_not_required",
                category="unknown_info",
                severity="info",
                scope="day",
                day_id=day.day_id,
                message="F07 已证实离开日可用时间不足3小时，因此该日不要求覆盖全部餐次和游览时段。",
                evidence_refs=tuple(activity.activity_id for activity in day.activities),
            )

        if day_timing is not None:
            _add_timing_issues(
                day=day,
                constraint=constraint,
                day_timing=day_timing,
                draft_activity_ids={activity.activity_id for activity in day.activities},
                add_issue=add_issue,
            )
        else:
            add_issue(
                code="timing_not_provided",
                category="unknown_info",
                severity="warning",
                scope="day",
                day_id=day.day_id,
                message="尚未提供当前草稿版本的F07时间与返程计算，日程可行性未核实。",
            )

        _add_place_unknowns(
            day=day,
            place_by_id=place_by_id,
            add_issue=add_issue,
        )
        _add_weather_unknown(
            day=day,
            facts=facts,
            add_issue=add_issue,
        )

        if short_day:
            completeness = "not_required"
        elif required_missing:
            completeness = "incomplete"
        elif conditional_missing:
            completeness = "conditional"
        elif required_count == 0:
            completeness = "not_required"
        else:
            completeness = "complete"

        if day_timing is None:
            feasibility = "unverified"
        elif day_timing.summary.feasibility == "conflict":
            feasibility = "conflict"
        elif day_timing.summary.feasibility == "verified":
            feasibility = "verified"
        else:
            feasibility = "unverified"
        if any(issue.category == "known_conflict" and issue.day_id == day.day_id for issue in issues):
            feasibility = "conflict"
        day_results.append(DayValidation(
            day_id=day.day_id,
            completeness=completeness,
            feasibility=feasibility,
            issue_ids=tuple(issues_by_day[day.day_id]),
        ))

    report = ValidationReport(
        plan_id=draft.plan_id,
        based_on_revision=draft.revision,
        issues=tuple(issues),
        days=tuple(day_results),
        requirements=requirement_results,
        repair_states=repair_states,
    )
    _validate_preview_graph(
        planning_input=planning_input,
        draft=draft,
        facts=facts,
        candidates=candidates,
        report=report,
    )
    return report


def build_day_summaries(
    planning_input: PlanningInput,
    draft: PlanDraft,
    timing: PlanTimingResult | None = None,
) -> tuple[DaySummary, ...]:
    """生成不含消费汇总的每日时间摘要；没有F07结果时明确保留未知路段。"""
    _validate_draft_context(planning_input, draft)
    _validate_timing_binding(planning_input, draft, timing)
    timing_by_day = {day.day_id: day for day in timing.days} if timing else {}
    legs_by_day: dict[str, list[str]] = {day.day_id: [] for day in draft.days}
    for leg in draft.legs:
        if leg.selection_source != "same_place":
            legs_by_day[leg.day_id].append(leg.leg_id)

    summaries: list[DaySummary] = []
    for day in draft.days:
        day_timing = timing_by_day.get(day.day_id)
        if day_timing is not None:
            summaries.append(day_timing.summary.model_copy(update={
                "cost_status": "aggregation_pending",
                "known_cost_subtotal": None,
            }))
            continue
        summaries.append(DaySummary(
            day_id=day.day_id,
            known_stay_s=sum(activity.duration_s for activity in day.activities),
            known_travel_s=0,
            unknown_leg_ids=tuple(sorted(legs_by_day[day.day_id])),
            feasibility="unverified",
            cost_status="aggregation_pending",
        ))
    return tuple(summaries)


def _proposal_from_draft(
    *,
    planning_input: PlanningInput,
    draft: PlanDraft,
    candidates: dict[str, CandidateRef],
) -> PlanProposal:
    proposal_days: list[ProposalDay] = []
    constraints = {day.day_id: day for day in planning_input.days}
    for day in draft.days:
        constraint = constraints.get(day.day_id)
        slots = constraint.slots if constraint is not None else ()
        covered_slot_ids = {
            activity.slot_id
            for activity in day.activities
            if _activity_covers_slot(
                activity,
                slots,
                candidates,
                None,
            )
        }
        actual_unfilled = tuple(
            slot.slot_id for slot in slots
            if slot.eligibility != "skipped" and slot.slot_id not in covered_slot_ids
        )
        proposal_days.append(ProposalDay(
            day_id=day.day_id,
            day_date=day.day_date,
            theme=day.theme,
            activities=tuple(ProposalActivity(
                proposal_ref=activity.activity_id,
                candidate_ref=activity.candidate_ref,
                slot_id=activity.slot_id,
                kind=activity.kind,
                requirement_ids=activity.requirement_ids,
                recommendation_reason=activity.recommendation_reason,
            ) for activity in day.activities),
            unfilled_slot_ids=actual_unfilled,
            lodging=(None if day.lodging is None else LodgingProposal(
                candidate_ref=day.lodging.candidate_ref,
                area_hint_id=day.lodging.area_hint_id,
            )),
        ))
    return PlanProposal(
        request_id=draft.request_id,
        rule_version=draft.rule_version,
        days=tuple(proposal_days),
        area_hints=draft.area_hints,
    )


def _validate_draft_unfilled_references(draft: PlanDraft, constraints: dict) -> None:
    issues: list[ReferenceIssue] = []
    for day in draft.days:
        constraint = constraints.get(day.day_id)
        if constraint is None:
            continue
        slots = {slot.slot_id: slot for slot in constraint.slots}
        if len(day.unfilled_slot_ids) != len(set(day.unfilled_slot_ids)):
            issues.append(ReferenceIssue(
                "duplicate_unfilled_slot",
                f"day:{day.day_id}.unfilled_slot_ids",
                "缺项时段不能重复",
            ))
        for slot_id in day.unfilled_slot_ids:
            slot = slots.get(slot_id)
            if slot is None or slot.eligibility == "skipped":
                issues.append(ReferenceIssue(
                    "invalid_unfilled_slot",
                    f"day:{day.day_id}.unfilled_slot_ids",
                    "缺项必须引用未跳过的有效时段",
                ))
    if issues:
        raise ContractValidationError(tuple(issues))


def _validate_activity_place_closure(draft, candidates, places) -> None:
    issues: list[ReferenceIssue] = []
    for day_index, day in enumerate(draft.days):
        for activity_index, activity in enumerate(day.activities):
            if activity.kind == "free_time":
                continue
            path = f"draft.days[{day_index}].activities[{activity_index}]"
            candidate = candidates.get(activity.candidate_ref or "")
            if candidate is None:
                # The existing proposal validator emits the canonical dangling ref.
                continue
            place = places.get(candidate.place_fact_id)
            if (
                activity.poi_id != candidate.poi_id
                or activity.place_fact_id != candidate.place_fact_id
                or place is None
                or activity.poi_id != place.poi_id
            ):
                issues.append(ReferenceIssue(
                    "candidate_activity_mismatch",
                    path,
                    "活动必须闭包到同一个候选、地点事实和 POI",
                ))
    if issues:
        raise ContractValidationError(tuple(issues))


def _validate_candidate_references(candidates, planning_input) -> None:
    """校验候选携带的时段与需求引用，包括当前未被活动使用的候选。"""
    known_slots = {
        slot.slot_id
        for day in planning_input.days
        for slot in day.slots
    }
    known_requirements = {
        requirement.requirement_id
        for requirement in planning_input.requirements.hard_requirements
    }
    issues: list[ReferenceIssue] = []
    for index, candidate in enumerate(candidates):
        path = f"candidates[{index}]"
        if len(candidate.slot_ids) != len(set(candidate.slot_ids)):
            issues.append(ReferenceIssue(
                "duplicate_candidate_slot",
                f"{path}.slot_ids",
                "候选适用时段不能重复",
            ))
        if set(candidate.slot_ids) - known_slots:
            issues.append(ReferenceIssue(
                "dangling_candidate_slot",
                f"{path}.slot_ids",
                "候选引用了 PlanningInput 中不存在的时段",
            ))
        if len(candidate.requirement_ids) != len(set(candidate.requirement_ids)):
            issues.append(ReferenceIssue(
                "duplicate_candidate_requirement",
                f"{path}.requirement_ids",
                "候选关联的硬性需求不能重复",
            ))
        if set(candidate.requirement_ids) - known_requirements:
            issues.append(ReferenceIssue(
                "dangling_candidate_requirement",
                f"{path}.requirement_ids",
                "候选引用了 PlanningInput 中不存在的硬性需求",
            ))
    if issues:
        raise ContractValidationError(tuple(issues))


def _requirement_results(*, planning_input, draft, candidates, place_by_id):
    results: list[RequirementResult] = []
    for requirement in planning_input.requirements.hard_requirements:
        if requirement.kind in {"must_avoid", "must_include"}:
            # 当前数据结构没有可核验的排除实体或通用偏好约束，不能从文字推断。
            results.append(RequirementResult(
                requirement_id=requirement.requirement_id,
                status="unverified",
            ))
            continue

        supported: list[tuple[DraftActivity, CandidateRef]] = []
        for day in draft.days:
            for activity in day.activities:
                candidate = candidates.get(activity.candidate_ref or "")
                if (
                    activity.kind == "sightseeing"
                    and requirement.requirement_id in activity.requirement_ids
                    and candidate is not None
                    and candidate.category == "sightseeing"
                    and requirement.requirement_id in candidate.requirement_ids
                    and candidate.place_fact_id == activity.place_fact_id
                    and candidate.poi_id == activity.poi_id
                    and candidate.place_fact_id in place_by_id
                    and place_by_id[candidate.place_fact_id].poi_id == candidate.poi_id
                ):
                    supported.append((activity, candidate))
        if supported:
            results.append(RequirementResult(
                requirement_id=requirement.requirement_id,
                status="satisfied",
                activity_ids=tuple(activity.activity_id for activity, _ in supported),
                place_fact_ids=tuple(dict.fromkeys(candidate.place_fact_id for _, candidate in supported)),
            ))
        else:
            results.append(RequirementResult(
                requirement_id=requirement.requirement_id,
                status="unsatisfied",
            ))
    return tuple(results)


def _add_timing_issues(*, day, constraint, day_timing, draft_activity_ids, add_issue) -> None:
    timings_by_activity = {item.activity_id: item for item in day_timing.activities}
    if (
        day_timing.scheduling_decision == "not_scheduled_insufficient_time"
        and not day.activities
    ):
        return

    for activity in day.activities:
        activity_timing = timings_by_activity.get(activity.activity_id)
        if activity_timing is None:
            continue
        if (
            activity_timing.status == "unverified"
            and activity_timing.unknown_reason != "departure_day_under_minimum_usable_time"
        ):
            add_issue(
                code="activity_timing_unverified",
                category="unknown_info",
                severity="warning",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message=f"活动时间尚未核实：{activity_timing.unknown_reason or '缺少时间依据'}。",
                evidence_refs=(activity.activity_id,),
            )
        if (
            constraint.available_end is not None
            and activity_timing.end_at is not None
            and activity_timing.end_at > constraint.available_end
        ):
            add_issue(
                code="activity_exceeds_available_end",
                category="known_conflict",
                severity="error",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message="F07已证实活动结束时间超过该日已核实的可用终点。",
                evidence_refs=(activity.activity_id,),
                triggers_repair=True,
            )

    for transition in day_timing.unknown_transitions:
        leg_id = transition.leg_id
        target_activity_id = (
            transition.to_reference_id
            if transition.to_reference_id in draft_activity_ids
            else None
        )
        add_issue(
            code="route_unverified",
            category="unknown_info",
            severity="warning",
            scope="leg" if leg_id else "activity" if target_activity_id else "day",
            day_id=day.day_id,
            target_id=leg_id or target_activity_id,
            message=f"路段耗时尚未核实：{transition.reason}。",
            evidence_refs=(leg_id or target_activity_id,) if leg_id or target_activity_id else (),
        )

    return_timing = day_timing.return_timing
    if return_timing.status == "conflict" and day.activities:
        leg_id = return_timing.route_leg_id
        if return_timing.overrun_is_lower_bound:
            detail = f"已证实返程至少超出截止时间 {return_timing.overrun_s or 0} 秒；该数值是下界。"
        else:
            detail = f"已证实返程超出截止时间 {return_timing.overrun_s or 0} 秒。"
        add_issue(
            code="return_deadline_conflict",
            category="known_conflict",
            severity="error",
            scope="leg" if leg_id else "day",
            day_id=day.day_id,
            target_id=leg_id,
            message=detail,
            evidence_refs=(leg_id,) if leg_id else tuple(
                activity.activity_id for activity in day.activities[-1:]
            ),
            triggers_repair=True,
        )
    elif (
        return_timing.status == "unverified"
        and return_timing.departure_at is not None
        and day.activities
    ):
        add_issue(
            code="return_timing_unverified",
            category="unknown_info",
            severity="warning",
            scope="day",
            day_id=day.day_id,
            message="返程地点或路线尚未得到足够核实，不能据此断言安排冲突。",
            evidence_refs=(return_timing.route_leg_id,) if return_timing.route_leg_id else (),
        )


def _add_place_unknowns(*, day, place_by_id, add_issue) -> None:
    for activity in day.activities:
        if activity.kind == "free_time":
            continue
        place = place_by_id.get(activity.place_fact_id or "")
        if place is None:
            continue
        if place.cost.value is None:
            add_issue(
                code="cost_unavailable",
                category="unknown_info",
                severity="warning",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message="该地点没有可用的人均消费数据；不会据此估算价格或触发补查修正。",
                evidence_refs=(activity.activity_id, place.fact_id),
            )
        if place.opening_hours_raw is None:
            add_issue(
                code="opening_hours_unavailable",
                category="unknown_info",
                severity="warning",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message="地点未提供原始营业时间；当前无法核实开放状态。",
                evidence_refs=(activity.activity_id, place.fact_id),
            )
        else:
            add_issue(
                code="opening_hours_unverified",
                category="unknown_info",
                severity="warning",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message="地点提供了原始营业时间，但当前没有可靠解析结果，不能据此断言开放冲突。",
                evidence_refs=(activity.activity_id, place.fact_id),
            )


def _add_weather_unknown(*, day, facts, add_issue) -> None:
    if day.weather_fact_id is None:
        add_issue(
            code="weather_unavailable",
            category="unknown_info",
            severity="warning",
            scope="day",
            day_id=day.day_id,
            message="该日没有绑定天气事实。",
        )
        return
    weather = next((item for item in facts.weather if item.fact_id == day.weather_fact_id), None)
    if weather is not None and day.day_date in {item.forecast_date for item in weather.forecasts}:
        return
    if weather is not None and day.day_date in weather.missing_dates:
        add_issue(
            code="weather_unavailable",
            category="unknown_info",
            severity="warning",
            scope="day",
            day_id=day.day_id,
            message="天气事实明确记录该日期未覆盖。",
            evidence_refs=(weather.fact_id,),
        )


def _validate_preview_graph(*, planning_input, draft, facts, candidates, report) -> None:
    constraints = {day.day_id: day for day in planning_input.days}
    probe_days: list[DraftDay] = []
    for day in draft.days:
        effective_unfilled = tuple(
            slot.slot_id
            for slot in constraints[day.day_id].slots
            if slot.eligibility != "skipped"
            and not any(
                _activity_covers_slot(activity, (slot,), {
                    candidate.candidate_id: candidate for candidate in candidates
                }, {
                    place.fact_id: place for place in facts.places
                })
                for activity in day.activities
            )
        )
        probe_days.append(day.model_copy(update={"unfilled_slot_ids": effective_unfilled}))
    probe_draft = draft.model_copy(update={"days": tuple(probe_days)})
    states_by_day = {state.day_id: state for state in report.repair_states}
    probe_states = tuple(
        states_by_day.get(day.day_id, RepairState(
            day_id=day.day_id,
            completed_rounds=0,
            status="open",
        ))
        for day in draft.days
    )
    probe_report = report.model_copy(update={
        "repair_states": probe_states,
    })
    preview = ItineraryPreview(
        preview_id="validation-only",
        planning_input=planning_input,
        draft=probe_draft,
        facts=facts,
        candidates=candidates,
        validation=probe_report,
        calculation=PreviewCalculation(
            based_on_revision=draft.revision,
            calculation_id="validation-only",
            current_routes="completed",
            alternative_backfill="pending",
        ),
    )
    validate_preview(preview)


def _activity_has_verified_place(activity, candidates, places) -> bool:
    if activity.kind == "free_time":
        return False
    candidate = candidates.get(activity.candidate_ref or "")
    if candidate is None:
        return False
    if candidate.poi_id != activity.poi_id or candidate.place_fact_id != activity.place_fact_id:
        return False
    if not places:
        return True
    place = places.get(candidate.place_fact_id)
    return bool(place and place.poi_id == candidate.poi_id and place.location is not None)


def _activity_covers_slot(activity, slots, candidates, places) -> bool:
    slot = next((item for item in slots if item.slot_id == activity.slot_id), None)
    if slot is None or slot.eligibility == "skipped":
        return False
    expected_kind = _EXPECTED_KIND_BY_PERIOD[slot.period]
    return activity.kind == expected_kind and _activity_has_verified_place(activity, candidates, places)


def _validate_timing_binding(planning_input, draft, timing) -> None:
    _validate_draft_context(planning_input, draft)
    if timing is None:
        return
    if timing.plan_id != draft.plan_id or timing.based_on_revision != draft.revision:
        raise ValueError("F07 timing 必须绑定当前 PlanDraft plan_id 和 revision")
    expected_days = {day.day_id for day in planning_input.days}
    timing_days = [day.day_id for day in timing.days]
    if len(timing_days) != len(set(timing_days)) or set(timing_days) != expected_days:
        raise ValueError("F07 timing 必须与 PlanningInput 日期一一对应")
    legs_by_day: dict[str, set[str]] = {day_id: set() for day_id in expected_days}
    activities_by_day: dict[str, dict[str, DraftActivity]] = {
        day.day_id: {activity.activity_id: activity for activity in day.activities}
        for day in draft.days
    }
    for leg in draft.legs:
        legs_by_day[leg.day_id].add(leg.leg_id)
    for day_timing in timing.days:
        if day_timing.summary.day_id != day_timing.day_id:
            raise ValueError("F07 每日摘要必须引用所属日期")
        actual = [item.activity_id for item in day_timing.activities]
        expected = set(activities_by_day[day_timing.day_id])
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(f"F07 timing 的活动集合必须与草稿日期 {day_timing.day_id} 一致")
        for activity_timing in day_timing.activities:
            activity = activities_by_day[day_timing.day_id][activity_timing.activity_id]
            if activity_timing.day_id != day_timing.day_id:
                raise ValueError("F07 活动时间必须引用所属日期")
            if (
                activity_timing.duration_s != activity.duration_s
                or activity_timing.duration_source != activity.duration_source
            ):
                raise ValueError("F07 活动时长必须与当前草稿一致")
            if activity_timing.incoming_leg_id is not None and activity_timing.incoming_leg_id not in legs_by_day[day_timing.day_id]:
                raise ValueError("F07 活动时间引用了不存在的同日路段")
        for transition in day_timing.unknown_transitions:
            if transition.to_reference_id not in expected:
                raise ValueError("F07 未知转移必须定位到当前日期的活动")
            if transition.leg_id is not None and transition.leg_id not in legs_by_day[day_timing.day_id]:
                raise ValueError("F07 未知转移引用了不存在的同日路段")
        if any(leg_id not in legs_by_day[day_timing.day_id] for leg_id in day_timing.summary.unknown_leg_ids):
            raise ValueError("F07 每日摘要引用了不存在的同日路段")
        return_leg_id = day_timing.return_timing.route_leg_id
        if return_leg_id is not None and return_leg_id not in legs_by_day[day_timing.day_id]:
            raise ValueError("F07 返程结果引用了不存在的同日路段")


def _validate_draft_context(planning_input, draft) -> None:
    if draft.request_id != planning_input.request.request_id:
        raise ValueError("草稿必须引用当前 PlanningInput request_id")
    if draft.rule_version != planning_input.rules.rule_version:
        raise ValueError("草稿必须引用当前 PlanningInput rule_version")
    constraints = {day.day_id: day.day_date for day in planning_input.days}
    draft_dates = {day.day_id: day.day_date for day in draft.days}
    if set(draft_dates) != set(constraints) or draft_dates != constraints:
        raise ValueError("草稿日期必须与 PlanningInput 一一对应")


def _validate_repair_states(planning_input, repair_states) -> None:
    expected_days = {day.day_id for day in planning_input.days}
    states = [state.day_id for state in repair_states]
    issues: list[ReferenceIssue] = []
    if len(states) != len(set(states)):
        issues.append(ReferenceIssue("duplicate_repair_state", "repair_states", "修复状态的 day_id 不能重复"))
    if set(states) - expected_days:
        issues.append(ReferenceIssue("dangling_repair_state", "repair_states", "修复状态引用了不存在的日期"))
    if any(state.completed_rounds > planning_input.rules.max_repair_rounds_per_day for state in repair_states):
        issues.append(ReferenceIssue("repair_round_limit_exceeded", "repair_states", "修复轮数超过当前规则上限"))
    if issues:
        raise ContractValidationError(tuple(issues))


def _issue_id(plan_id, revision, code, scope, day_id, target_id, evidence_refs) -> str:
    seed = "|".join((
        plan_id,
        str(revision),
        code,
        scope,
        day_id or "",
        target_id or "",
        ",".join(evidence_refs),
    ))
    return "issue-" + sha256(seed.encode("utf-8")).hexdigest()[:24]


def _period_label(period: str) -> str:
    return {
        "breakfast": "早餐",
        "morning": "上午游览",
        "lunch": "午餐",
        "afternoon": "下午游览",
        "dinner": "晚餐",
        "evening": "晚上游览",
    }[period]
