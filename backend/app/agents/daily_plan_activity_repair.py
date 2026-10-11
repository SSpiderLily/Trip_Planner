"""F06 初版活动的受限局部修复契约与快照操作。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ..models.planning_v4 import PlanningInput, ProposalActivity, ProposalDay
from ..models.planning_v4.base import ContractModel


class DailyPlanModelOutput(ContractModel):
    """模型只输出每日安排；区域事实和请求版本由后端注入。"""

    days: tuple[ProposalDay, ...] = Field(min_length=1)
    missing_notes: tuple[str, ...] = ()


class ActivityReplacement(ContractModel):
    """修复只选择已核实候选与需求引用，不含日期、时段或任意说明字段。"""

    candidate_ref: str = Field(min_length=1)
    kind: Literal["sightseeing", "meal"]
    duration_s: int | None = Field(default=None, ge=0)
    requirement_ids: tuple[str, ...] = ()


class ActivityRepairItem(ContractModel):
    repair_id: str = Field(min_length=1)
    action: Literal["replace", "leave_unfilled"]
    replacement: ActivityReplacement | None = None

    @model_validator(mode="after")
    def validate_action_payload(self):
        if (self.action == "replace") != (self.replacement is not None):
            raise ValueError("replace 必须提供 replacement，leave_unfilled 不得提供 replacement")
        return self


class ActivityRepairOutput(ContractModel):
    repairs: tuple[ActivityRepairItem, ...] = ()


@dataclass(frozen=True)
class ActivityRepairTarget:
    """后端生成的局部定位；不使用模型提供的名称或候选值作为身份。"""

    repair_id: str
    day_index: int
    activity_index: int
    day_id: str
    day_date: date
    slot_id: str
    period: str
    expected_kind: Literal["sightseeing", "meal"]
    proposal_ref: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class DailyPlanRecoveryNotice:
    """不含模型原文的 F06 恢复记录，供本轮 F09 报告定位提示。"""

    day_id: str
    slot_id: str
    activity_index: int
    reason_code: Literal[
        "invalid_activity_structure",
        "candidate_reference_invalid",
        "candidate_incompatible",
        "requirement_reference_invalid",
        "activity_kind_reference_mismatch",
    ]


@dataclass(frozen=True)
class ActivityRepairSnapshot:
    """坏活动已隔离；保留日期元数据和好活动原位置，供单次patch调用。"""

    output: DailyPlanModelOutput
    targets: tuple[ActivityRepairTarget, ...]
    positioned_activities: tuple[tuple[int, int, ProposalActivity], ...]

    def rebuild(
        self,
        replacements: dict[str, ProposalActivity],
    ) -> DailyPlanModelOutput:
        by_position = {
            (day_index, activity_index): activity
            for day_index, activity_index, activity in self.positioned_activities
        }
        for target in self.targets:
            replacement = replacements.get(target.repair_id)
            if replacement is not None:
                by_position[(target.day_index, target.activity_index)] = replacement

        days = []
        for day_index, day in enumerate(self.output.days):
            activities = tuple(
                activity
                for (row_day, _), activity in sorted(by_position.items())
                if row_day == day_index
            )
            days.append(day.model_copy(update={"activities": activities}))
        return self.output.model_copy(update={"days": tuple(days)})


_ACTIVITY_STRUCTURE_CODE_BY_FIELD = {
    "proposal_ref": "invalid_activity_structure",
    "existing_activity_id": "invalid_activity_structure",
    "candidate_ref": "candidate_reference_invalid",
    "slot_id": "invalid_activity_structure",
    "kind": "invalid_activity_kind",
    "duration_s": "invalid_duration",
    "requirement_ids": "requirement_reference_invalid",
    "recommendation_reason": "invalid_activity_structure",
}

_REFERENCE_CODE_MAP = {
    "dangling_candidate": "unknown_candidate",
    "candidate_category_mismatch": "candidate_wrong_category",
    "candidate_slot_mismatch": "candidate_wrong_slot",
    "dangling_requirement": "unknown_requirement",
    "initial_existing_activity": "invalid_activity_structure",
    "activity_kind_slot_mismatch": "activity_wrong_kind_for_slot",
    "candidate_slot_scope_invalid": "candidate_wrong_slot",
    "selected_candidate_city_invalid": "candidate_not_selectable",
    "selected_candidate_location_missing": "candidate_not_selectable",
    "selected_candidate_category_invalid": "candidate_wrong_category",
    "selected_candidate_source_invalid": "candidate_not_selectable",
}

_REFERENCE_ERROR_GROUPS = {
    "missing_candidate_for_place": "地点活动必须引用一个当前有效候选；没有合适候选时将该项标记为缺项",
    "free_time_with_candidate": "free_time 不得引用POI候选；地点活动必须使用required_kind指定的类型",
    "invalid_duration": "duration_s 必须是非负整数秒；不能确认时省略该字段",
    "invalid_activity_kind": "kind 必须与时段匹配；具体类型见required_kind",
    "unknown_candidate": "candidate_ref 不在当前已核实候选中；只能从本目标列出的候选中选择",
    "candidate_wrong_category": "候选类别与required_kind不一致；只能从本目标列出的候选中选择",
    "candidate_wrong_slot": "候选不适用于当前slot_id；只能从本目标列出的候选中选择",
    "unknown_requirement": "requirement_ids含未知需求；只能使用所选候选列出的allowed_requirement_ids，也可以留空",
    "activity_wrong_kind_for_slot": "活动kind不符合当前时段；必须使用required_kind",
    "candidate_not_selectable": "候选缺少当前城市、类别、位置或来源的核实条件；只能使用本目标列出的候选",
    "candidate_reference_invalid": "candidate_ref 无效；只能从本目标列出的候选中选择",
    "candidate_incompatible": "候选类别或适用时段与当前活动要求不匹配",
    "requirement_reference_invalid": "需求引用无效；可以留空",
    "activity_kind_reference_mismatch": "活动类型与候选引用组合无效",
    "invalid_activity_structure": "活动字段结构不符合契约",
}


def build_reference_snapshot(
    output: DailyPlanModelOutput,
    planning_input: PlanningInput,
    activity_reasons: dict[tuple[int, int], tuple[str, ...]],
) -> ActivityRepairSnapshot | None:
    """仅隔离可用有效日期/时段/唯一proposal_ref精确定位的活动。"""
    slots_by_day = {day.day_id: {slot.slot_id: slot for slot in day.slots} for day in planning_input.days}
    refs = [activity.proposal_ref for day in output.days for activity in day.activities]
    if len(refs) != len(set(refs)) or any(not ref.strip() for ref in refs):
        return None

    targets: list[ActivityRepairTarget] = []
    positioned: list[tuple[int, int, ProposalActivity]] = []
    for day_index, day in enumerate(output.days):
        constraint = planning_input.days[day_index] if day_index < len(planning_input.days) else None
        if constraint is None or day.day_id != constraint.day_id or day.day_date != constraint.day_date:
            return None
        slot_by_id = slots_by_day.get(day.day_id, {})
        for activity_index, activity in enumerate(day.activities):
            reasons = activity_reasons.get((day_index, activity_index))
            if reasons is None:
                positioned.append((day_index, activity_index, activity))
                continue
            slot = slot_by_id.get(activity.slot_id)
            if slot is None or slot.eligibility == "skipped":
                return None
            expected_kind = _kind_for_period(slot.period)
            if expected_kind is None:
                return None
            targets.append(ActivityRepairTarget(
                repair_id=f"repair-{len(targets)}",
                day_index=day_index,
                activity_index=activity_index,
                day_id=day.day_id,
                day_date=day.day_date,
                slot_id=activity.slot_id,
                period=slot.period,
                expected_kind=expected_kind,
                proposal_ref=activity.proposal_ref,
                reason_codes=tuple(sorted(set(reasons))),
            ))

    if not targets:
        return None
    positioned_by_day: dict[int, list[tuple[int, ProposalActivity]]] = {}
    for day_index, activity_index, activity in positioned:
        positioned_by_day.setdefault(day_index, []).append((activity_index, activity))
    sanitized_days = tuple(
        day.model_copy(update={"activities": tuple(
            activity for _, activity in sorted(positioned_by_day.get(day_index, ()))
        )})
        for day_index, day in enumerate(output.days)
    )
    # build_reference_snapshot receives typed output; sanitize positions directly to
    # avoid retaining the bad activity in later proposal construction or logging.
    sanitized = output.model_copy(update={"days": sanitized_days})
    return ActivityRepairSnapshot(sanitized, tuple(targets), tuple(positioned))


def build_structural_snapshot(
    payload: object,
    planning_input: PlanningInput,
) -> ActivityRepairSnapshot | None:
    """把可定位的活动级Pydantic错误分离；日期/时段/提案身份不完整时拒绝。"""
    if not isinstance(payload, dict) or set(payload) - {"days", "missing_notes"}:
        return None
    if not isinstance(payload.get("days"), list) or len(payload["days"]) != len(planning_input.days):
        return None
    if "missing_notes" in payload and not isinstance(payload["missing_notes"], list):
        return None

    day_values: list[dict] = []
    raw_activities: list[list[tuple[int, dict, tuple[str, ...]]]] = []
    all_refs: list[str] = []
    for day_index, (raw_day, constraint) in enumerate(zip(payload["days"], planning_input.days, strict=True)):
        if not isinstance(raw_day, dict) or not isinstance(raw_day.get("activities"), list):
            return None
        if raw_day.get("day_id") != constraint.day_id or raw_day.get("day_date") != constraint.day_date.isoformat():
            return None
        try:
            # Validate all day-level metadata with activities removed. A bad theme,
            # lodging or weather value is global and cannot use local recovery.
            day_stub = ProposalDay.model_validate({**raw_day, "activities": []})
        except ValidationError:
            return None

        activity_rows: list[tuple[int, dict, tuple[str, ...]]] = []
        slot_by_id = {slot.slot_id: slot for slot in constraint.slots}
        for activity_index, raw_activity in enumerate(raw_day["activities"]):
            if not isinstance(raw_activity, dict):
                return None
            proposal_ref = raw_activity.get("proposal_ref")
            slot_id = raw_activity.get("slot_id")
            slot = slot_by_id.get(slot_id) if isinstance(slot_id, str) else None
            if not isinstance(proposal_ref, str) or not proposal_ref.strip() or slot is None or slot.eligibility == "skipped":
                return None
            all_refs.append(proposal_ref)
            try:
                ProposalActivity.model_validate(raw_activity)
                activity_rows.append((activity_index, raw_activity, ()))
            except ValidationError as exc:
                reasons = _structural_reasons(raw_activity, exc)
                if not reasons:
                    return None
                activity_rows.append((activity_index, raw_activity, reasons))
        raw_activities.append(activity_rows)
        day_values.append(day_stub.model_dump(mode="python"))

    if len(all_refs) != len(set(all_refs)):
        return None
    try:
        missing_notes = tuple(payload.get("missing_notes", ()))
        # Preserve valid global notes only after validating their exact contract.
        output = DailyPlanModelOutput(
            days=tuple(ProposalDay.model_validate(day) for day in day_values),
            missing_notes=missing_notes,
        )
    except (ValidationError, TypeError):
        return None

    targets: list[ActivityRepairTarget] = []
    positioned: list[tuple[int, int, ProposalActivity]] = []
    days_with_valid_activities: list[ProposalDay] = []
    for day_index, (day, rows, constraint) in enumerate(zip(output.days, raw_activities, planning_input.days, strict=True)):
        valid_activities = []
        slot_by_id = {slot.slot_id: slot for slot in constraint.slots}
        for activity_index, raw_activity, reasons in rows:
            if not reasons:
                activity = ProposalActivity.model_validate(raw_activity)
                valid_activities.append(activity)
                positioned.append((day_index, activity_index, activity))
                continue
            slot = slot_by_id[raw_activity["slot_id"]]
            expected_kind = _kind_for_period(slot.period)
            if expected_kind is None:
                return None
            targets.append(ActivityRepairTarget(
                repair_id=f"repair-{len(targets)}",
                day_index=day_index,
                activity_index=activity_index,
                day_id=day.day_id,
                day_date=day.day_date,
                slot_id=slot.slot_id,
                period=slot.period,
                expected_kind=expected_kind,
                proposal_ref=raw_activity["proposal_ref"],
                reason_codes=reasons,
            ))
        days_with_valid_activities.append(day.model_copy(update={"activities": tuple(valid_activities)}))

    if not targets:
        return None
    sanitized = output.model_copy(update={"days": tuple(days_with_valid_activities)})
    return ActivityRepairSnapshot(sanitized, tuple(targets), tuple(positioned))


def reference_reasons_from_issues(issues) -> dict[tuple[int, int], tuple[str, ...]] | None:
    """提取活动路径上的固定错误码；任何全局或未知错误都拒绝局部降级。"""
    reasons: dict[tuple[int, int], set[str]] = {}
    for issue in issues:
        location = _activity_location(issue.path)
        reason = _REFERENCE_CODE_MAP.get(issue.code)
        if location is None or reason is None:
            return None
        reasons.setdefault(location, set()).add(reason)
    if not reasons:
        return None
    return {key: tuple(sorted(value)) for key, value in reasons.items()}


def reference_reasons_from_selected(codes, locations) -> dict[tuple[int, int], tuple[str, ...]] | None:
    """选中候选校验保留每项活动路径，避免把全局代码误映射到所有活动。"""
    if not locations or set(codes) != {
        code for item_codes in locations.values() for code in item_codes
    }:
        return None
    mapped: dict[tuple[int, int], tuple[str, ...]] = {}
    for location, item_codes in locations.items():
        reasons = [_REFERENCE_CODE_MAP.get(code) for code in item_codes]
        if not item_codes or any(reason is None for reason in reasons):
            return None
        mapped[location] = tuple(sorted(set(reasons)))
    return mapped


def parse_patch_response(response, target_ids: set[str]) -> dict[str, ActivityRepairItem]:
    """独立解析每个patch；坏项不影响已正确返回的其他目标。"""
    import json

    if not isinstance(response, str):
        return {}
    try:
        value = response.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if len(lines) < 3 or lines[-1].strip() != "```":
                return {}
            language = lines[0][3:].strip().lower()
            if language not in {"", "json"}:
                return {}
            value = "\n".join(lines[1:-1]).strip()
        payload = json.loads(value)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("repairs"), list):
        return {}
    grouped: dict[str, list[ActivityRepairItem]] = {}
    counts: dict[str, int] = {}
    for raw in payload["repairs"]:
        repair_id = raw.get("repair_id") if isinstance(raw, dict) else None
        if isinstance(repair_id, str) and repair_id in target_ids:
            # 即使其中一条结构错误，也不能让同 repair_id 的另一条指令胜出。
            counts[repair_id] = counts.get(repair_id, 0) + 1
        try:
            item = ActivityRepairItem.model_validate(raw)
        except (ValidationError, TypeError):
            continue
        if item.repair_id not in target_ids:
            continue
        grouped.setdefault(item.repair_id, []).append(item)
    # Duplicate instructions for one target are ambiguous and therefore ignored,
    # including when one duplicate has an invalid action/replacement shape.
    return {
        repair_id: rows[0]
        for repair_id, rows in grouped.items()
        if len(rows) == 1 and counts.get(repair_id) == 1
    }


def notice_reason_code(target: ActivityRepairTarget) -> Literal[
    "invalid_activity_structure",
    "candidate_reference_invalid",
    "candidate_incompatible",
    "requirement_reference_invalid",
    "activity_kind_reference_mismatch",
]:
    """把多个错误收敛到安全、稳定的用户提示类别。"""
    priority = (
        "activity_wrong_kind_for_slot",
        "free_time_with_candidate",
        "missing_candidate_for_place",
        "invalid_activity_kind",
        "candidate_wrong_category",
        "candidate_wrong_slot",
        "candidate_not_selectable",
        "unknown_candidate",
        "unknown_requirement",
        "candidate_incompatible",
        "candidate_reference_invalid",
        "requirement_reference_invalid",
        "invalid_activity_structure",
    )
    reasons = set(target.reason_codes)
    reason = next((code for code in priority if code in reasons), "invalid_activity_structure")
    if reason in {"missing_candidate_for_place", "free_time_with_candidate", "invalid_activity_kind", "activity_wrong_kind_for_slot"}:
        return "activity_kind_reference_mismatch"
    if reason in {"candidate_wrong_category", "candidate_wrong_slot", "candidate_not_selectable", "candidate_incompatible"}:
        return "candidate_incompatible"
    if reason == "unknown_candidate":
        return "candidate_reference_invalid"
    if reason == "unknown_requirement":
        return "requirement_reference_invalid"
    return reason


def safe_reason_text(reason_codes: tuple[str, ...]) -> str:
    """返回固定诊断文案，不拼接错误值、字段未知名或异常原文。"""
    groups = sorted({_REFERENCE_ERROR_GROUPS.get(code, "活动字段结构不符合契约") for code in reason_codes})
    return "；".join(groups)


def _structural_reasons(raw_activity: dict, exc: ValidationError) -> tuple[str, ...]:
    reasons = set()
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        location = error.get("loc", ())
        field = location[-1] if location and isinstance(location[-1], str) else None
        if error.get("type") == "value_error":
            kind, candidate = raw_activity.get("kind"), raw_activity.get("candidate_ref")
            if kind == "free_time" and candidate is not None:
                reasons.add("free_time_with_candidate")
                continue
            if kind in {"sightseeing", "meal"} and candidate is None:
                reasons.add("missing_candidate_for_place")
                continue
        reason = _ACTIVITY_STRUCTURE_CODE_BY_FIELD.get(field)
        if field == "candidate_ref" and raw_activity.get("kind") in {"sightseeing", "meal"}:
            reason = "missing_candidate_for_place"
        if field == "candidate_ref" and raw_activity.get("kind") == "free_time":
            reason = "free_time_with_candidate"
        if field == "duration_s" and error.get("type") in {"greater_than_equal", "int_type", "int_from_float"}:
            reason = "invalid_duration"
        if field == "kind":
            reason = "invalid_activity_kind"
        if reason is None and error.get("type") == "extra_forbidden":
            reason = "invalid_activity_structure"
        if reason is None:
            return ()
        reasons.add(reason)
    return tuple(sorted(reasons))


def _activity_location(path: str) -> tuple[int, int] | None:
    # Paths are generated by the validator. Accept only its fixed numeric grammar.
    import re

    match = re.fullmatch(r"proposal\.days\[(\d+)\]\.activities\[(\d+)\](?:\.[a-z_]+)?", path)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _kind_for_period(period: str) -> Literal["sightseeing", "meal"] | None:
    if period in {"morning", "afternoon", "evening"}:
        return "sightseeing"
    if period in {"breakfast", "lunch", "dinner"}:
        return "meal"
    return None
