"""v4 模型提案、后端草稿、校验报告、预览和基本编辑命令。"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, Field, PositiveInt, model_validator

from .base import ContractModel
from .facts import FactStore, MapCoordinate
from .input import PlanningInput
from .tasks import CandidateRef


class ProposalActivity(ContractModel):
    proposal_ref: str = Field(min_length=1)
    existing_activity_id: str | None = None
    candidate_ref: str | None = None
    slot_id: str = Field(min_length=1)
    kind: Literal["sightseeing", "meal", "free_time"]
    duration_s: int | None = Field(default=None, ge=0)
    requirement_ids: tuple[str, ...] = ()
    recommendation_reason: str = ""

    @model_validator(mode="after")
    def validate_candidate_selection(self):
        if self.kind == "free_time" and self.candidate_ref is not None:
            raise ValueError("free_time 不应引用 POI 候选")
        if self.kind != "free_time" and self.candidate_ref is None:
            raise ValueError("地点活动必须引用候选")
        return self


class AreaHint(ContractModel):
    area_hint_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    anchor_poi_ids: tuple[str, ...] = ()
    center: MapCoordinate | None = None
    basis_fact_ids: tuple[str, ...] = ()
    basis: str = "model_recommendation"


class LodgingProposal(ContractModel):
    candidate_ref: str | None = None
    area_hint_id: str | None = None
    recommendation_reason: str = ""


class ProposalDay(ContractModel):
    day_id: str = Field(min_length=1)
    day_date: date
    theme: str = ""
    activities: tuple[ProposalActivity, ...] = ()
    unfilled_slot_ids: tuple[str, ...] = ()
    lodging: LodgingProposal | None = None


class PlanProposal(ContractModel):
    """模型只能选择候选引用，不能自行提交地点事实、价格或路线。"""
    schema_version: Literal[4] = 4
    request_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    days: tuple[ProposalDay, ...] = Field(min_length=1)
    area_hints: tuple[AreaHint, ...] = ()
    missing_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_proposal_ids(self):
        refs = [activity.proposal_ref for day in self.days for activity in day.activities]
        if len(refs) != len(set(refs)):
            raise ValueError("proposal_ref 必须唯一")
        day_ids = [day.day_id for day in self.days]
        if len(day_ids) != len(set(day_ids)):
            raise ValueError("proposal 中 day_id 必须唯一")
        return self


class DraftActivity(ContractModel):
    activity_id: str = Field(min_length=1)
    candidate_ref: str | None = None
    slot_id: str = Field(min_length=1)
    kind: Literal["sightseeing", "meal", "free_time"]
    poi_id: str | None = None
    place_fact_id: str | None = None
    duration_s: int = Field(ge=0)
    duration_source: Literal["model", "default", "user"]
    duration_reason: str | None = None
    requirement_ids: tuple[str, ...] = ()
    recommendation_reason: str = ""

    @model_validator(mode="after")
    def validate_activity_identity(self):
        references = (self.candidate_ref, self.poi_id, self.place_fact_id)
        if self.kind == "free_time" and any(reference is not None for reference in references):
            raise ValueError("free_time 不应伪造 POI 候选或事实")
        if self.kind != "free_time" and any(reference is None for reference in references):
            raise ValueError("地点活动必须关联候选、poi_id 和 place_fact_id")
        return self


class DraftDay(ContractModel):
    day_id: str = Field(min_length=1)
    day_date: date
    theme: str = ""
    activities: tuple[DraftActivity, ...] = ()
    unfilled_slot_ids: tuple[str, ...] = ()
    weather_fact_id: str | None = None
    weather_advice: str | None = None
    lodging: "LodgingRef | None" = None


class LodgingRef(ContractModel):
    candidate_ref: str | None = None
    poi_id: str | None = None
    place_fact_id: str | None = None
    area_hint_id: str | None = None
    selection_source: Literal["user", "recommended", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_identity(self):
        if (self.poi_id is None) != (self.place_fact_id is None):
            raise ValueError("住宿 poi_id 与 place_fact_id 必须同时存在或同时为空")
        return self


class LegEndpoint(ContractModel):
    kind: Literal["activity", "lodging", "arrival", "departure"]
    reference_id: str = Field(min_length=1)
    poi_id: str = Field(min_length=1)


class RouteModeResult(ContractModel):
    mode: Literal["transit", "bicycling", "walking"]
    status: Literal["pending", "available", "empty", "error"]
    route_fact_id: str | None = None
    option_ids: tuple[str, ...] = ()
    error_category: Literal[
        "invalid_request", "auth", "rate_limit", "timeout", "network", "provider_error", "invalid_response"
    ] | None = None

    @model_validator(mode="after")
    def validate_mode_result(self):
        if self.status == "available" and (not self.route_fact_id or not self.option_ids):
            raise ValueError("available 方式必须关联路线事实和方案")
        if self.status != "available" and self.option_ids:
            raise ValueError("非 available 方式不能引用有效方案")
        if self.status == "error" and not self.error_category:
            raise ValueError("error 方式必须记录错误分类")
        if self.status == "pending" and (self.route_fact_id is not None or self.error_category is not None):
            raise ValueError("pending 方式不能引用已结束的路线事实或错误")
        if self.status != "error" and self.error_category is not None:
            raise ValueError("只有 error 方式可以设置 error_category")
        return self


class RouteLeg(ContractModel):
    leg_id: str = Field(min_length=1)
    plan_revision: PositiveInt
    day_id: str = Field(min_length=1)
    from_endpoint: LegEndpoint
    to_endpoint: LegEndpoint
    requested_departure_at: AwareDatetime | None = None
    mode_results: tuple[RouteModeResult, ...] = ()
    selected_mode: Literal["transit", "bicycling", "walking"] | None = None
    selected_option_id: str | None = None
    selection_source: Literal["backend_fastest_available", "user", "same_place"] | None = None

    @model_validator(mode="after")
    def validate_route_selection(self):
        modes = [result.mode for result in self.mode_results]
        if len(modes) != len(set(modes)):
            raise ValueError("每个路段的交通方式结果必须唯一")
        if (self.selected_mode is None) != (self.selected_option_id is None):
            raise ValueError("选中方式与方案必须同时存在或同时为空")
        if self.selected_mode is not None:
            selected = next((result for result in self.mode_results if result.mode == self.selected_mode), None)
            if selected is None or selected.status != "available" or self.selected_option_id not in selected.option_ids:
                raise ValueError("选中方案必须属于该路段该方式的有效结果")
        return self


class PlanDraft(ContractModel):
    schema_version: Literal[4] = 4
    plan_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    revision: PositiveInt
    base_revision: int | None = Field(default=None, ge=1)
    rule_version: str = Field(min_length=1)
    days: tuple[DraftDay, ...] = Field(min_length=1)
    area_hints: tuple[AreaHint, ...] = ()
    legs: tuple[RouteLeg, ...] = ()

    @model_validator(mode="after")
    def validate_revision_and_ids(self):
        if self.base_revision is not None and self.revision <= self.base_revision:
            raise ValueError("revision 必须大于 base_revision")
        day_ids = [day.day_id for day in self.days]
        if len(day_ids) != len(set(day_ids)):
            raise ValueError("day_id 必须唯一")
        activity_ids = [activity.activity_id for day in self.days for activity in day.activities]
        if len(activity_ids) != len(set(activity_ids)):
            raise ValueError("activity_id 必须全行程唯一")
        leg_ids = [leg.leg_id for leg in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise ValueError("leg_id 必须唯一")
        if any(leg.plan_revision != self.revision for leg in self.legs):
            raise ValueError("路段必须引用当前 PlanDraft revision")
        area_ids = [area.area_hint_id for area in self.area_hints]
        if len(area_ids) != len(set(area_ids)):
            raise ValueError("area_hint_id 必须唯一")
        return self


class ValidationIssue(ContractModel):
    issue_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    category: Literal["missing_arrangement", "known_conflict", "unknown_info", "invalid_reference"]
    severity: Literal["info", "warning", "error"]
    scope: Literal["plan", "day", "slot", "activity", "leg"]
    day_id: str | None = None
    target_id: str | None = None
    message: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    triggers_repair: bool = False
    based_on_revision: PositiveInt


class RequirementResult(ContractModel):
    requirement_id: str = Field(min_length=1)
    status: Literal["satisfied", "unsatisfied", "unverified"]
    activity_ids: tuple[str, ...] = ()
    place_fact_ids: tuple[str, ...] = ()


class DayValidation(ContractModel):
    day_id: str = Field(min_length=1)
    completeness: Literal["complete", "incomplete", "not_required", "conditional"]
    feasibility: Literal["verified", "conflict", "unverified"]
    issue_ids: tuple[str, ...] = ()


class ValidationReport(ContractModel):
    plan_id: str = Field(min_length=1)
    based_on_revision: PositiveInt
    issues: tuple[ValidationIssue, ...] = ()
    days: tuple[DayValidation, ...] = ()
    requirements: tuple[RequirementResult, ...] = ()
    repair_states: tuple["RepairState", ...] = ()


class RepairState(ContractModel):
    day_id: str = Field(min_length=1)
    completed_rounds: int = Field(ge=0, le=3)
    active_round_id: str | None = None
    status: Literal["open", "active", "complete", "stopped"]
    stop_reason: Literal["complete", "round_limit", "no_progress", "deadline"] | None = None

    @model_validator(mode="after")
    def validate_repair_state(self):
        if (self.status == "active") != (self.active_round_id is not None):
            raise ValueError("active 状态必须且只能设置 active_round_id")
        if self.status in {"complete", "stopped"} and self.stop_reason is None:
            raise ValueError("complete/stopped 状态必须记录 stop_reason")
        if self.status == "complete" and self.stop_reason != "complete":
            raise ValueError("complete 状态的 stop_reason 必须是 complete")
        if self.status == "stopped" and self.stop_reason == "complete":
            raise ValueError("stopped 状态不能使用 complete 原因")
        if self.completed_rounds == 3 and self.status in {"open", "active"}:
            raise ValueError("达到三轮上限后不能继续修复")
        return self


class DayRevision(ContractModel):
    day_id: str = Field(min_length=1)
    base_revision: PositiveInt
    revision: PositiveInt
    based_on_report_revision: PositiveInt
    replacement_day: ProposalDay
    preserve_activity_ids: tuple[str, ...] = ()
    source_issue_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_revision_target(self):
        if self.revision <= self.base_revision:
            raise ValueError("DayRevision 必须产生更高版本")
        if self.replacement_day.day_id != self.day_id:
            raise ValueError("DayRevision 只能替换目标日期")
        if len(self.preserve_activity_ids) != len(set(self.preserve_activity_ids)):
            raise ValueError("preserve_activity_ids 必须唯一")
        if len(self.source_issue_ids) != len(set(self.source_issue_ids)):
            raise ValueError("source_issue_ids 必须唯一")
        replacement_ids = {
            activity.existing_activity_id for activity in self.replacement_day.activities
            if activity.existing_activity_id is not None
        }
        if not set(self.preserve_activity_ids).issubset(replacement_ids):
            raise ValueError("replacement_day 必须保留所有指定的 existing_activity_id")
        return self


class DaySummary(ContractModel):
    day_id: str = Field(min_length=1)
    known_stay_s: int = Field(ge=0)
    known_travel_s: int = Field(ge=0)
    unknown_leg_ids: tuple[str, ...] = ()
    feasibility: Literal["verified", "conflict", "unverified"]
    cost_status: Literal["available", "missing", "aggregation_pending"] = "aggregation_pending"
    known_cost_subtotal: str | None = None


class ItineraryPreview(ContractModel):
    schema_version: Literal[4] = 4
    preview_id: str = Field(min_length=1)
    planning_input: PlanningInput
    draft: PlanDraft
    facts: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    validation: ValidationReport
    day_summaries: tuple[DaySummary, ...] = ()
    calculation: "PreviewCalculation"

    @model_validator(mode="after")
    def validate_current_calculation(self):
        if self.validation.plan_id != self.draft.plan_id or self.validation.based_on_revision != self.draft.revision:
            raise ValueError("预览校验报告必须引用当前行程版本")
        if self.calculation.based_on_revision != self.draft.revision:
            raise ValueError("路线计算状态必须引用当前行程版本")
        if self.draft.request_id != self.planning_input.request.request_id:
            raise ValueError("预览草稿必须引用随预览提供的 PlanningInput")
        if self.draft.rule_version != self.planning_input.rules.rule_version:
            raise ValueError("预览草稿规则版本必须与 PlanningInput 一致")
        if {day.day_id for day in self.draft.days} != {day.day_id for day in self.planning_input.days}:
            raise ValueError("预览草稿日期集合必须覆盖 PlanningInput 日期")
        if self.calculation.current_routes == "completed" and self.calculation.pending_leg_ids:
            raise ValueError("current_routes=completed 时不能有 pending_leg_ids")
        draft_leg_ids = {leg.leg_id for leg in self.draft.legs}
        if set(self.calculation.pending_leg_ids) - draft_leg_ids:
            raise ValueError("pending_leg_ids 必须引用当前草稿路段")
        return self


class PreviewCalculation(ContractModel):
    based_on_revision: PositiveInt
    calculation_id: str = Field(min_length=1)
    current_routes: Literal["pending", "completed", "failed"]
    alternative_backfill: Literal["pending", "running", "completed", "failed"]
    pending_leg_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_pending_legs(self):
        if len(self.pending_leg_ids) != len(set(self.pending_leg_ids)):
            raise ValueError("pending_leg_ids 必须唯一")
        if self.current_routes == "completed" and self.pending_leg_ids:
            raise ValueError("current_routes=completed 时不能有 pending_leg_ids")
        if self.current_routes == "pending" and not self.pending_leg_ids:
            raise ValueError("current_routes=pending 时必须列出待计算路段")
        return self


class AddActivity(ContractModel):
    type: Literal["add_activity"]
    client_ref: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    slot_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)


class DeleteActivity(ContractModel):
    type: Literal["remove_activity"]
    day_id: str = Field(min_length=1)
    activity_id: str = Field(min_length=1)


class ReorderActivities(ContractModel):
    type: Literal["reorder_activities"]
    day_id: str = Field(min_length=1)
    activity_ids: tuple[str, ...]


class SelectLegMode(ContractModel):
    type: Literal["select_leg_mode"]
    day_id: str = Field(min_length=1)
    leg_id: str = Field(min_length=1)
    mode: Literal["transit", "bicycling", "walking"]
    option_id: str = Field(min_length=1)


EditOperation = Annotated[
    Union[AddActivity, DeleteActivity, ReorderActivities, SelectLegMode],
    Field(discriminator="type"),
]


class EditCommand(ContractModel):
    plan_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    base_revision: PositiveInt
    command_id: str = Field(min_length=1)
    operations: tuple[EditOperation, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_operation_days(self):
        if any(operation.day_id != self.day_id for operation in self.operations):
            raise ValueError("EditCommand 的每项操作必须属于命令 day_id")
        return self


DraftDay.model_rebuild()
ItineraryPreview.model_rebuild()
