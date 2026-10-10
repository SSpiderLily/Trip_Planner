"""v4 行程图引用校验。该模块只校验结构，不执行规划或调度。"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.models.planning_v4.facts import FactStore, PlaceFact, RouteFact
from app.models.planning_v4.input import PlanningInput
from app.models.planning_v4.plan import (
    AddActivity,
    DeleteActivity,
    EditCommand,
    ItineraryPreview,
    PlanDraft,
    PlanProposal,
    DayRevision,
    ValidationReport,
    ReorderActivities,
    SelectLegMode,
)
from app.models.planning_v4.tasks import CandidateRef


@dataclass(frozen=True)
class ReferenceIssue:
    code: str
    path: str
    message: str


class ContractValidationError(ValueError):
    def __init__(self, issues: tuple[ReferenceIssue, ...]):
        self.issues = issues
        summary = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        super().__init__(summary or "v4 契约校验失败")


def _raise_if_any(issues: list[ReferenceIssue]) -> None:
    if issues:
        raise ContractValidationError(tuple(issues))


def _index_candidates(candidates: tuple[CandidateRef, ...], issues: list[ReferenceIssue]) -> dict[str, CandidateRef]:
    by_id: dict[str, CandidateRef] = {}
    for index, candidate in enumerate(candidates):
        if candidate.candidate_id in by_id:
            issues.append(ReferenceIssue("duplicate_candidate_id", f"candidates[{index}].candidate_id", "candidate_id 必须唯一"))
        else:
            by_id[candidate.candidate_id] = candidate
    return by_id


def _source_index(facts: FactStore, issues: list[ReferenceIssue]):
    source_by_id = {source.tool_call_id: source for source in facts.sources}
    for index, place in enumerate(facts.places):
        for source_id in place.source_refs:
            if source_id not in source_by_id:
                issues.append(ReferenceIssue("dangling_source", f"facts.places[{index}].source_refs", f"来源 {source_id} 不存在"))
        for field_name, source_id in place.field_sources.items():
            if source_id not in source_by_id:
                issues.append(ReferenceIssue("dangling_field_source", f"facts.places[{index}].field_sources.{field_name}", f"来源 {source_id} 不存在"))
    for kind, items in (("weather", facts.weather), ("routes", facts.routes)):
        for index, fact in enumerate(items):
            source_id = fact.source.tool_call_id
            if source_id not in source_by_id:
                issues.append(ReferenceIssue("dangling_source", f"facts.{kind}[{index}].source", f"来源 {source_id} 不存在"))
            elif source_by_id[source_id] != fact.source:
                issues.append(ReferenceIssue("source_mismatch", f"facts.{kind}[{index}].source", "事实来源与 FactStore 来源记录不一致"))
    return source_by_id


def _validate_candidate(candidate: CandidateRef, places: dict[str, PlaceFact], path: str, issues: list[ReferenceIssue]) -> None:
    fact = places.get(candidate.place_fact_id)
    if fact is None:
        issues.append(ReferenceIssue("dangling_candidate_fact", f"{path}.place_fact_id", f"地点事实 {candidate.place_fact_id} 不存在"))
    elif fact.poi_id != candidate.poi_id:
        issues.append(ReferenceIssue("candidate_poi_mismatch", path, "候选 poi_id 与地点事实不一致"))
    elif fact.location is None:
        issues.append(ReferenceIssue("candidate_location_missing", f"{path}.place_fact_id", "缺少坐标的地点不能作为已核实候选"))


def validate_plan_proposal(
    proposal: PlanProposal,
    planning_input: PlanningInput,
    candidates: tuple[CandidateRef, ...],
    facts: FactStore,
) -> None:
    """确保模型提案只能引用既有候选、有效日期和未跳过时段。"""
    issues: list[ReferenceIssue] = []
    if proposal.request_id != planning_input.request.request_id:
        issues.append(ReferenceIssue("request_mismatch", "proposal.request_id", "提案必须引用当前原始需求"))
    if proposal.rule_version != planning_input.rules.rule_version:
        issues.append(ReferenceIssue("rule_version_mismatch", "proposal.rule_version", "提案必须使用当前规则版本"))
    candidate_by_id = _index_candidates(candidates, issues)
    _source_index(facts, issues)
    place_by_id = {place.fact_id: place for place in facts.places}
    for candidate in candidates:
        _validate_candidate(candidate, place_by_id, f"candidate:{candidate.candidate_id}", issues)
    input_days = {day.day_id: day for day in planning_input.days}
    if {day.day_id for day in proposal.days} != set(input_days):
        issues.append(ReferenceIssue("day_set_mismatch", "proposal.days", "提案日期集合必须覆盖 PlanningInput 日期"))
    if {day.day_date for day in proposal.days} != {day.day_date for day in planning_input.days}:
        issues.append(ReferenceIssue("day_date_set_mismatch", "proposal.days", "提案日期必须覆盖 PlanningInput 日期"))
    req_ids = {req.requirement_id for req in planning_input.requirements.hard_requirements}
    area_by_id = {area.area_hint_id: area for area in proposal.area_hints}
    if len(area_by_id) != len(proposal.area_hints):
        issues.append(ReferenceIssue("duplicate_area_hint", "proposal.area_hints", "area_hint_id 必须唯一"))
    fact_ids = set(place_by_id) | {item.fact_id for item in facts.weather} | {item.fact_id for item in facts.routes}
    poi_ids = {place.poi_id for place in facts.places}
    for area_index, area in enumerate(proposal.area_hints):
        if any(fact_id not in fact_ids for fact_id in area.basis_fact_ids):
            issues.append(ReferenceIssue("dangling_area_basis", f"proposal.area_hints[{area_index}].basis_fact_ids", "区域依据事实不存在"))
        if any(poi_id not in poi_ids for poi_id in area.anchor_poi_ids):
            issues.append(ReferenceIssue("dangling_area_anchor", f"proposal.area_hints[{area_index}].anchor_poi_ids", "区域锚点 POI 不存在"))
        if area.center is not None and not area.basis_fact_ids:
            issues.append(ReferenceIssue("unsupported_area_center", f"proposal.area_hints[{area_index}].center", "区域中心必须关联事实依据"))

    for day_index, proposal_day in enumerate(proposal.days):
        path = f"proposal.days[{day_index}]"
        constraint = input_days.get(proposal_day.day_id)
        if constraint is None:
            issues.append(ReferenceIssue("dangling_day", f"{path}.day_id", "提案日期不在 PlanningInput 中"))
            continue
        if proposal_day.day_date != constraint.day_date:
            issues.append(ReferenceIssue("day_date_mismatch", f"{path}.day_date", "提案日期与 PlanningInput 不一致"))
        slots = {slot.slot_id: slot for slot in constraint.slots}
        for activity_index, activity in enumerate(proposal_day.activities):
            activity_path = f"{path}.activities[{activity_index}]"
            if activity.existing_activity_id is not None:
                issues.append(ReferenceIssue("initial_existing_activity", f"{activity_path}.existing_activity_id", "首次规划提案不能引用既有活动 ID"))
            slot = slots.get(activity.slot_id)
            if slot is None:
                issues.append(ReferenceIssue("dangling_slot", f"{activity_path}.slot_id", "活动时段不存在"))
            elif slot.eligibility == "skipped":
                issues.append(ReferenceIssue("activity_in_skipped_slot", f"{activity_path}.slot_id", "skipped 时段不能安排活动"))
            if activity.candidate_ref is not None:
                candidate = candidate_by_id.get(activity.candidate_ref)
                if candidate is None:
                    issues.append(ReferenceIssue("dangling_candidate", f"{activity_path}.candidate_ref", "模型提案引用了未知候选"))
                else:
                    expected_category = "sightseeing" if activity.kind == "sightseeing" else "meal"
                    if candidate.category != expected_category:
                        issues.append(ReferenceIssue("candidate_category_mismatch", f"{activity_path}.candidate_ref", "候选类别与活动类型不一致"))
                    if candidate.slot_ids and activity.slot_id not in candidate.slot_ids:
                        issues.append(ReferenceIssue("candidate_slot_mismatch", f"{activity_path}.slot_id", "候选不适用于该时段"))
            unknown_requirements = set(activity.requirement_ids) - req_ids
            if unknown_requirements:
                issues.append(ReferenceIssue("dangling_requirement", f"{activity_path}.requirement_ids", "活动引用了未知硬性需求"))
        for slot_id in proposal_day.unfilled_slot_ids:
            slot = slots.get(slot_id)
            if slot is None or slot.eligibility == "skipped":
                issues.append(ReferenceIssue("invalid_unfilled_slot", f"{path}.unfilled_slot_ids", "缺项必须引用未跳过的有效时段"))
        activity_slots = {activity.slot_id for activity in proposal_day.activities}
        unfilled_slots = set(proposal_day.unfilled_slot_ids)
        for slot in slots.values():
            if slot.eligibility != "skipped" and slot.slot_id not in activity_slots and slot.slot_id not in unfilled_slots:
                issues.append(ReferenceIssue("proposal_slot_unaccounted", f"{path}.activities", f"时段 {slot.slot_id} 必须安排活动或显式记录缺项"))
        lodging = proposal_day.lodging
        if lodging:
            if lodging.candidate_ref:
                candidate = candidate_by_id.get(lodging.candidate_ref)
                if candidate is None or candidate.category != "lodging":
                    issues.append(ReferenceIssue("invalid_lodging_candidate", f"{path}.lodging.candidate_ref", "住宿必须引用已核实的酒店候选"))
                elif candidate.place_fact_id not in place_by_id or candidate.poi_id != place_by_id[candidate.place_fact_id].poi_id:
                    issues.append(ReferenceIssue("lodging_candidate_mismatch", f"{path}.lodging.candidate_ref", "住宿候选必须关联对应地点事实"))
            if lodging.area_hint_id and lodging.area_hint_id not in area_by_id:
                issues.append(ReferenceIssue("dangling_lodging_area", f"{path}.lodging.area_hint_id", "住宿区域引用不存在"))
    _raise_if_any(issues)


def validate_preview(
    preview: ItineraryPreview,
    *,
    expected_base_revision: int | None = None,
) -> None:
    """使用预览携带的完整 PlanningInput 校验版本、来源、候选与路线引用。"""
    issues: list[ReferenceIssue] = []
    planning_input = preview.planning_input
    draft, facts = preview.draft, preview.facts
    if expected_base_revision is not None and draft.base_revision != expected_base_revision:
        issues.append(ReferenceIssue("stale_base_revision", "draft.base_revision", "草稿基于过期版本"))
    if preview.validation.plan_id != draft.plan_id or preview.validation.based_on_revision != draft.revision:
        issues.append(ReferenceIssue("validation_version_mismatch", "validation", "校验报告与当前草稿版本不一致"))
    if preview.calculation.based_on_revision != draft.revision:
        issues.append(ReferenceIssue("calculation_version_mismatch", "calculation", "路线计算与当前草稿版本不一致"))

    source_by_id = _source_index(facts, issues)
    place_by_id = {place.fact_id: place for place in facts.places}
    place_by_poi = {place.poi_id: place for place in facts.places}
    weather_by_id = {weather.fact_id: weather for weather in facts.weather}
    route_by_id = {route.fact_id: route for route in facts.routes}
    candidate_by_id = _index_candidates(preview.candidates, issues)
    for candidate in preview.candidates:
        _validate_candidate(candidate, place_by_id, f"candidate:{candidate.candidate_id}", issues)

    day_by_id = {day.day_id: day for day in draft.days}
    day_constraints = {day.day_id: day for day in planning_input.days}
    if draft.request_id != planning_input.request.request_id:
        issues.append(ReferenceIssue("request_mismatch", "draft.request_id", "草稿必须引用原始需求"))
    if draft.rule_version != planning_input.rules.rule_version:
        issues.append(ReferenceIssue("rule_version_mismatch", "draft.rule_version", "草稿必须引用本次规则版本"))
    if {day.day_id for day in draft.days} != set(day_constraints):
        issues.append(ReferenceIssue("day_set_mismatch", "draft.days", "草稿日期集合必须覆盖 PlanningInput 日期"))
    if {day.day_date for day in draft.days} != {day.day_date for day in planning_input.days}:
        issues.append(ReferenceIssue("day_date_set_mismatch", "draft.days", "草稿日期必须覆盖 PlanningInput 日期"))

    proposal_area_ids = {area.area_hint_id for area in draft.area_hints}
    known_requirement_ids = {requirement.requirement_id for requirement in planning_input.requirements.hard_requirements}
    activity_by_id: dict[str, tuple[str, object]] = {}
    slot_activity_ids: dict[str, set[str]] = {}
    unfilled_ids: set[tuple[str, str]] = set()

    for day_index, day in enumerate(draft.days):
        day_path = f"draft.days[{day_index}]"
        constraint = day_constraints.get(day.day_id)
        if constraint and day.day_date != constraint.day_date:
            issues.append(ReferenceIssue("day_date_mismatch", f"{day_path}.day_date", "草稿日期与规则输入不一致"))
        slots = {slot.slot_id: slot for slot in constraint.slots} if constraint else {}
        day_activity_ids = {activity.activity_id for activity in day.activities}
        for activity_index, activity in enumerate(day.activities):
            activity_path = f"{day_path}.activities[{activity_index}]"
            activity_by_id[activity.activity_id] = (day.day_id, activity)
            slot = slots.get(activity.slot_id)
            if slot is None:
                issues.append(ReferenceIssue("dangling_slot", f"{activity_path}.slot_id", "活动时段不存在"))
            elif slot and slot.eligibility == "skipped":
                issues.append(ReferenceIssue("activity_in_skipped_slot", f"{activity_path}.slot_id", "skipped 时段不能安排活动"))
            if activity.kind != "free_time":
                candidate = candidate_by_id.get(activity.candidate_ref or "")
                if candidate is None:
                    issues.append(ReferenceIssue("dangling_candidate", f"{activity_path}.candidate_ref", "活动候选不存在"))
                else:
                    expected_category = "sightseeing" if activity.kind == "sightseeing" else "meal"
                    if candidate.category != expected_category:
                        issues.append(ReferenceIssue("candidate_category_mismatch", f"{activity_path}.candidate_ref", "候选类别与活动类型不一致"))
                    if candidate.poi_id != activity.poi_id or candidate.place_fact_id != activity.place_fact_id:
                        issues.append(ReferenceIssue("candidate_activity_mismatch", activity_path, "活动身份与候选不一致"))
                    if candidate.slot_ids and activity.slot_id not in candidate.slot_ids:
                        issues.append(ReferenceIssue("candidate_slot_mismatch", f"{activity_path}.slot_id", "候选不适用于该时段"))
                fact = place_by_id.get(activity.place_fact_id or "")
                if fact is None:
                    issues.append(ReferenceIssue("dangling_place_fact", f"{activity_path}.place_fact_id", "活动地点事实不存在"))
                elif activity.poi_id != fact.poi_id:
                    issues.append(ReferenceIssue("activity_poi_mismatch", activity_path, "活动 poi_id 与地点事实不一致"))
                elif fact.location is None:
                    issues.append(ReferenceIssue("activity_location_missing", f"{activity_path}.place_fact_id", "缺少坐标的地点不能作为正式活动"))
            unknown_requirements = set(activity.requirement_ids) - known_requirement_ids
            if unknown_requirements:
                issues.append(ReferenceIssue("dangling_requirement", f"{activity_path}.requirement_ids", "活动引用了未知硬性需求"))
            if activity.duration_source == "default":
                expected_duration = (
                    planning_input.rules.sightseeing_default_s
                    if activity.kind == "sightseeing" else planning_input.rules.meal_default_s
                )
                if activity.duration_s != expected_duration:
                    issues.append(ReferenceIssue("default_duration_mismatch", f"{activity_path}.duration_s", "默认时长与规则版本不一致"))
            slot_activity_ids.setdefault(activity.slot_id, set()).add(activity.activity_id)
        for slot_id in day.unfilled_slot_ids:
            unfilled_ids.add((day.day_id, slot_id))
            slot = slots.get(slot_id)
            if slot is None or slot.eligibility == "skipped":
                issues.append(ReferenceIssue("invalid_unfilled_slot", f"{day_path}.unfilled_slot_ids", "缺项必须引用未跳过的有效时段"))
        if day.weather_fact_id:
            weather = weather_by_id.get(day.weather_fact_id)
            if weather is None:
                issues.append(ReferenceIssue("dangling_weather_fact", f"{day_path}.weather_fact_id", "天气事实不存在"))
            elif day.day_date not in {cast.forecast_date for cast in weather.forecasts}:
                # Missing forecast coverage is an unknown fact, not an invalid preview.
                if day.day_date not in weather.missing_dates:
                    issues.append(ReferenceIssue("unrecorded_weather_gap", f"{day_path}.weather_fact_id", "缺少该日预报且未记录为未覆盖"))
        if day.lodging:
            lodging = day.lodging
            if lodging.place_fact_id:
                place = place_by_id.get(lodging.place_fact_id)
                if place is None:
                    issues.append(ReferenceIssue("dangling_lodging_fact", f"{day_path}.lodging.place_fact_id", "住宿事实不存在"))
                elif place.poi_id != lodging.poi_id:
                    issues.append(ReferenceIssue("lodging_poi_mismatch", f"{day_path}.lodging", "住宿 POI 与地点事实不一致"))
                elif place.location is None:
                    issues.append(ReferenceIssue("lodging_location_missing", f"{day_path}.lodging.place_fact_id", "缺少坐标的地点不能作为已核实住宿"))
            if lodging.candidate_ref:
                candidate = candidate_by_id.get(lodging.candidate_ref)
                if candidate is None or candidate.category != "lodging":
                    issues.append(ReferenceIssue("invalid_lodging_candidate", f"{day_path}.lodging.candidate_ref", "住宿候选不存在或类别不符"))
                elif candidate.poi_id != lodging.poi_id or candidate.place_fact_id != lodging.place_fact_id:
                    issues.append(ReferenceIssue("lodging_candidate_mismatch", f"{day_path}.lodging", "住宿信息与候选不一致"))
            if lodging.area_hint_id and lodging.area_hint_id not in proposal_area_ids:
                issues.append(ReferenceIssue("dangling_lodging_area", f"{day_path}.lodging.area_hint_id", "住宿区域不存在"))

    issue_by_id = {issue.issue_id: issue for issue in preview.validation.issues}
    if len(issue_by_id) != len(preview.validation.issues):
        issues.append(ReferenceIssue("duplicate_issue_id", "validation.issues", "issue_id 必须唯一"))
    for day in draft.days:
            constraint = day_constraints.get(day.day_id)
            slots = {slot.slot_id: slot for slot in constraint.slots} if constraint else {}
            issue_slots = {
                issue.target_id for issue in preview.validation.issues
                if issue.day_id == day.day_id and issue.scope == "slot"
            }
            activities_by_slot = slot_activity_ids
            for slot in slots.values():
                has_activity = bool(activities_by_slot.get(slot.slot_id))
                is_unfilled = (day.day_id, slot.slot_id) in unfilled_ids
                has_issue = slot.slot_id in issue_slots
                if slot.eligibility == "skipped" and (has_activity or is_unfilled):
                    issues.append(ReferenceIssue("skipped_slot_content", f"day:{day.day_id}.slot:{slot.slot_id}", "skipped 时段不能有活动或缺项"))
                if slot.eligibility != "skipped" and not has_activity and not (is_unfilled and has_issue):
                    issues.append(ReferenceIssue("missing_slot_not_reported", f"day:{day.day_id}.slot:{slot.slot_id}", "无活动时必须同时记录缺项和对应 Issue"))
    for issue_index, issue in enumerate(preview.validation.issues):
            path = f"validation.issues[{issue_index}]"
            if issue.based_on_revision != preview.validation.based_on_revision:
                issues.append(ReferenceIssue("issue_version_mismatch", f"{path}.based_on_revision", "问题来源版本必须与 ValidationReport 一致"))
            if issue.category == "unknown_info" and issue.triggers_repair:
                issues.append(ReferenceIssue("unknown_info_triggers_repair", f"{path}.triggers_repair", "未知信息不能触发自动修复"))
            if issue.scope != "plan" and issue.day_id not in day_by_id:
                issues.append(ReferenceIssue("dangling_issue_day", f"{path}.day_id", "日期级问题必须引用有效日期"))
            if issue.scope in {"slot", "activity", "leg"} and not issue.target_id:
                issues.append(ReferenceIssue("missing_issue_target", f"{path}.target_id", "局部问题必须定位目标"))
            issue_constraint = day_constraints.get(issue.day_id or "")
            if issue.scope == "slot" and issue_constraint and issue.target_id not in {slot.slot_id for slot in issue_constraint.slots}:
                issues.append(ReferenceIssue("dangling_issue_slot", f"{path}.target_id", "问题时段不存在"))
            if issue.scope == "slot" and issue_constraint and issue.category == "missing_arrangement":
                target_slot = next((slot for slot in issue_constraint.slots if slot.slot_id == issue.target_id), None)
                if target_slot is not None and target_slot.eligibility == "skipped":
                    issues.append(ReferenceIssue("skipped_slot_missing_issue", f"{path}.target_id", "skipped 时段不能产生缺项"))
            if issue.scope == "activity" and issue.target_id not in activity_by_id:
                issues.append(ReferenceIssue("dangling_issue_activity", f"{path}.target_id", "问题活动不存在"))
            elif issue.scope == "activity" and issue.target_id in activity_by_id and activity_by_id[issue.target_id][0] != issue.day_id:
                issues.append(ReferenceIssue("issue_activity_day_mismatch", f"{path}.target_id", "问题活动必须属于定位日期"))
            if issue.scope == "leg" and issue.target_id not in {leg.leg_id for leg in draft.legs}:
                issues.append(ReferenceIssue("dangling_issue_leg", f"{path}.target_id", "问题路段不存在"))
            elif issue.scope == "leg" and issue.target_id in {leg.leg_id for leg in draft.legs}:
                target_leg = next(leg for leg in draft.legs if leg.leg_id == issue.target_id)
                if target_leg.day_id != issue.day_id:
                    issues.append(ReferenceIssue("issue_leg_day_mismatch", f"{path}.target_id", "问题路段必须属于定位日期"))
            for evidence_id in issue.evidence_refs:
                valid_refs = set(place_by_id) | set(weather_by_id) | set(route_by_id) | set(activity_by_id) | {leg.leg_id for leg in draft.legs} | {slot.slot_id for day in day_constraints.values() for slot in day.slots}
                if evidence_id not in valid_refs and evidence_id not in source_by_id:
                    issues.append(ReferenceIssue("dangling_issue_evidence", f"{path}.evidence_refs", f"证据 {evidence_id} 不存在"))
    if len({item.day_id for item in preview.validation.days}) != len(preview.validation.days) or {item.day_id for item in preview.validation.days} != set(day_by_id):
        issues.append(ReferenceIssue("day_validation_set_mismatch", "validation.days", "日期校验必须与行程日期一一对应"))
    for day_result in preview.validation.days:
            if day_result.day_id not in day_by_id:
                issues.append(ReferenceIssue("dangling_day_validation", "validation.days", "日期校验引用不存在"))
            if any(issue_id not in issue_by_id for issue_id in day_result.issue_ids):
                issues.append(ReferenceIssue("dangling_day_issue", f"validation.days.{day_result.day_id}.issue_ids", "日期校验引用了不存在的问题"))
    if len({item.requirement_id for item in preview.validation.requirements}) != len(preview.validation.requirements) or {item.requirement_id for item in preview.validation.requirements} != known_requirement_ids:
        issues.append(ReferenceIssue("requirement_result_set_mismatch", "validation.requirements", "硬性需求校验必须与需求列表一一对应"))
    for requirement in preview.validation.requirements:
            if requirement.requirement_id not in (known_requirement_ids or set()):
                issues.append(ReferenceIssue("dangling_requirement_result", "validation.requirements", "校验结果引用了未知需求"))
            if any(activity_id not in activity_by_id for activity_id in requirement.activity_ids):
                issues.append(ReferenceIssue("dangling_requirement_activity", "validation.requirements", "需求结果引用了不存在的活动"))
            if any(fact_id not in place_by_id for fact_id in requirement.place_fact_ids):
                issues.append(ReferenceIssue("dangling_requirement_fact", "validation.requirements", "需求结果引用了不存在的地点事实"))
            if requirement.status == "satisfied":
                requirement_kind = next((
                    item.kind for item in planning_input.requirements.hard_requirements
                    if item.requirement_id == requirement.requirement_id
                ), None)
                if requirement_kind != "must_avoid":
                    if not requirement.activity_ids or not requirement.place_fact_ids:
                        issues.append(ReferenceIssue("unbound_satisfied_requirement", f"validation.requirements.{requirement.requirement_id}", "满足的硬性需求必须绑定实际活动和地点事实"))
                    matched = [activity_by_id.get(activity_id) for activity_id in requirement.activity_ids]
                    if not any(
                        pair is not None
                        and getattr(pair[1], "place_fact_id", None) in requirement.place_fact_ids
                        and requirement.requirement_id in getattr(pair[1], "requirement_ids", ())
                        for pair in matched
                    ):
                        issues.append(ReferenceIssue("requirement_activity_fact_mismatch", f"validation.requirements.{requirement.requirement_id}", "满足的硬性需求必须通过同一活动绑定其地点事实"))

    if len({item.day_id for item in preview.validation.repair_states}) != len(preview.validation.repair_states) or {item.day_id for item in preview.validation.repair_states} != set(day_by_id):
        issues.append(ReferenceIssue("repair_state_set_mismatch", "validation.repair_states", "每个行程日期必须有且只有一个修复状态"))
    issue_ids = set(issue_by_id)
    for state in preview.validation.repair_states:
        if state.status == "active" and state.active_round_id is None:
            issues.append(ReferenceIssue("invalid_active_repair_state", f"validation.repair_states.{state.day_id}", "active 状态必须引用当前修复轮次"))
        if state.completed_rounds > planning_input.rules.max_repair_rounds_per_day:
            issues.append(ReferenceIssue("repair_round_limit_exceeded", f"validation.repair_states.{state.day_id}", "修复轮次超过 RuleSet 上限"))

    leg_by_id = {leg.leg_id: leg for leg in draft.legs}
    for leg_index, leg in enumerate(draft.legs):
        path = f"draft.legs[{leg_index}]"
        day = day_by_id.get(leg.day_id)
        if day is None:
            issues.append(ReferenceIssue("dangling_leg_day", f"{path}.day_id", "路段日期不存在"))
            continue
        for end_name, endpoint in (("from_endpoint", leg.from_endpoint), ("to_endpoint", leg.to_endpoint)):
            endpoint_path = f"{path}.{end_name}"
            if endpoint.kind == "activity":
                pair = activity_by_id.get(endpoint.reference_id)
                if pair is None or pair[0] != leg.day_id:
                    issues.append(ReferenceIssue("cross_day_endpoint", f"{endpoint_path}.reference_id", "路段活动端点必须属于该路段日期"))
                elif getattr(pair[1], "poi_id") != endpoint.poi_id:
                    issues.append(ReferenceIssue("endpoint_poi_mismatch", endpoint_path, "路段端点 POI 与活动不一致"))
            elif endpoint.kind == "lodging":
                lodging = day.lodging
                if lodging is None or endpoint.reference_id != f"lodging:{leg.day_id}" or lodging.poi_id != endpoint.poi_id:
                    issues.append(ReferenceIssue("invalid_lodging_endpoint", endpoint_path, "住宿端点必须引用该日住宿"))
            elif endpoint.kind in {"arrival", "departure"}:
                if endpoint.kind == "arrival" and planning_input and not planning_input.request.arrival_place_query:
                    issues.append(ReferenceIssue("unknown_arrival_endpoint", endpoint_path, "未提供到达地点时不能构造路线端点"))
                if endpoint.kind == "departure" and planning_input and not planning_input.request.departure_place_query:
                    issues.append(ReferenceIssue("unknown_departure_endpoint", endpoint_path, "未提供离开地点时不能构造路线端点"))
            point = place_by_poi.get(endpoint.poi_id)
            if point is None:
                issues.append(ReferenceIssue("dangling_leg_place", endpoint_path, f"路线端点 POI {endpoint.poi_id} 无地点事实"))

        for mode_index, mode_result in enumerate(leg.mode_results):
            mode_path = f"{path}.mode_results[{mode_index}]"
            if mode_result.status == "pending":
                continue
            route_fact = route_by_id.get(mode_result.route_fact_id or "")
            if route_fact is None:
                issues.append(ReferenceIssue("dangling_route_fact", f"{mode_path}.route_fact_id", "交通方式引用的路线事实不存在"))
                continue
            if route_fact.mode != mode_result.mode:
                issues.append(ReferenceIssue("route_mode_mismatch", mode_path, "路线事实方式与交通选项不一致"))
            if route_fact.status != mode_result.status:
                issues.append(ReferenceIssue("route_status_mismatch", mode_path, "路线事实状态与交通选项不一致"))
            if route_fact.origin_poi_id != leg.from_endpoint.poi_id or route_fact.destination_poi_id != leg.to_endpoint.poi_id:
                issues.append(ReferenceIssue("route_direction_mismatch", mode_path, "路线事实起终点方向与当前路段不一致"))
            origin_fact = place_by_poi.get(route_fact.origin_poi_id)
            destination_fact = place_by_poi.get(route_fact.destination_poi_id)
            if origin_fact and origin_fact.location != route_fact.origin:
                issues.append(ReferenceIssue("route_origin_coordinate_mismatch", mode_path, "路线起点坐标与地点事实不一致"))
            if destination_fact and destination_fact.location != route_fact.destination:
                issues.append(ReferenceIssue("route_destination_coordinate_mismatch", mode_path, "路线终点坐标与地点事实不一致"))
            options = {option.option_id for option in route_fact.options}
            if set(mode_result.option_ids) - options:
                issues.append(ReferenceIssue("dangling_route_option", f"{mode_path}.option_ids", "路线方案不属于该 RouteFact"))
            if mode_result.mode == "transit":
                if route_fact.requested_departure_at != leg.requested_departure_at:
                    issues.append(ReferenceIssue("transit_time_mismatch", mode_path, "公交事实出发时间与该路段不一致"))
                params = source_by_id.get(route_fact.source.tool_call_id)
                if route_fact.requested_departure_at is None:
                    if params and ("date" in params.effective_params or "time" in params.effective_params):
                        issues.append(ReferenceIssue("unbound_transit_time", mode_path, "路线发送了日期/时刻，但 RouteFact 未绑定请求时刻"))
                else:
                    local = route_fact.requested_departure_at.astimezone(ZoneInfo(planning_input.request.timezone))
                    has_date = bool(params and params.effective_params.get("date"))
                    has_time = bool(params and params.effective_params.get("time"))
                    if route_fact.time_verification == "not_sent":
                        if has_date or has_time:
                            issues.append(ReferenceIssue("transit_time_evidence_mismatch", mode_path, "来源参数含日期或时刻，但 RouteFact 标记为未发送"))
                        if route_fact.status == "available":
                            issues.append(ReferenceIssue("transit_time_not_sent", mode_path, "可用公交方案必须发送请求日期和时刻"))
                    else:
                        if params and params.effective_params.get("date") != local.date().isoformat():
                            issues.append(ReferenceIssue("transit_date_not_sent", mode_path, "来源参数没有发送对应出发日期"))
                        if params and params.effective_params.get("time") != local.strftime("%H:%M"):
                            issues.append(ReferenceIssue("transit_time_not_sent", mode_path, "来源参数没有发送对应出发时刻"))
                    if route_fact.time_verification == "provider_echoed" and route_fact.provider_departure_at != route_fact.requested_departure_at:
                        issues.append(ReferenceIssue("provider_time_mismatch", mode_path, "供应商回显时间与请求不同"))
                sent_strategy = params.effective_params.get("strategy") if params else None
                if route_fact.requested_strategy is None and sent_strategy is not None:
                    issues.append(ReferenceIssue("unbound_transit_strategy", mode_path, "路线来源发送了策略，但 RouteFact 未记录请求策略"))
                elif route_fact.requested_strategy is not None and sent_strategy is not None and str(sent_strategy) != str(route_fact.requested_strategy):
                    issues.append(ReferenceIssue("transit_strategy_mismatch", mode_path, "路线来源策略与 RouteFact 请求策略不一致"))
                elif route_fact.requested_strategy is not None and route_fact.status == "available" and sent_strategy is None:
                    issues.append(ReferenceIssue("transit_strategy_not_sent", mode_path, "有效公交方案必须发送 RouteFact 声明的策略"))
            elif route_fact.requested_departure_at is not None:
                issues.append(ReferenceIssue("non_transit_departure_time", mode_path, "非公共交通路线不能绑定公交出发时刻"))
        if leg.selected_mode is not None:
            mode_result = next((item for item in leg.mode_results if item.mode == leg.selected_mode), None)
            if mode_result is None or mode_result.status != "available" or leg.selected_option_id not in mode_result.option_ids:
                issues.append(ReferenceIssue("invalid_selected_route", f"{path}.selected_option_id", "选中方案不属于当前路段的有效方式结果"))

    if preview.day_summaries:
        summary_ids = [summary.day_id for summary in preview.day_summaries]
        if set(summary_ids) != set(day_by_id) or len(summary_ids) != len(set(summary_ids)):
            issues.append(ReferenceIssue("day_summary_set_mismatch", "day_summaries", "每日摘要必须与行程日期一一对应"))
        for summary in preview.day_summaries:
            if any(leg_id not in leg_by_id or leg_by_id[leg_id].day_id != summary.day_id for leg_id in summary.unknown_leg_ids):
                issues.append(ReferenceIssue("dangling_summary_leg", f"day_summaries.{summary.day_id}.unknown_leg_ids", "摘要引用了其他日期或不存在的路段"))
    _raise_if_any(issues)


def validate_edit_command(
    command: EditCommand,
    draft: PlanDraft,
    *,
    candidates: tuple[CandidateRef, ...] = (),
    planning_input: PlanningInput | None = None,
) -> None:
    """校验编辑命令基于当前版本；重排必须是该日活动 ID 的完整排列。"""
    issues: list[ReferenceIssue] = []
    if command.plan_id != draft.plan_id:
        issues.append(ReferenceIssue("plan_mismatch", "command.plan_id", "编辑命令引用了其他行程"))
    if command.base_revision != draft.revision:
        issues.append(ReferenceIssue("stale_edit_revision", "command.base_revision", "编辑命令基于旧版本"))
    day = next((item for item in draft.days if item.day_id == command.day_id), None)
    if day is None:
        issues.append(ReferenceIssue("dangling_edit_day", "command.day_id", "编辑日期不存在"))
        _raise_if_any(issues)
        return
    activity_ids = [activity.activity_id for activity in day.activities]
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    slots = {}
    if planning_input:
        constraint = next((item for item in planning_input.days if item.day_id == command.day_id), None)
        slots = {slot.slot_id: slot for slot in constraint.slots} if constraint else {}
    for index, operation in enumerate(command.operations):
        path = f"command.operations[{index}]"
        if operation.day_id != command.day_id:
            issues.append(ReferenceIssue("edit_operation_day_mismatch", f"{path}.day_id", "操作日期与命令日期不一致"))
        if isinstance(operation, ReorderActivities):
            if len(operation.activity_ids) != len(set(operation.activity_ids)):
                issues.append(ReferenceIssue("duplicate_reorder_id", f"{path}.activity_ids", "重排列表不能重复 activity_id"))
            if set(operation.activity_ids) != set(activity_ids) or len(operation.activity_ids) != len(activity_ids):
                issues.append(ReferenceIssue("reorder_must_be_permutation", f"{path}.activity_ids", "重排必须完整保留该日现有活动 ID，不能增删"))
        elif isinstance(operation, DeleteActivity):
            if operation.activity_id not in activity_ids:
                issues.append(ReferenceIssue("dangling_removed_activity", f"{path}.activity_id", "要删除的活动不属于该日"))
        elif isinstance(operation, AddActivity):
            candidate = candidate_by_id.get(operation.candidate_ref)
            if candidate is None:
                issues.append(ReferenceIssue("dangling_added_candidate", f"{path}.candidate_ref", "新增活动候选不存在"))
            if planning_input and (operation.slot_id not in slots or slots[operation.slot_id].eligibility == "skipped"):
                issues.append(ReferenceIssue("invalid_added_slot", f"{path}.slot_id", "新增活动时段无效或已跳过"))
        elif isinstance(operation, SelectLegMode):
            leg = next((item for item in draft.legs if item.leg_id == operation.leg_id), None)
            if leg is None or leg.day_id != command.day_id:
                issues.append(ReferenceIssue("dangling_selected_leg", f"{path}.leg_id", "所选路段不属于该日"))
            else:
                result = next((item for item in leg.mode_results if item.mode == operation.mode), None)
                if result is None or result.status != "available" or operation.option_id not in result.option_ids:
                    issues.append(ReferenceIssue("invalid_selected_option", f"{path}.option_id", "交通方案不属于该路段的有效方式结果"))
    _raise_if_any(issues)


def validate_day_revision(
    revision: DayRevision,
    draft: PlanDraft,
    report: ValidationReport,
    planning_input: PlanningInput,
    candidates: tuple[CandidateRef, ...],
    facts: FactStore,
) -> None:
    """校验局部修订只更新目标日期，并保留明确指定的既有活动。"""
    issues: list[ReferenceIssue] = []
    if report.plan_id != draft.plan_id:
        issues.append(ReferenceIssue("revision_plan_mismatch", "report.plan_id", "校验报告必须属于当前行程"))
    if report.based_on_revision != draft.revision or revision.base_revision != draft.revision:
        issues.append(ReferenceIssue("revision_base_mismatch", "revision.base_revision", "日期修订必须基于当前行程版本"))
    if revision.based_on_report_revision != report.based_on_revision:
        issues.append(ReferenceIssue("revision_report_mismatch", "revision.based_on_report_revision", "日期修订必须引用当前校验报告版本"))
    old_day = next((day for day in draft.days if day.day_id == revision.day_id), None)
    constraint = next((day for day in planning_input.days if day.day_id == revision.day_id), None)
    if old_day is None or constraint is None:
        issues.append(ReferenceIssue("revision_day_missing", "revision.day_id", "修订日期必须存在于当前行程和 PlanningInput"))
        _raise_if_any(issues)
        return
    if revision.replacement_day.day_date != constraint.day_date or old_day.day_date != constraint.day_date:
        issues.append(ReferenceIssue("revision_date_mismatch", "revision.replacement_day.day_date", "修订日期必须与 PlanningInput 一致"))
    old_activity_ids = {activity.activity_id for activity in old_day.activities}
    replacement_existing_ids = {
        activity.existing_activity_id for activity in revision.replacement_day.activities
        if activity.existing_activity_id is not None
    }
    if not set(revision.preserve_activity_ids).issubset(old_activity_ids):
        issues.append(ReferenceIssue("unknown_preserved_activity", "revision.preserve_activity_ids", "保留活动必须来自当前目标日期"))
    if not set(revision.preserve_activity_ids).issubset(replacement_existing_ids):
        issues.append(ReferenceIssue("preserved_activity_missing", "revision.replacement_day.activities", "修订提案没有保留指定活动"))
    if replacement_existing_ids - old_activity_ids:
        issues.append(ReferenceIssue("unknown_existing_activity", "revision.replacement_day.activities", "existing_activity_id 必须属于当前目标日期"))
    issue_by_id = {issue.issue_id: issue for issue in report.issues}
    if not revision.source_issue_ids:
        issues.append(ReferenceIssue("revision_issue_missing", "revision.source_issue_ids", "日期修订必须关联触发本次修复的问题"))
    for issue_id in revision.source_issue_ids:
        source_issue = issue_by_id.get(issue_id)
        if source_issue is None or source_issue.day_id != revision.day_id or not source_issue.triggers_repair or source_issue.category == "unknown_info":
            issues.append(ReferenceIssue("invalid_revision_issue", "revision.source_issue_ids", "修订只能引用目标日期上触发修复的已知问题"))
    slots = {slot.slot_id: slot for slot in constraint.slots}
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    place_by_id = {place.fact_id: place for place in facts.places}
    requirement_ids = {item.requirement_id for item in planning_input.requirements.hard_requirements}
    activity_slots: set[str] = set()
    for index, activity in enumerate(revision.replacement_day.activities):
        path = f"revision.replacement_day.activities[{index}]"
        activity_slots.add(activity.slot_id)
        slot = slots.get(activity.slot_id)
        if slot is None or slot.eligibility == "skipped":
            issues.append(ReferenceIssue("invalid_revision_slot", f"{path}.slot_id", "修订活动必须使用未跳过的有效时段"))
        if set(activity.requirement_ids) - requirement_ids:
            issues.append(ReferenceIssue("dangling_revision_requirement", f"{path}.requirement_ids", "修订活动引用了未知硬性需求"))
        if activity.existing_activity_id:
            old_activity = next((item for item in old_day.activities if item.activity_id == activity.existing_activity_id), None)
            if old_activity and (activity.candidate_ref != old_activity.candidate_ref or activity.kind != old_activity.kind):
                issues.append(ReferenceIssue("preserved_activity_identity_changed", path, "既有活动保留时不能改变其地点身份或活动类型"))
        if activity.kind != "free_time":
            candidate = candidate_by_id.get(activity.candidate_ref or "")
            if candidate is None:
                issues.append(ReferenceIssue("dangling_revision_candidate", f"{path}.candidate_ref", "修订活动必须引用已核实候选"))
            else:
                expected = "sightseeing" if activity.kind == "sightseeing" else "meal"
                fact = place_by_id.get(candidate.place_fact_id)
                if candidate.category != expected or fact is None or fact.poi_id != candidate.poi_id or fact.location is None:
                    issues.append(ReferenceIssue("invalid_revision_candidate", f"{path}.candidate_ref", "修订候选类别或地点事实无效"))
        if activity.existing_activity_id is not None and activity.existing_activity_id not in old_activity_ids:
            issues.append(ReferenceIssue("dangling_existing_activity", f"{path}.existing_activity_id", "修订引用了不存在的既有活动"))
    unfilled = set(revision.replacement_day.unfilled_slot_ids)
    if len(unfilled) != len(revision.replacement_day.unfilled_slot_ids):
        issues.append(ReferenceIssue("duplicate_revision_unfilled_slot", "revision.replacement_day.unfilled_slot_ids", "缺项时段不能重复"))
    for slot_id in unfilled:
        slot = slots.get(slot_id)
        if slot is None or slot.eligibility == "skipped":
            issues.append(ReferenceIssue("invalid_revision_unfilled_slot", "revision.replacement_day.unfilled_slot_ids", "缺项必须引用未跳过的有效时段"))
    for slot in constraint.slots:
        if slot.eligibility == "skipped" and (slot.slot_id in activity_slots or slot.slot_id in unfilled):
            issues.append(ReferenceIssue("revision_skipped_slot_content", f"revision.replacement_day.{slot.slot_id}", "skipped 时段不能包含活动或缺项"))
        if slot.eligibility != "skipped" and slot.slot_id not in activity_slots and slot.slot_id not in unfilled:
            issues.append(ReferenceIssue("revision_slot_unaccounted", f"revision.replacement_day.{slot.slot_id}", "修订必须安排活动或显式记录缺项"))
    if revision.replacement_day.lodging:
        lodging = revision.replacement_day.lodging
        if lodging.candidate_ref:
            candidate = candidate_by_id.get(lodging.candidate_ref)
            if candidate is None or candidate.category != "lodging":
                issues.append(ReferenceIssue("invalid_revision_lodging", "revision.replacement_day.lodging.candidate_ref", "修订住宿必须引用已核实酒店候选"))
    _raise_if_any(issues)
