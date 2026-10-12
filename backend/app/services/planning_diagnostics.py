"""规划与单日修订的安全诊断协议；观测不改变重试、采用或降级规则。"""
from contextlib import contextmanager
from functools import wraps
import re
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from app.models import planning_v4
from app.services.observation_service import span
from app.services.planning_v4_validator import ContractValidationError

MAX_DIAGNOSTICS = 8
# 仅保留契约中声明的字段名，模型填入的字典键/引用值不进入诊断路径。
_PATH_FIELDS = frozenset({"draft", "proposal", "revision", "facts", "validation", "report"}).union(
    *(set(value.model_fields) for value in vars(planning_v4).values()
      if isinstance(value, type) and hasattr(value, "model_fields"))
)
_INPUT_CONDITIONS = {
    "recalculation returned the wrong plan revision": "recalculation_plan_revision_mismatch",
    "recalculated report does not bind the new revision": "recalculation_report_revision_mismatch",
    "recalculation modified the target day after DayRevision validation": "recalculation_target_day_changed",
    "recalculation modified a non-target day": "recalculation_other_day_changed",
    "recalculation modified non-target route legs": "recalculation_other_legs_changed",
    "DayRevision target does not match the active repair day": "revision_target_day_mismatch",
    "DayRevision source_issue_ids do not match the assigned repair issues": "revision_issue_set_mismatch",
    "DayRevision target does not exist": "revision_target_day_missing",
    "DayRevision revision must be the next PlanDraft revision": "revision_not_next_version",
    "modifiable activity IDs must belong to the target day": "modifiable_activity_outside_day",
    "DayRevision preserve_activity_ids do not match the repair policy": "preserve_activity_set_mismatch",
    "removable activity IDs must belong to the target day": "removable_activity_outside_day",
    "an activity cannot be both preserved and modifiable": "activity_policy_overlap",
    "DayRevision changed the relative order of preserved activities": "preserved_activity_order_changed",
    "DayRevision omitted an activity that is not removable": "protected_activity_removed",
    "repair policy must account for every real target-day activity": "activity_policy_incomplete",
    "revision candidate is absent": "revision_candidate_missing",
    "an existing activity can appear only once in a DayRevision": "duplicate_existing_activity",
    "proposal_ref values must be unique in a DayRevision": "duplicate_proposal_ref",
    "DayRevision references an activity outside the target day": "revision_activity_outside_day",
}
_GUIDANCE = {
    "transit_time_mismatch": "路线事实和路段的公共交通出发时刻须属于目的地时区的同一日期、同一分钟；检查跨分钟或日期的错误复用。",
    "candidate_wrong_slot": "活动时段必须包含在候选允许的时段集合中。",
    "candidate_wrong_kind": "活动类别必须与候选类别相符。",
    "dangling_candidate": "活动引用的候选必须存在于当前候选池。",
    "dangling_route_fact": "路段引用的路线事实必须存在于当前事实集合。",
}
_SCHEMA_TYPES = frozenset("missing extra_forbidden literal_error enum value_error model_type string_type int_type int_parsing float_type greater_than_equal less_than_equal too_short too_long".split())
_EXCEPTION_TYPES = frozenset("ContractValidationError ValidationError DayRepairInputError DayRepairError PlanningError CurrentRouteError TypeError AttributeError ValueError RuntimeError TimeoutError CancelledError KeyError IndexError".split())


def safe_path(path):
    parts = path if isinstance(path, (list, tuple)) else re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\[\d+\]", str(path))
    output = []
    for part in parts[:16]:
        if isinstance(part, int) and 0 <= part <= 100000:
            output.append(f"[{part}]")
        elif isinstance(part, str) and re.fullmatch(r"\[\d{1,6}\]", part):
            output.append(part)
        else:
            field = part if isinstance(part, str) and part in _PATH_FIELDS else "field"
            output.append(("." if output else "") + field)
    return "".join(output) or "$"


def safe_error_code(code):
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z_0-9]{0,95}", code):
        return code
    return "REPAIR_STAGE_FAILED"


def _time_comparison(path, draft, facts, timezone_name):
    match = re.fullmatch(r"draft\.legs\[(\d+)\]\.mode_results\[(\d+)\]", path)
    if not match or draft is None or facts is None:
        return None
    try:
        leg = draft.legs[int(match[1])]
        mode = leg.mode_results[int(match[2])]
        fact = next(f for f in facts.routes if f.fact_id == mode.route_fact_id)
        left, right = leg.requested_departure_at, fact.requested_departure_at
        # 只记录本条件需要的时刻与差值，不记录地点、请求、响应全文。
        zone = ZoneInfo(timezone_name)
        return {
            "leg_departure_at": left.isoformat() if left else None,
            "route_fact_departure_at": right.isoformat() if right else None,
            "same_query_minute": bool(left and right and
                left.astimezone(zone).replace(second=0, microsecond=0) ==
                right.astimezone(zone).replace(second=0, microsecond=0)),
            "difference_s": (left - right).total_seconds() if left and right else None,
        }
    except (IndexError, StopIteration):
        return None


