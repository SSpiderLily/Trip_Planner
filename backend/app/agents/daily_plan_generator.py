"""生成单份 planning_v4 每日安排提案，并绑定后端核实事实。"""
from __future__ import annotations

from app.planning.context import DailyPlanProjection, SYSTEM_PROMPT
from app.planning.rules import _InvalidReferences, _reference_error, _stable_id

from app.services.model_diagnostics import safe_finish_reason

from app.services.planning_lodging_policy import lodging_stays, stay_for_days, city_for_days, choose_lodging_for_stay
from app.services.planning_timeout_policy import deadline_after, wait_timeout, timeouts_enabled
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from datetime import date
import json
import math
import time
from threading import Event, Lock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, ValidationError

from .daily_plan_activity_repair import (
    ActivityRepairOutput,
    ActivityRepairSnapshot,
    ActivityRepairTarget,
    DailyPlanModelOutput,
    DailyPlanRecoveryNotice,
    build_reference_snapshot,
    build_structural_snapshot,
    notice_reason_code,
    parse_patch_response,
    reference_reasons_from_issues,
    reference_reasons_from_selected,
    safe_reason_text,
)
from ..models.planning_v4 import (
    AreaHint,
    CandidateRef,
    DayRevision,
    FactStore,
    PlanDraft,
    PlanProposal,
    PlanningInput,
    ProposalActivity,
    ProposalDay,
    DraftActivity,
    DraftDay,
    LodgingRef,
    TaskResult,
)
from ..models.planning_v4.base import ContractModel
from ..models.planning_v4.facts import PlaceFact, WeatherFact
from ..services.planning_candidate_policy import applicable_slot_ids, candidate_allows_slot
from ..services.planning_v4_validation import is_required_place_gap
from ..services.llm_service import LLMCompletion, get_llm
from ..services.observation_service import span, token_usage
from ..services.planning_v4_validator import (
    ContractValidationError,
    validate_day_revision,
    validate_plan_proposal,
)
from ..services.weather_query_service import _has_valid_weather_fields
from .attraction_subtask import _category_status, _city_matches
from .dining_subtask import _dining_category
from .execution import PlanningError
from .lodging_subtask import _is_region_or_business_area, _lodging_category, _matches_original_query


DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_FORMAT_REPAIRS = 1
_MODEL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="daily-plan-model")


class DailyDayRevisionModelOutput(ContractModel):
    """模型只输出一个目标日期，不返回或重生成其他日期。"""

    replacement_day: ProposalDay


_MAX_FORMAT_DIAGNOSTICS = 8
_SAFE_PATH_FIELDS = {
    "initial": {"days", "missing_notes"},
    "revision": {"replacement_day"},
    "day": {"day_id", "day_date", "theme", "activities", "unfilled_slot_ids", "lodging", "weather_advice"},
    "activity": {
        "proposal_ref", "existing_activity_id", "candidate_ref", "slot_id", "kind",
        "requirement_ids", "recommendation_reason",
    },
    "lodging": {"candidate_ref", "area_hint_id", "recommendation_reason"},
}
_SAFE_PATH_CHILD = {
    ("initial", "days"): "day",
    ("revision", "replacement_day"): "day",
    ("day", "activities"): "activity",
    ("day", "lodging"): "lodging",
}
_SAFE_VALIDATION_TYPES = frozenset({
    "missing", "extra_forbidden", "literal_error", "enum", "value_error",
    "json_invalid", "model_type", "model_attributes_type", "list_type",
    "tuple_type", "dict_type", "string_type", "int_type", "float_type",
    "bool_type", "int_from_float", "greater_than_equal", "less_than_equal",
    "greater_than", "less_than", "finite_number", "date_parsing",
    "date_from_datetime_parsing", "string_too_short", "string_too_long",
    "too_short", "too_long",
})
_VALIDATION_TYPE_GROUPS = {
    "missing": "missing_required_field",
    "extra_forbidden": "unexpected_field",
    "literal_error": "invalid_choice",
    "enum": "invalid_choice",
    "value_error": "invalid_combination",
    "json_invalid": "invalid_json",
    "model_type": "invalid_shape",
    "model_attributes_type": "invalid_shape",
    "list_type": "invalid_shape",
    "tuple_type": "invalid_shape",
    "dict_type": "invalid_shape",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "float_type": "invalid_type",
    "bool_type": "invalid_type",
    "int_from_float": "invalid_number",
    "greater_than_equal": "invalid_number",
    "less_than_equal": "invalid_number",
    "greater_than": "invalid_number",
    "less_than": "invalid_number",
    "finite_number": "invalid_number",
    "date_parsing": "invalid_date",
    "date_from_datetime_parsing": "invalid_date",
    "string_too_short": "invalid_value",
    "string_too_long": "invalid_value",
    "too_short": "invalid_size",
    "too_long": "invalid_size",
}
_SAFE_FORMAT_INSTRUCTIONS = {
    "invalid_json": "输出合法JSON对象；不要添加Markdown围栏或说明文字。",
    "invalid_shape": "按目标输出结构提供对象、数组和字段类型；只使用输入中的事实与ID。",
    "invalid_structure": "修复未分类的结构错误；只使用Schema字段，不改变行程语义或增加事实。",
    "missing_required_field": "补齐该必需字段；只使用输入中已有的日期、时段和候选ID。",
    "unexpected_field": "删除未定义字段；不要增加Schema以外的信息。",
    "invalid_choice": "使用Schema为该字段定义的枚举值。",
    "invalid_combination": "检查活动字段组合：sightseeing/meal必须引用候选；free_time不得引用POI候选。",
    "invalid_type": "使用Schema要求的数据类型；不要改动活动语义或增加事实。",
    "invalid_number": "使用非负整数秒数；只修改结构，不调整行程语义。",
    "invalid_date": "使用输入中对应日期的YYYY-MM-DD值。",
    "invalid_size": "检查该列表满足Schema规定的项目数量；保留所有请求日期。",
    "invalid_value": "按该字段的固定Schema约束修复；不要改动行程语义或增加事实。",
    "additional_issues": "继续检查其他字段的固定Schema约束；不要改动行程语义或增加事实。",
}


@dataclass(frozen=True)
class DailyPlanExecution:
    proposal: PlanProposal
    draft: PlanDraft
    usage: dict[str, int | None] | None
    model_calls: int
    format_repairs: int
    elapsed_seconds: float
    recovery_notices: tuple[DailyPlanRecoveryNotice, ...] = ()


