"""planning_v4 地点覆盖与附带信息报告，不校验精确时间或交通可行性。"""
from __future__ import annotations

from hashlib import sha256
from math import isfinite
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


def is_required_place_gap(planning_input: PlanningInput, issue: ValidationIssue) -> bool:
    """业务修订只接受必需时段缺真实地点，其他提示不启动补查。"""
    if not issue.triggers_repair or issue.category != "missing_arrangement" or issue.scope != "slot":
        return False
    day = next((day for day in planning_input.days if day.day_id == issue.day_id), None)
    slot = next((slot for slot in day.slots if slot.slot_id == issue.target_id), None) if day else None
    if slot is None or slot.eligibility != "required":
        return False
    return issue.code == ("missing_meal" if _EXPECTED_KIND_BY_PERIOD[slot.period] == "meal" else "missing_sightseeing")


def validate_daily_plan(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    candidates: tuple[CandidateRef, ...],
    *,
    repair_states: tuple[RepairState, ...] = (),
) -> ValidationReport:
    """只报告每日地点覆盖、硬性需求及附带信息，不计算活动时间。"""
    _validate_draft_context(planning_input, draft)
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
    validate_plan_proposal(
        proposal,
        planning_input,
        candidates,
        facts,
        user_activity_refs=frozenset(
            activity.activity_id
            for day in draft.days
            for activity in day.activities
            if activity.activity_source == "user"
        ),
    )

    issues: list[ValidationIssue] = []
    issues_by_day: dict[str, list[str]] = {day_id: [] for day_id in day_constraints}

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
                triggers_repair=False,
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

    # 重复是模型安排质量反馈，不是额外搜索或删除活动的依据。
    sightseeing_by_poi: dict[str, list[tuple[DraftDay, DraftActivity]]] = {}
    for day in draft.days:
        for activity in day.activities:
            if activity.kind == "sightseeing" and activity.poi_id:
                sightseeing_by_poi.setdefault(activity.poi_id, []).append((day, activity))
    for occurrences in sightseeing_by_poi.values():
        if len({day.day_id for day, _ in occurrences}) < 2:
            continue
        first_day_id = occurrences[0][0].day_id
        for day, activity in occurrences:
            if day.day_id == first_day_id or (activity.recommendation_reason or "").strip():
                continue
            add_issue(
                code="cross_day_sightseeing_repeat",
                category="unknown_info",
                severity="warning",
                scope="activity",
                day_id=day.day_id,
                target_id=activity.activity_id,
                message="同一景点已在其他日期安排，本次跨天重复没有说明理由，请核对需求。",
                evidence_refs=tuple(item.activity_id for _, item in occurrences),
            )

    day_results: list[DayValidation] = []
    for constraint in planning_input.days:
        day = draft_days[constraint.day_id]
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
                if not covered:
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
            elif not covered:
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

        if required_missing:
            completeness = "incomplete"
        elif conditional_missing:
            completeness = "conditional"
        elif required_count == 0:
            completeness = "not_required"
        else:
            completeness = "complete"

        day_results.append(DayValidation(
            day_id=day.day_id,
            completeness=completeness,
            feasibility="unverified",
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
    facts: FactStore,
) -> tuple[DaySummary, ...]:
    """只汇总已查询交通耗时；不推算活动时刻或停留时长。"""
    _validate_draft_context(planning_input, draft)
    routes = {route.fact_id: route for route in facts.routes}
    summaries = []
    for day in draft.days:
        known_travel_s = 0.0
        unknown_leg_ids = []
        for leg in draft.legs:
            if leg.day_id != day.day_id or leg.selection_source == "same_place":
                continue
            selected = next((result for result in leg.mode_results
                             if result.mode == leg.selected_mode and result.status == "available"), None)
            route = routes.get(selected.route_fact_id) if selected else None
            option = next((option for option in route.options
                           if option.option_id == leg.selected_option_id), None) if route else None
            duration = option.duration_s if option else None
            if duration is None or not isfinite(duration) or duration < 0:
                unknown_leg_ids.append(leg.leg_id)
            else:
                known_travel_s += duration
        summaries.append(DaySummary(
            day_id=day.day_id,
            known_travel_s=known_travel_s,
            unknown_leg_ids=tuple(unknown_leg_ids),
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
            alternative_backfill="not_required",
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