def error_summary(exc, *, draft=None, facts=None, timezone_name="Asia/Shanghai", error_prefix="REPAIR"):
    diagnostics = []
    if isinstance(exc, ContractValidationError):
        code, count = "REPAIR_REFERENCE_INVALID", len(exc.issues)
        for issue in exc.issues[:MAX_DIAGNOSTICS]:
            # ReferenceIssue.code 由后端校验器定义；message 可能含模型值，不能保存。
            issue_code = issue.code if issue.code in _REFERENCE_CODES else "reference_invalid"
            item = {"code": issue_code, "path": safe_path(issue.path),
                    "guidance": _GUIDANCE.get(issue_code, "此字段不满足后端引用校验；按错误码定位对应规则。")}
            if issue_code == "transit_time_mismatch":
                comparison = _time_comparison(issue.path, draft, facts, timezone_name)
                if comparison is not None:
                    item["time_comparison"] = comparison
            diagnostics.append(item)
    elif isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        code, count = "REPAIR_SCHEMA_INVALID", len(errors)
        diagnostics = [{"code": e["type"] if e["type"] in _SCHEMA_TYPES else "invalid_structure",
                        "path": safe_path(e["loc"]), "guidance": "字段不满足数据结构约束。"}
                       for e in errors[:MAX_DIAGNOSTICS]]
    else:
        condition = _INPUT_CONDITIONS.get(str(exc))
        code, count = safe_error_code(getattr(exc, "code", None)), 1
        if condition:
            code = "REPAIR_ACCEPTANCE_INVALID"
        diagnostics = [{"code": condition or code, "path": "$",
                        "guidance": str(exc) if condition else "该阶段执行失败；未保存外部异常文本。"}]
    exception_type = type(exc).__name__
    if error_prefix != "REPAIR" and code.startswith("REPAIR_"):
        code = error_prefix + code[len("REPAIR"):]
    return {"valid": False, "error_code": code,
            "exception_type": exception_type if exception_type in _EXCEPTION_TYPES else "Exception",
            "issue_count": count,
            "diagnostics_truncated": count > MAX_DIAGNOSTICS, "diagnostics": diagnostics}


class _RecordedFailure(Exception):
    def __init__(self, code):
        self.code = code


@contextmanager
def diagnostic_stage(name, kind, metadata, *, error_prefix="PLANNING", **context):
    """日志使用安全异常；调用方仍收到原异常，不影响既有分支与取消语义。"""
    failure = None
    try:
        with span(name, kind, metadata) as record:
            try:
                yield record
            except BaseException as exc:
                failure = exc
                record.output = error_summary(exc, error_prefix=error_prefix, **context)
                record.output.update(schema_version=1, stage=name,
                                     outcome="cancelled" if type(exc).__name__ == "CancelledError" else "rejected")
                raise _RecordedFailure(record.output["error_code"]) from None
            else:
                if record.output is None:
                    record.output = {"valid": True}
                record.output.setdefault("schema_version", 1)
                record.output.setdefault("stage", name)
                record.output.setdefault("outcome", "rejected" if record.output.get("valid") is False else "passed")
    except _RecordedFailure:
        raise failure


def repair_stage(stage, metadata, **context):
    return diagnostic_stage(f"repair.{stage}", "validation", metadata, error_prefix="REPAIR", **context)