@dataclass(frozen=True)
class DailyDayRevisionExecution:
    revision: DayRevision
    usage: dict[str, int | None] | None
    model_calls: int
    format_repairs: int
    elapsed_seconds: float


class _ModelCallState:
    """超时与排队开始之间使用锁排序，过期工作不会继续发模型请求。"""

    def __init__(self):
        self._lock = Lock()
        self._started = False
        self._cancelled = False

    def cancel_before_start(self) -> None:
        with self._lock:
            if not self._started:
                self._cancelled = True

    def try_start(self, deadline: float) -> bool:
        with self._lock:
            if self._cancelled or time.monotonic() >= deadline:
                return False
            self._started = True
            return True

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started


def _parse_json(text: Any) -> Any:
    if not isinstance(text, str):
        raise ValueError("response_not_text")
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("invalid_fenced_json")
        language = lines[0][3:].strip().lower()
        if language not in {"", "json"}:
            raise ValueError("unsupported_fence")
        value = "\n".join(lines[1:-1]).strip()
    return json.loads(value)


class DailyPlanGenerator(DailyPlanProjection):
    """使用一次独立 LLM 调用生成 PlanProposal 并确定性绑定 PlanDraft。"""

    def __init__(self, llm=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds 必须大于0且不超过120秒")
        self.llm = llm
        self.timeout_seconds = float(timeout_seconds)

    def generate(
        self,
        planning_input: PlanningInput,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        source_run_id: str,
        plan_id: str | None = None,
        cancel_event: Event | None = None,
    ) -> DailyPlanExecution:
        stats = _GenerationStats()
        try:
            return self._generate(
                planning_input,
                candidates,
                facts,
                area_hints,
                task_results,
                source_run_id=source_run_id,
                plan_id=plan_id,
                cancel_event=cancel_event,
                _stats=stats,
            )
        except Exception as exc:
            stats.attach(exc)
            raise

    def revise_day(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        report,
        day_id: str,
        issues,
        assignments,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        preserve_activity_ids: tuple[str, ...],
        modifiable_activity_ids: tuple[str, ...] = (),
        removable_activity_ids: tuple[str, ...] = (),
        source_run_id: str | None = None,
        round_id: str | None = None,
        round_number: int = 1,
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> DailyDayRevisionExecution:
        stats = _GenerationStats()
        try:
            return self._revise_day_impl(
                planning_input, draft, report, day_id, issues, assignments,
                candidates, facts, area_hints, task_results,
                preserve_activity_ids=preserve_activity_ids,
                modifiable_activity_ids=modifiable_activity_ids,
                removable_activity_ids=removable_activity_ids,
                source_run_id=source_run_id,
                round_id=round_id,
                round_number=round_number,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
                _stats=stats,
            )
        except Exception as exc:
            stats.attach(exc)
            raise

    def _revise_day_impl(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        report,
        day_id: str,
        issues,
        assignments,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        preserve_activity_ids: tuple[str, ...],
        modifiable_activity_ids: tuple[str, ...] = (),
        removable_activity_ids: tuple[str, ...] = (),
        source_run_id: str | None = None,
        round_id: str | None = None,
        round_number: int = 1,
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
        _stats: "_GenerationStats",
    ) -> DailyDayRevisionExecution:
        """只为一个日期调用模型并产生经引用校验的 DayRevision。"""
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须为正的有限数")
        effective_timeout = min(
            self.timeout_seconds,
            float(timeout_seconds) if timeout_seconds is not None else self.timeout_seconds,
        )
        started = time.monotonic()
        usage_totals = _UsageAccumulator()
        model_calls = 0
        format_repairs = 0
        _stats.sync(model_calls, format_repairs, usage_totals.value())
        run_id = source_run_id or draft.source_run_id
        old_day = next((day for day in draft.days if day.day_id == day_id), None)
        constraint = next((day for day in planning_input.days if day.day_id == day_id), None)
        if old_day is None or constraint is None:
            raise PlanningError("PLAN_REVISION_DAY_MISSING", "日期修订目标不存在", step="daily_plan_revision_input")
        if report.plan_id != draft.plan_id or report.based_on_revision != draft.revision:
            raise PlanningError("PLAN_REVISION_REPORT_STALE", "日期修订报告未绑定当前草稿版本", step="daily_plan_revision_input")
        if any(issue.day_id not in {day_id, None} for issue in issues):
            raise PlanningError("PLAN_REVISION_ISSUE_SCOPE_INVALID", "日期修订包含其他日期的问题", step="daily_plan_revision_input")
        if not issues or any(not is_required_place_gap(planning_input, issue) for issue in issues):
            raise PlanningError("PLAN_REVISION_ISSUE_INVALID", "日期修订仅接受必需时段的真实地点缺项", step="daily_plan_revision_input")

        messages = self._revision_messages(
            planning_input, draft, report, old_day, constraint, issues, assignments,
            candidates, facts, area_hints, task_results,
            preserve_activity_ids, modifiable_activity_ids, removable_activity_ids,
            round_id, round_number,
        )
        deadline = deadline_after(started, effective_timeout)
        output: DailyDayRevisionModelOutput | None = None
        for attempt in range(MAX_FORMAT_REPAIRS + 1):
            try:
                response, usage, finish_reason, call_started = self._invoke_once(
                    messages,
                    deadline,
                    attempt=attempt + 1,
                    request_id=planning_input.request.request_id,
                    source_run_id=run_id,
                    cancel_event=cancel_event,
                )
            except PlanningError as exc:
                model_calls += getattr(exc, "model_calls", 0)
                usage_totals.add(getattr(exc, "usage", None))
                exc.model_calls = model_calls
                exc.format_repairs = format_repairs
                exc.usage = usage_totals.value()
                _stats.sync(model_calls, format_repairs, exc.usage)
                raise
            if call_started:
                model_calls += 1
            usage_totals.add(usage)
            _stats.sync(model_calls, format_repairs, usage_totals.value())
            if finish_reason is not None and finish_reason != "stop":
                if finish_reason != "length" or format_repairs >= MAX_FORMAT_REPAIRS:
                    raise PlanningError(
                        "PLAN_REVISION_OUTPUT_INCOMPLETE",
                        "日期修订模型未正常完成 JSON 输出",
                        step="daily_plan_revision_output",
                    )
                format_repairs += 1
                _stats.sync(model_calls, format_repairs, usage_totals.value())
                messages = [
                    *messages,
                    {"role": "assistant", "content": response if isinstance(response, str) else ""},
                    {"role": "user", "content": "上次输出因长度限制被截断。只补全目标日期的完整 replacement_day JSON，不新增事实或日期。"},
                ]
                continue
            with span(
                "validation.daily_plan_revision_output",
                "validation",
                {"attempt": attempt + 1, "day_id": day_id},
            ) as validation_record:
                output, diagnostics, issue_count, truncated = self._validate_model_output(
                    response, DailyDayRevisionModelOutput,
                )
                validation_record.output = {
                    "valid": output is not None,
                    "issue_count": issue_count,
                    "diagnostics_truncated": truncated,
                    "diagnostics": diagnostics,
                }
            if output is not None:
                break
            if format_repairs >= MAX_FORMAT_REPAIRS:
                raise PlanningError(
                    "PLAN_REVISION_OUTPUT_INVALID",
                    "日期修订模型输出的 JSON 格式或字段结构无效",
                    step="daily_plan_revision_output",
                ) from None
            format_repairs += 1
            _stats.sync(model_calls, format_repairs, usage_totals.value())
            messages = [
                *messages,
                {"role": "assistant", "content": response if isinstance(response, str) else ""},
                {
                    "role": "user",
                    "content": "上次回答不符合 replacement_day JSON 结构。只修复结构诊断中的问题，不改动行程语义，不增加事实。安全诊断如下："
                    + json.dumps(diagnostics, ensure_ascii=False),
                },
            ]
        if output is None:
            raise PlanningError("PLAN_REVISION_OUTPUT_INVALID", "日期修订没有可用结构化输出", step="daily_plan_revision_output")

        source_issue_ids = tuple(dict.fromkeys(issue.issue_id for issue in issues))
        try:
            revision = DayRevision(
                day_id=day_id,
                base_revision=draft.revision,
                revision=draft.revision + 1,
                based_on_report_revision=report.based_on_revision,
                replacement_day=output.replacement_day,
                preserve_activity_ids=preserve_activity_ids,
                source_issue_ids=source_issue_ids,
            )
            validate_day_revision(revision, draft, report, planning_input, candidates, facts)
            self._validate_revision_candidates(
                revision, planning_input, candidates, facts, area_hints,
                set(preserve_activity_ids), set(modifiable_activity_ids),
            )
        except ContractValidationError as exc:
            raise PlanningError(
                "PLAN_REVISION_REFERENCES_INVALID",
                "日期修订引用与当前事实不一致",
                step="daily_plan_revision_validation",
            ) from exc
        except (ValueError, TypeError) as exc:
            raise PlanningError(
                "PLAN_REVISION_REFERENCES_INVALID",
                "日期修订引用与当前事实不一致",
                step="daily_plan_revision_validation",
            ) from exc
        return DailyDayRevisionExecution(
            revision=revision,
            usage=usage_totals.value(),
            model_calls=model_calls,
            format_repairs=format_repairs,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def _generate(
        self,
        planning_input: PlanningInput,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        source_run_id: str,
        plan_id: str | None,
        cancel_event: Event | None,
        _stats: "_GenerationStats",
    ) -> DailyPlanExecution:
        started = time.monotonic()
        deadline = deadline_after(started, self.timeout_seconds)
        if not isinstance(planning_input, PlanningInput):
            raise TypeError("planning_input 必须是 PlanningInput")
        if not isinstance(facts, FactStore):
            raise TypeError("facts 必须是 FactStore")
        if not source_run_id or not isinstance(source_run_id, str):
            raise ValueError("source_run_id 不能为空")
        if any(not isinstance(item, CandidateRef) for item in candidates):
            raise TypeError("candidates 必须是 CandidateRef 序列")
        if any(not isinstance(item, AreaHint) for item in area_hints):
            raise TypeError("area_hints 必须是 AreaHint 序列")
        if any(not isinstance(item, TaskResult) for item in task_results):
            raise TypeError("task_results 必须是 TaskResult 序列")
        safe_context = {
            "request_id": planning_input.request.request_id,
            "run_id": source_run_id,
            "candidate_count": len(candidates),
            "fact_place_count": len(facts.places),
            "weather_fact_count": len(facts.weather),
            "area_hint_count": len(area_hints),
            "task_result_count": len(task_results),
            "timeout_seconds": wait_timeout(self.timeout_seconds),
        }
        with span("agent.daily_plan_generation", "agent", safe_context) as agent_record:
            usage_totals = _UsageAccumulator()
            model_calls = 0
            format_repairs = 0
            repair_budget_used = False
            recovery_notices: tuple[DailyPlanRecoveryNotice, ...] = ()
            local_repair_details: dict[str, dict[str, Any]] = {}
            _stats.sync(model_calls, format_repairs, usage_totals.value())
            with span(
                "validation.daily_plan_inputs",
                "validation",
                {"candidate_count": len(candidates), "area_hint_count": len(area_hints)},
            ) as validation_record:
                try:
                    self._validate_input_references(
                        planning_input,
                        candidates,
                        facts,
                        area_hints,
                        task_results,
                        source_run_id,
                    )
                    validation_record.output = {"valid": True}
                except PlanningError as exc:
                    validation_record.output = {
                        "valid": False,
                        "issue_codes": list(getattr(exc, "reference_issue_codes", ())),
                    }
                    agent_record.output = {"status": "input_reference_invalid", "model_calls": 0}
                    raise
            messages = self._messages(
                planning_input, candidates, facts, area_hints, task_results,
            )
            output: DailyPlanModelOutput | None = None

            def raise_if_cancelled():
                if cancel_event is not None and cancel_event.is_set():
                    raise PlanningError(
                        "MODEL_CALL_CANCELLED", "每日安排生成已取消", step="daily_plan_model_call",
                    ) from None

            def record_local_recovery(snapshot, notices, status):
                safe_targets = sorted(
                    snapshot.targets,
                    key=lambda target: (target.day_index, target.activity_index),
                )
                diagnostics = [
                    {
                        "path": f"days[{target.day_index}].activities[{target.activity_index}]",
                        "reason_codes": list(target.reason_codes),
                        "guidance": safe_reason_text(target.reason_codes),
                        **local_repair_details.get(target.repair_id, {}),
                    }
                    for target in safe_targets[:8]
                ]
                with span(
                    "validation.daily_plan_local_repair",
                    "validation",
                    {"target_count": len(safe_targets)},
                ) as repair_record:
                    repair_record.output = {
                        "schema_version": 1,
                        "outcome": "repaired" if not notices else "unresolved",
                        "status": status,
                        "target_count": len(safe_targets),
                        "diagnostic_count": len(safe_targets),
                        "diagnostics_truncated": len(safe_targets) > len(diagnostics),
                        "diagnostics": diagnostics,
                        "recovered_count": len(snapshot.targets) - len(notices),
                        "unresolved_count": len(notices),
                        "reason_codes": sorted({item.reason_code for item in notices}),
                    }

            def repair_local(snapshot: ActivityRepairSnapshot) -> DailyPlanModelOutput:
                nonlocal model_calls, format_repairs, repair_budget_used, recovery_notices
                raise_if_cancelled()
                local_repair_details.clear()
                for target in snapshot.targets:
                    count = len(self._local_candidate_options(target, planning_input, candidates, facts, area_hints))
                    local_repair_details[target.repair_id] = {
                        "eligible_candidate_count": count,
                        "outcome": "no_verified_candidate" if count == 0 else "pending",
                    }
                if repair_budget_used:
                    for detail in local_repair_details.values():
                        if detail["eligible_candidate_count"]:
                            detail["outcome"] = "repair_budget_exhausted"
                    recovery_notices = tuple(self._recovery_notice(target) for target in snapshot.targets)
                    record_local_recovery(snapshot, recovery_notices, "repair_budget_exhausted")
                    raise_if_cancelled()
                    return snapshot.rebuild({})
                repair_budget_used = True
                try:
                    repaired, notices, local_calls, local_usage, call_attempted = self._repair_local_activities(
                        snapshot,
                        planning_input,
                        candidates,
                        facts,
                        area_hints,
                        deadline,
                        request_id=planning_input.request.request_id,
                        source_run_id=source_run_id,
                        cancel_event=cancel_event,
                        diagnostics=local_repair_details,
                    )
                except PlanningError as exc:
                    # 取消继续向上传递；其它局部模型错误保留已经隔离的
                    # 有效快照，并把坏活动交给F09作为缺项。
                    model_calls += getattr(exc, "model_calls", 0)
                    usage_totals.add(getattr(exc, "usage", None))
                    format_repairs += 1
                    _stats.sync(model_calls, format_repairs, usage_totals.value())
                    if exc.code == "MODEL_CALL_CANCELLED":
                        raise
                    for detail in local_repair_details.values():
                        if detail["eligible_candidate_count"]:
                            detail["outcome"] = "repair_call_unavailable"
                    recovery_notices = tuple(self._recovery_notice(target) for target in snapshot.targets)
                    raise_if_cancelled()
                    record_local_recovery(snapshot, recovery_notices, "repair_call_unavailable")
                    return snapshot.rebuild({})
                model_calls += local_calls
                if call_attempted:
                    format_repairs += 1
                recovery_notices = notices
                if local_calls > 0:
                    usage_totals.add(local_usage)
                _stats.sync(model_calls, format_repairs, usage_totals.value())
                raise_if_cancelled()
                record_local_recovery(
                    snapshot,
                    recovery_notices,
                    "repaired" if not recovery_notices else "partially_repaired",
                )
                return repaired

            for attempt in range(MAX_FORMAT_REPAIRS + 1):
                try:
                    response, raw_usage, finish_reason, call_started = self._invoke_once(
                        messages,
                        deadline,
                        attempt=attempt + 1,
                        request_id=planning_input.request.request_id,
                        source_run_id=source_run_id,
                        cancel_event=cancel_event,
                    )
                except PlanningError as exc:
                    model_calls += getattr(exc, "model_calls", 0)
                    usage_totals.add(getattr(exc, "usage", None))
                    _stats.sync(model_calls, format_repairs, usage_totals.value())
                    agent_record.output = {
                        "status": "model_error",
                        "error_code": exc.code,
                        "model_calls": model_calls,
                        "format_repairs": format_repairs,
                    }
                    raise
                if call_started:
                    model_calls += 1
                usage_totals.add(raw_usage)
                _stats.sync(model_calls, format_repairs, usage_totals.value())
                if finish_reason is not None and finish_reason != "stop":
                    with span(
                        "validation.daily_plan_completion",
                        "validation",
                        {"attempt": attempt + 1, "finish_reason": finish_reason},
                    ) as validation_record:
                        validation_record.output = {
                            "valid": False,
                            "reason": "truncated" if finish_reason == "length" else "unsupported_finish_reason",
                        }
                    if finish_reason != "length":
                        agent_record.output = {
                            "status": "incomplete_output",
                            "model_calls": model_calls,
                            "format_repairs": format_repairs,
                        }
                        raise PlanningError(
                            "PLAN_OUTPUT_INCOMPLETE",
                            "每日安排模型未正常完成 JSON 输出",
                            step="daily_plan_output_validation",
                        ) from None
                    if format_repairs >= MAX_FORMAT_REPAIRS:
                        agent_record.output = {
                            "status": "truncated_output",
                            "model_calls": model_calls,
                            "format_repairs": format_repairs,
                        }
                        raise PlanningError(
                            "PLAN_OUTPUT_TRUNCATED",
                            "每日安排模型输出达到长度上限，内容不完整",
                            step="daily_plan_output_validation",
                        ) from None
                    format_repairs += 1
                    repair_budget_used = True
                    _stats.sync(model_calls, format_repairs, usage_totals.value())
                    messages = [
                        *messages,
                        {"role": "assistant", "content": response if isinstance(response, str) else ""},
                        {
                            "role": "user",
                            "content": "上次输出因长度限制被截断。请仅补全为完整 JSON Schema 结构；保留已给出的行程语义，不新增事实或候选。",
                        },
                    ]
                    continue
                try:
                    with span(
                        "validation.daily_plan_output",
                        "validation",
                        {"attempt": attempt + 1, "request_id": planning_input.request.request_id},
                    ) as validation_record:
                        output, issues, issue_count, truncated = self._validate_model_output(
                            response, DailyPlanModelOutput,
                        )
                        if output is not None:
                            validation_record.output = {"valid": True, "day_count": len(output.days)}
                        else:
                            validation_record.output = {
                                "valid": False,
                                "issue_count": issue_count,
                                "diagnostics_truncated": truncated,
                                "issue_types": sorted({issue["type"] for issue in issues}),
                                "diagnostics": issues,
                            }
                            raise _InvalidModelOutput(issues) from None
                except _InvalidModelOutput as invalid:
                    # 活动字段错误只有在日期、有效时段和唯一 proposal_ref 都可
                    # 安全定位时才进入局部修复；其他结构问题沿用一次全局格式修复。
                    try:
                        raw_payload = _parse_json(response)
                    except (ValueError, TypeError):
                        raw_payload = None
                    initial_snapshot = build_structural_snapshot(raw_payload, planning_input)
                    if initial_snapshot is not None:
                        local_snapshot, _, _ = self._collect_local_activity_errors(
                            initial_snapshot.output,
                            planning_input,
                            candidates,
                            facts,
                            area_hints,
                            initial_snapshot=initial_snapshot,
                        )
                        if local_snapshot is not None:
                            # 如果前一轮已消耗唯一修复调用，只隔离坏活动并将其交给
                            # F09；不再发起第三次模型调用。
                            output = repair_local(local_snapshot)
                            break
                    if attempt >= MAX_FORMAT_REPAIRS:
                        agent_record.output = {
                            "status": "invalid_output",
                            "model_calls": model_calls,
                            "format_repairs": format_repairs,
                        }
                        raise PlanningError(
                            "PLAN_OUTPUT_INVALID",
                            "每日安排模型输出的 JSON 格式或字段结构无效",
                            step="daily_plan_output_validation",
                        ) from None
                    format_repairs += 1
                    repair_budget_used = True
                    _stats.sync(model_calls, format_repairs, usage_totals.value())
                    messages = [
                        *messages,
                        {"role": "assistant", "content": response if isinstance(response, str) else ""},
                        {
                            "role": "user",
                            "content": "上次回答不符合 JSON 格式或字段结构。只修复结构诊断中的问题，不改动行程语义，不增加事实。安全诊断如下："
                            + json.dumps(invalid.issues, ensure_ascii=False),
                        },
                    ]
                    continue
                break

            assert output is not None
            try:
                with span(
                    "validation.daily_plan_references",
                    "validation",
                    {"request_id": planning_input.request.request_id, "candidate_count": len(candidates)},
                ) as validation_record:
                    snapshot, proposal, issue_codes = self._collect_local_activity_errors(
                        output, planning_input, candidates, facts, area_hints,
                    )
                    if issue_codes is not None:
                        validation_record.output = {"valid": False, "issue_codes": list(issue_codes)}
                        raise _reference_error(issue_codes) from None
                    if snapshot is not None:
                        output = repair_local(snapshot)
                        proposal = self._validate_proposal_output(
                            output, planning_input, candidates, facts, area_hints,
                        )
                    if proposal is None:
                        # 局部扫描未能安全定位全局错误。复验后只返回固定错误码。
                        try:
                            proposal = self._validate_proposal_output(
                                output, planning_input, candidates, facts, area_hints,
                            )
                        except ContractValidationError as exc:
                            codes = sorted({issue.code for issue in exc.issues})
                            validation_record.output = {"valid": False, "issue_codes": codes}
                            raise _reference_error(codes) from None
                        except _InvalidReferences as exc:
                            validation_record.output = {"valid": False, "issue_codes": list(exc.codes)}
                            raise _reference_error(exc.codes) from None
                    validation_record.output = {
                        "valid": True,
                        "local_recovery_count": len(recovery_notices),
                        "recovery_reason_codes": sorted({item.reason_code for item in recovery_notices}),
                    }

                draft = self._build_draft(
                    proposal, planning_input, candidates, facts, area_hints, source_run_id,
                    plan_id=plan_id,
                )
            except PlanningError:
                agent_record.output = {
                    "status": "reference_invalid",
                    "model_calls": model_calls,
                    "format_repairs": format_repairs,
                }
                raise
            except Exception:
                agent_record.output = {
                    "status": "assembly_failed",
                    "model_calls": model_calls,
                    "format_repairs": format_repairs,
                }
                raise PlanningError(
                    "PLAN_GENERATION_FAILED",
                    "每日安排草稿生成失败",
                    step="daily_plan_draft_assembly",
                ) from None

            elapsed = round(time.monotonic() - started, 3)
            execution = DailyPlanExecution(
                proposal=proposal,
                draft=draft,
                usage=usage_totals.value(),
                model_calls=model_calls,
                format_repairs=format_repairs,
                elapsed_seconds=elapsed,
                recovery_notices=recovery_notices,
            )
            agent_record.output = {
                "status": "complete",
                "day_count": len(draft.days),
                "activity_count": sum(len(day.activities) for day in draft.days),
                "model_calls": model_calls,
                "format_repairs": format_repairs,
                "recovery_notice_count": len(recovery_notices),
                "elapsed_seconds": elapsed,
            }
            return execution

    def _collect_local_activity_errors(
        self,
        output: DailyPlanModelOutput,
        planning_input: PlanningInput,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        *,
        initial_snapshot: ActivityRepairSnapshot | None = None,
    ) -> tuple[ActivityRepairSnapshot | None, PlanProposal | None, tuple[str, ...] | None]:
        """逐层移除可定位的活动错误，直到全局提案剩余部分完全通过校验。

        返回 issue_codes 时表示存在全局或无法安全归属的错误。每轮必须至少剥离
        一个活动，循环次数受初版活动数限制；所有本地问题最终共用同一patch预算。
        """
        current = initial_snapshot.output if initial_snapshot is not None else output
        original_positions: dict[str, tuple[int, int]] = {}
        targets_by_ref: dict[str, ActivityRepairTarget] = {}

        if initial_snapshot is not None:
            for day_index, activity_index, activity in initial_snapshot.positioned_activities:
                original_positions[activity.proposal_ref] = (day_index, activity_index)
            for target in initial_snapshot.targets:
                original_positions[target.proposal_ref] = (target.day_index, target.activity_index)
                targets_by_ref[target.proposal_ref] = target
        else:
            for day_index, day in enumerate(current.days):
                for activity_index, activity in enumerate(day.activities):
                    if activity.proposal_ref in original_positions:
                        return None, None, ("duplicate_proposal_ref",)
                    original_positions[activity.proposal_ref] = (day_index, activity_index)

        max_passes = sum(len(day.activities) for day in current.days) + 1
        for _ in range(max_passes):
            try:
                proposal = self._validate_proposal_output(
                    current, planning_input, candidates, facts, area_hints,
                )
            except ContractValidationError as exc:
                issue_codes = tuple(sorted({issue.code for issue in exc.issues}))
                reasons = reference_reasons_from_issues(exc.issues)
            except _InvalidReferences as exc:
                issue_codes = exc.codes
                reasons = reference_reasons_from_selected(exc.codes, exc.activity_issue_codes)
            else:
                if not targets_by_ref:
                    return None, proposal, None
                ordered = sorted(
                    targets_by_ref.values(),
                    key=lambda target: (target.day_index, target.activity_index),
                )
                normalized_targets = tuple(
                    ActivityRepairTarget(
                        repair_id=f"repair-{index}",
                        day_index=target.day_index,
                        activity_index=target.activity_index,
                        day_id=target.day_id,
                        day_date=target.day_date,
                        slot_id=target.slot_id,
                        period=target.period,
                        expected_kind=target.expected_kind,
                        proposal_ref=target.proposal_ref,
                        reason_codes=target.reason_codes,
                    )
                    for index, target in enumerate(ordered)
                )
                positioned = []
                for day_index, day in enumerate(current.days):
                    for activity in day.activities:
                        position = original_positions.get(activity.proposal_ref)
                        if position is None or position[0] != day_index:
                            return None, None, ("activity_identity_unresolved",)
                        positioned.append((position[0], position[1], activity))
                snapshot = ActivityRepairSnapshot(
                    current,
                    normalized_targets,
                    tuple(sorted(positioned, key=lambda item: (item[0], item[1]))),
                )
                return snapshot, None, None

            if reasons is None:
                return None, None, issue_codes
            step = build_reference_snapshot(current, planning_input, reasons)
            if step is None or not step.targets:
                return None, None, issue_codes
            for target in step.targets:
                activity = current.days[target.day_index].activities[target.activity_index]
                original_position = original_positions.get(activity.proposal_ref)
                if original_position is None or original_position[0] != target.day_index:
                    return None, None, issue_codes
                previous = targets_by_ref.get(activity.proposal_ref)
                combined_reasons = set(target.reason_codes)
                if previous is not None:
                    combined_reasons.update(previous.reason_codes)
                targets_by_ref[activity.proposal_ref] = ActivityRepairTarget(
                    repair_id="",
                    day_index=original_position[0],
                    activity_index=original_position[1],
                    day_id=target.day_id,
                    day_date=target.day_date,
                    slot_id=target.slot_id,
                    period=target.period,
                    expected_kind=target.expected_kind,
                    proposal_ref=activity.proposal_ref,
                    reason_codes=tuple(sorted(combined_reasons)),
                )
            current = step.output

        return None, None, ("activity_localization_limit",)

    def _invoke_once(
        self, messages, deadline, *, attempt, request_id, source_run_id,
        cancel_event: Event | None = None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            error = PlanningError("MODEL_CALL_CANCELLED", "每日安排生成已取消", step="daily_plan_model_call")
            _attach_call_metadata(error, started=False)
            raise error from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = PlanningError("MODEL_TIMEOUT", "每日安排模型调用超时", step="daily_plan_model_call")
            _attach_call_metadata(error, started=False)
            raise error from None
        init_state, call_state = _ModelCallState(), _ModelCallState()
        safe_context = {
            "request_id": request_id,
            "run_id": source_run_id,
            "attempt": attempt,
            "timeout_seconds": wait_timeout(round(min(self.timeout_seconds, remaining), 3)),
        }
        copied_context = copy_context()
        future: Future = _MODEL_EXECUTOR.submit(
            copied_context.run,
            self._invoke_worker,
            messages,
            deadline,
            safe_context,
            init_state,
            call_state,
            cancel_event,
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                init_state.cancel_before_start()
                call_state.cancel_before_start()
                future.cancel()
                error = PlanningError(
                    "MODEL_CALL_CANCELLED", "每日安排生成已取消", step="daily_plan_model_call",
                )
                _attach_call_metadata(error, started=call_state.started)
                raise error from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                init_state.cancel_before_start()
                call_state.cancel_before_start()
                future.cancel()
                error = PlanningError(
                    "MODEL_TIMEOUT",
                    "每日安排模型调用超时",
                    step="daily_plan_model_call",
                )
                # Started calls have no provider usage until the response arrives.
                # If the request never started, usage is known to be zero.
                _attach_call_metadata(error, started=call_state.started)
                raise error from None
            done, _ = wait((future,), timeout=min(0.05, remaining))
            if done:
                break
        try:
            response, usage, finish_reason = future.result()
        except PlanningError as exc:
            safe_error = PlanningError(exc.code, str(exc), step=exc.step)
            _attach_call_metadata(safe_error, started=call_state.started)
            raise safe_error from None
        except _CallNotStarted:
            safe_error = PlanningError("MODEL_TIMEOUT", "每日安排模型调用超时", step="daily_plan_model_call")
            _attach_call_metadata(safe_error, started=call_state.started)
            raise safe_error from None
        except Exception as exc:
            code, message = self._model_error(exc)
            safe_error = PlanningError(code, message, step="daily_plan_model_call")
            _attach_call_metadata(safe_error, started=call_state.started)
            raise safe_error from None
        return response, usage, finish_reason, call_state.started

    def _invoke_worker(self, messages, deadline, safe_context, init_state, call_state, cancel_event=None):
        with span("llm.daily_plan_generation", "llm", safe_context) as llm_record:
            if cancel_event is not None and cancel_event.is_set():
                llm_record.output = {"status": "not_started", "reason": "cancelled"}
                raise PlanningError(
                    "MODEL_CALL_CANCELLED", "每日安排生成已取消", step="daily_plan_model_call",
                )
            if not init_state.try_start(deadline):
                llm_record.output = {"status": "not_started", "reason": "deadline"}
                raise _CallNotStarted()
            try:
                llm = self.llm if self.llm is not None else get_llm()
            except Exception:
                llm_record.output = {"status": "error", "code": "MODEL_INITIALIZATION_FAILED"}
                raise PlanningError(
                    "MODEL_INITIALIZATION_FAILED",
                    "每日安排模型初始化失败",
                    step="daily_plan_model_initialization",
                ) from None
            if cancel_event is not None and cancel_event.is_set():
                llm_record.output = {"status": "not_started", "reason": "cancelled"}
                raise PlanningError(
                    "MODEL_CALL_CANCELLED", "每日安排生成已取消", step="daily_plan_model_call",
                )
            if not call_state.try_start(deadline):
                llm_record.output = {"status": "not_started", "reason": "deadline"}
                raise _CallNotStarted()
            invoke_metadata = getattr(llm, "invoke_with_metadata", None)
            invoke_usage = getattr(llm, "invoke_with_usage", None)
            if not callable(invoke_metadata) and not callable(invoke_usage):
                llm_record.output = {"status": "error", "code": "MODEL_INTERFACE_INVALID"}
                raise PlanningError(
                    "MODEL_CALL_FAILED",
                    "每日安排模型调用失败",
                    step="daily_plan_model_call",
                ) from None
            remaining = max(0.001, deadline - time.monotonic())
            timeout = min(self.timeout_seconds, remaining)
            try:
                invoke_kwargs = {
                    "request_timeout": timeout,
                    "max_retries": 0,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                }
                if callable(invoke_metadata):
                    completion = invoke_metadata(messages, **invoke_kwargs)
                    if isinstance(completion, LLMCompletion):
                        response, usage, finish_reason = (
                            completion.text,
                            completion.usage,
                            completion.finish_reason,
                        )
                    else:
                        response = getattr(completion, "text")
                        usage = getattr(completion, "usage")
                        finish_reason = getattr(completion, "finish_reason", None)
                else:
                    response, usage = invoke_usage(messages, **invoke_kwargs)
                    # 旧 fake 与兼容模型接口不提供结束原因；仍完整校验 JSON 和引用。
                    finish_reason = None
            except Exception as exc:
                code, message = self._model_error(exc)
                llm_record.output = {"status": "error", "code": code}
                raise PlanningError(code, message, step="daily_plan_model_call") from None
            llm_record.usage = token_usage(usage)
            llm_record.output = {"status": "returned", "finish_reason": safe_finish_reason(finish_reason)}
            return response, usage, finish_reason


    def _repair_local_activities(
        self,
        snapshot: ActivityRepairSnapshot,
        planning_input: PlanningInput,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        deadline: float,
        *,
        request_id: str,
        source_run_id: str,
        cancel_event: Event | None,
        diagnostics: dict[str, dict[str, Any]],
    ):
        """对可定位坏活动执行一次patch调用；所有好活动由后端快照锁定。"""
        place_by_fact = {place.fact_id: place for place in facts.places}
        options_by_target: dict[str, dict[str, CandidateRef]] = {}
        target_records = []
        for target in snapshot.targets:
            options = self._local_candidate_options(target, planning_input, candidates, facts, area_hints)
            records = []
            for candidate in options.values():
                place = place_by_fact[candidate.place_fact_id]
                records.append({
                    "candidate_id": candidate.candidate_id,
                    "category": candidate.category,
                    "name": place.name,
                    "city": place.city,
                    "type_name": place.type_name,
                    "allowed_requirement_ids": list(candidate.requirement_ids),
                })
            if not records:
                continue
            options_by_target[target.repair_id] = options
            target_records.append({
                "repair_id": target.repair_id,
                "day_id": target.day_id,
                "day_date": target.day_date.isoformat(),
                "slot_id": target.slot_id,
                "period": target.period,
                "required_kind": target.expected_kind,
                "condition": safe_reason_text(target.reason_codes),
                "available_verified_candidates": records,
            })

        if not target_records:
            return (
                snapshot.rebuild({}),
                tuple(self._recovery_notice(target) for target in snapshot.targets),
                0,
                None,
                False,
            )

        request = planning_input.request
        repair_messages = [
            {
                "role": "system",
                "content": """你只修复列出的初版活动。只返回 repairs 数组，不输出整份行程。
每个 repair_id 只能提交一个 replace 或 leave_unfilled。replace 只能从该目标列出的 selectable 已核实候选中选择；kind必须等于required_kind，requirement_ids只能取所选候选的allowed_requirement_ids，不确定时留空。不要提供地点名称、日期、时段、proposal_ref、说明文字或其他字段。
后端会把patch放回原活动位置，并锁定所有未列出的有效活动、原顺序、主题、天气和住宿。没有合适候选时使用leave_unfilled。""",
            },
            {
                "role": "user",
                "content": json.dumps({
                    "schema": ActivityRepairOutput.model_json_schema(),
                    "trip_context": {
                        "city": request.city,
                        "preferences": list(request.preferences),
                        "transport_preferences": list(request.transport_preferences),
                        "lodging_query": request.lodging_query,
                        "remarks": request.remarks,
                        "hard_requirements": [
                            {"requirement_id": item.requirement_id, "kind": item.kind, "text": item.text}
                            for item in planning_input.requirements.hard_requirements
                        ],
                    },
                    "repair_targets": target_records,
                }, ensure_ascii=False),
            },
        ]
        response = None
        call_started = False
        raw_usage = None
        try:
            response, raw_usage, finish_reason, call_started = self._invoke_once(
                repair_messages,
                deadline,
                attempt=2,
                request_id=request_id,
                source_run_id=source_run_id,
                cancel_event=cancel_event,
            )
        except PlanningError:
            # The caller preserves the isolated snapshot for non-cancellation errors.
            raise

        parsed = parse_patch_response(response, set(options_by_target)) if finish_reason in (None, "stop") else {}
        replacements: dict[str, ProposalActivity] = {}
        for target in snapshot.targets:
            if target.repair_id not in options_by_target:
                continue
            detail = diagnostics[target.repair_id]
            item = parsed.get(target.repair_id)
            if item is None:
                detail["outcome"] = "invalid_or_missing_patch"
                continue
            if item.action != "replace" or item.replacement is None:
                detail["outcome"] = "model_left_unfilled"
                continue
            candidate = options_by_target.get(target.repair_id, {}).get(item.replacement.candidate_ref)
            if candidate is None or item.replacement.kind != target.expected_kind:
                detail["outcome"] = "patch_candidate_invalid"
                continue
            requirement_ids = item.replacement.requirement_ids
            if len(requirement_ids) != len(set(requirement_ids)):
                detail["outcome"] = "patch_requirement_invalid"
                continue
            if not set(requirement_ids).issubset(set(candidate.requirement_ids)):
                detail["outcome"] = "patch_requirement_invalid"
                continue
            try:
                replacement = ProposalActivity(
                    proposal_ref=target.proposal_ref,
                    candidate_ref=candidate.candidate_id,
                    slot_id=target.slot_id,
                    kind=target.expected_kind,
                    requirement_ids=requirement_ids,
                    recommendation_reason="根据已核实候选修复的活动。",
                )
            except (ValidationError, ValueError):
                detail["outcome"] = "invalid_or_missing_patch"
                continue
            replacements[target.repair_id] = replacement
            detail["outcome"] = "repaired"

        notices = tuple(
            self._recovery_notice(target)
            for target in snapshot.targets
            if target.repair_id not in replacements
        )
        return snapshot.rebuild(replacements), notices, int(call_started), raw_usage, True

    def _local_candidate_options(self, target, planning_input, candidates, facts, area_hints):
        places = {place.fact_id: place for place in facts.places}
        return {
            candidate.candidate_id: candidate for candidate in candidates
            if candidate.category == target.expected_kind
            and candidate_allows_slot(candidate, target.slot_id)
            and self._candidate_rejection(
                candidate, places.get(candidate.place_fact_id), planning_input, facts, area_hints,
            ) is None
        }

    @staticmethod
    def _recovery_notice(target: ActivityRepairTarget) -> DailyPlanRecoveryNotice:
        return DailyPlanRecoveryNotice(
            day_id=target.day_id,
            slot_id=target.slot_id,
            activity_index=target.activity_index,
            reason_code=notice_reason_code(target),
        )


    @classmethod
    def _validate_model_output(cls, response, output_model):
        try:
            payload = _parse_json(response)
        except (ValueError, TypeError):
            return None, [cls._format_diagnostic("json_invalid", "output")], 1, False
        try:
            return output_model.model_validate(payload), [], 0, False
        except ValidationError as exc:
            raw_errors = exc.errors(
                include_input=False,
                include_context=False,
                include_url=False,
            )
            diagnostics = []
            for error in raw_errors[:_MAX_FORMAT_DIAGNOSTICS]:
                raw_type = error.get("type")
                safe_type = (
                    raw_type
                    if isinstance(raw_type, str) and raw_type in _SAFE_VALIDATION_TYPES
                    else "invalid_structure"
                )
                path = cls._safe_validation_path(output_model, error.get("loc", ()))
                diagnostics.append(cls._format_diagnostic(safe_type, path))
            if not diagnostics:
                diagnostics.append(cls._format_diagnostic("invalid_structure", "output"))
            if len(raw_errors) > _MAX_FORMAT_DIAGNOSTICS:
                diagnostics.append(cls._format_diagnostic("additional_issues", "output"))
            return None, diagnostics, len(raw_errors), len(raw_errors) > _MAX_FORMAT_DIAGNOSTICS
        except (ValueError, TypeError):
            return None, [cls._format_diagnostic("invalid_structure", "output")], 1, False

    @staticmethod
    def _safe_validation_path(output_model, location):
        context = "initial" if output_model is DailyPlanModelOutput else "revision"
        path: list[str] = []
        segments = location if isinstance(location, (tuple, list)) else ()
        for segment in tuple(segments)[:12]:
            if isinstance(segment, int) and not isinstance(segment, bool):
                index = f"[{segment}]" if 0 <= segment < 10_000 else "[item]"
                if path:
                    path[-1] += index
                else:
                    path.append(f"output{index}")
                continue
            if not isinstance(segment, str) or segment not in _SAFE_PATH_FIELDS.get(context, set()):
                path.append("unknown_field")
                break
            path.append(segment)
            context = _SAFE_PATH_CHILD.get((context, segment), "")
        return ".".join(path) or "output"

    @staticmethod
    def _format_diagnostic(issue_type, path):
        issue_group = _VALIDATION_TYPE_GROUPS.get(issue_type, "invalid_structure")
        field = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if issue_group == "invalid_combination" and ".activities[" in path:
            guidance = _SAFE_FORMAT_INSTRUCTIONS["invalid_combination"]
        elif issue_group == "missing_required_field" and field == "candidate_ref":
            guidance = "为地点活动引用输入中有效候选；free_time不得引用POI候选。"
        elif issue_group == "missing_required_field" and field == "slot_id":
            guidance = "使用该日期输入中可用的slot_id。"
        elif issue_group == "invalid_choice" and field == "kind":
            guidance = "kind只能使用sightseeing、meal或free_time。"
        elif issue_group == "invalid_date" and field == "day_date":
            guidance = _SAFE_FORMAT_INSTRUCTIONS["invalid_date"]
        else:
            guidance = _SAFE_FORMAT_INSTRUCTIONS.get(
                issue_group,
                _SAFE_FORMAT_INSTRUCTIONS["invalid_structure"],
            )
        return {"path": path, "type": issue_type, "guidance": guidance}

    @staticmethod
    def _model_error(exc):
        seen, names, statuses = set(), set(), set()
        current = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            names.add(type(current).__name__)
            status = getattr(current, "status_code", None)
            if status is not None:
                statuses.add(status)
            current = current.__cause__ or current.__context__
        if names & {"APITimeoutError", "TimeoutException", "TimeoutError", "ConnectTimeout", "ReadTimeout"}:
            return "MODEL_TIMEOUT", "每日安排模型调用超时"
        if names & {"AuthenticationError", "PermissionDeniedError"} or statuses & {401, 403}:
            return "MODEL_AUTHENTICATION_FAILED", "每日安排模型认证失败"
        return "MODEL_CALL_FAILED", "每日安排模型调用失败"


class _InvalidModelOutput(Exception):
    def __init__(self, issues):
        self.issues = issues


class _CallNotStarted(Exception):
    pass


class _UsageAccumulator:
    def __init__(self):
        self._totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._known = {key: True for key in self._totals}

    def add(self, raw_usage):
        current = token_usage(raw_usage)
        if current is None:
            self._known = {key: False for key in self._known}
            return
        for key in self._totals:
            value = current.get(key)
            if value is None:
                self._known[key] = False
            else:
                self._totals[key] += value

    def value(self):
        if not any(self._known.values()):
            return None
        return {
            key: self._totals[key] if self._known[key] else None
            for key in self._totals
        }


class _GenerationStats:
    def __init__(self):
        self.model_calls = 0
        self.format_repairs = 0
        self.usage = _zero_usage()

    def sync(self, model_calls, format_repairs, usage):
        self.model_calls = model_calls
        self.format_repairs = format_repairs
        self.usage = usage

    def attach(self, error):
        error.model_calls = self.model_calls
        error.format_repairs = self.format_repairs
        error.usage = self.usage


def _attach_call_metadata(error: PlanningError, *, started: bool) -> None:
    error.model_calls = int(started)
    error.usage = None if started else _zero_usage()


def _zero_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