def observe_pipeline_run(function):
    """记录整次生成的关联标识和交付质量；技术完成不代表每天安排完整。"""
    @wraps(function)
    async def observed(self, request, *, task_id, plan_id, progress):
        with diagnostic_stage("planning.v4.run", "planning", {
            "request_id": getattr(request, "request_id", None),
            "plan_id": plan_id,
            "task_id": task_id,
            "timeouts_enabled": self.enforce_timeouts,
        }) as record:
            result = await function(self, request, task_id=task_id, plan_id=plan_id, progress=progress)
            preview = result.snapshot.preview
            days = []
            for day in preview.draft.days:
                validation = next((d for d in preview.validation.days if d.day_id == day.day_id), None)
                state = next((s for s in preview.validation.repair_states if s.day_id == day.day_id), None)
                days.append({
                    "day_id": day.day_id,
                    "sightseeing_count": sum(a.kind == "sightseeing" for a in day.activities),
                    "meal_count": sum(a.kind == "meal" for a in day.activities),
                    "free_time_count": sum(a.kind == "free_time" for a in day.activities),
                    "unfilled_slot_count": len(day.unfilled_slot_ids),
                    "completeness": validation.completeness if validation else "unknown",
                    "feasibility": validation.feasibility if validation else "unknown",
                    "repair_rounds": state.completed_rounds if state else 0,
                    "repair_stop_reason": state.stop_reason if state else None,
                })
            issues = preview.validation.issues
            incomplete = any(d["completeness"] in {"incomplete", "unknown"} or d["unfilled_slot_count"] for d in days)
            record.output = {
                "outcome": "draft_ready_with_gaps" if incomplete else "draft_ready",
                "revision": preview.draft.revision,
                "issue_counts": {category: sum(i.category == category for i in issues)
                                 for category in ("missing_arrangement", "known_conflict", "unknown_info", "invalid_reference")},
                "days": days,
            }
            return result
    return observed


# 后端固定错误码白名单；未知代码不会带出模型字段或异常值。
_REFERENCE_CODES = frozenset("""
activity_in_skipped_slot activity_location_missing activity_poi_mismatch calculation_version_mismatch
candidate_activity_mismatch candidate_category_mismatch candidate_location_missing candidate_poi_mismatch
candidate_slot_mismatch cross_day_endpoint dangling_added_candidate dangling_area_anchor
dangling_area_basis dangling_candidate dangling_candidate_area dangling_candidate_fact
dangling_candidate_requirement dangling_candidate_slot dangling_day dangling_day_issue
dangling_day_validation dangling_edit_day dangling_existing_activity dangling_field_source
dangling_issue_activity dangling_issue_day dangling_issue_evidence dangling_issue_leg dangling_issue_slot
dangling_leg_day dangling_leg_place dangling_lodging_area dangling_lodging_fact dangling_place_fact
dangling_removed_activity dangling_repair_state dangling_requirement dangling_requirement_activity
dangling_requirement_fact dangling_requirement_result dangling_revision_candidate
dangling_revision_requirement dangling_route_fact dangling_route_option dangling_selected_leg
dangling_slot dangling_source dangling_summary_leg dangling_weather_fact day_date_mismatch
day_date_set_mismatch day_set_mismatch day_summary_set_mismatch day_validation_set_mismatch
default_duration_mismatch duplicate_area_hint duplicate_candidate_id duplicate_candidate_requirement
duplicate_candidate_slot duplicate_issue_id duplicate_reorder_id duplicate_repair_state
duplicate_revision_unfilled_slot duplicate_unfilled_slot edit_operation_day_mismatch endpoint_poi_mismatch
initial_existing_activity invalid_active_repair_state invalid_added_slot invalid_lodging_candidate
invalid_lodging_endpoint invalid_revision_candidate invalid_revision_candidate_requirement
invalid_revision_candidate_slot invalid_revision_issue invalid_revision_lodging invalid_revision_slot
invalid_revision_unfilled_slot invalid_selected_option invalid_selected_route invalid_unfilled_slot
issue_activity_day_mismatch issue_leg_day_mismatch issue_version_mismatch lodging_area_mismatch
lodging_candidate_mismatch lodging_location_missing lodging_poi_mismatch missing_issue_target
missing_slot_not_reported non_transit_departure_time plan_mismatch preserved_activity_identity_changed
preserved_activity_missing proposal_slot_unaccounted provider_time_mismatch reorder_must_be_permutation
repair_round_limit_exceeded repair_state_set_mismatch request_mismatch requirement_activity_fact_mismatch
requirement_result_set_mismatch revision_base_mismatch revision_date_mismatch revision_day_missing
revision_issue_missing revision_plan_mismatch revision_report_mismatch revision_skipped_slot_content
revision_slot_unaccounted route_destination_coordinate_mismatch route_direction_mismatch
route_error_without_category route_mode_mismatch route_origin_coordinate_mismatch route_status_mismatch
rule_version_mismatch skipped_slot_content skipped_slot_missing_issue source_mismatch stale_base_revision
stale_edit_revision transit_date_not_sent transit_strategy_mismatch transit_strategy_not_sent
transit_time_evidence_mismatch transit_time_mismatch transit_time_not_sent unbound_satisfied_requirement
unbound_transit_strategy unbound_transit_time unknown_arrival_endpoint unknown_departure_endpoint
unknown_existing_activity unknown_info_triggers_repair unknown_preserved_activity unrecorded_weather_gap
unsupported_area_center validation_version_mismatch
""".split())
