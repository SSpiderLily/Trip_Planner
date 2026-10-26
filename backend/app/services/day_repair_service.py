"""按日期补查、局部修订及重检的可注入编排服务。"""
from __future__ import annotations

import inspect
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
from uuid import uuid4
from pydantic import ValidationError, model_validator
from app.planning.repair_policy import RECOVERABLE_REFERENCE_CODES

from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    DayRevision,
    DraftActivity,
    DraftDay,
    FactStore,
    PlanDraft,
    PlanningInput,
    ProposalActivity,
    ProposalDay,
    RepairState,
    TaskResult,
    ValidationIssue,
    ValidationReport,
)
from app.models.planning_v4.base import ContractModel
from app.services.planning_candidate_policy import candidate_allows_slot
from app.services.planning_lodging_policy import city_for_days
from app.services.planning_v4_validation import is_required_place_gap
from app.planning.fact_policy import _category_status, _city_matches, _dining_category
from app.services.planning_diagnostics import repair_stage, safe_error_code
from app.services.observation_service import span
from app.services.planning_v4_validator import ContractValidationError, validate_day_revision


MAX_REPAIR_ROUNDS_PER_DAY = 3
MAX_TECHNICAL_FAILURES_PER_DAY = 3
RECOVERABLE_COLLECTION_CODES = RECOVERABLE_REFERENCE_CODES | {
    "MASTER_LLM_FAILED", "MODEL_CALL_FAILED", "REPAIR_COLLECTION_TIMEOUT", "REPAIR_MAP_FAILED",
}
from app.models.planning_repair import (RepairStop, CollectionStop, IssueDayAssignment, DayRepairAttempt, DayRepairResult)


class DayRepairError(RuntimeError):
    """回调的安全失败类型；只携带可公开的错误代码。"""

    def __init__(
        self,
        code: str,
        *,
        deadline: bool = False,
        global_deadline: bool = False,
        fatal: bool = False,
    ):
        self.code = code
        self.deadline = deadline
        self.global_deadline = global_deadline
        self.fatal = fatal
        super().__init__(code)


class DayRepairInputError(ValueError):
    """修正输入与当前规划版本不匹配。"""


class DayRepairCollectionTarget(ContractModel):
    """单一日期内，一个缺项对应的严格搜索边界。"""

    day_id: str
    role: Literal["attractions", "dining"]
    issue_ids: tuple[str, ...]
    slot_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.issue_ids or not self.slot_ids:
            raise ValueError("collection target must bind an issue and eligible slot")
        for values in (self.issue_ids, self.slot_ids, self.requirement_ids, self.candidate_ids):
            if len(values) != len(set(values)):
                raise ValueError("collection target identities must be unique")
        return self


class DayRepairCollectionRequest(ContractModel):
    planning_input: PlanningInput
    draft: PlanDraft
    day: DraftDay
    issues: tuple[ValidationIssue, ...]
    assignments: tuple[IssueDayAssignment, ...]
    round_id: str
    round_number: int = 1
    remaining_seconds: float | None = None
    targets: tuple[DayRepairCollectionTarget, ...] = ()
    known_fact_ids: tuple[str, ...] = ()
    fact_store: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()


class DayRepairCollectionResult(ContractModel):
    """collect 返回累积快照；no_progress/deadline 可携带已核实的部分结果。"""

    stop_reason: CollectionStop
    fact_store: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    error_code: str | None = None

    @classmethod
    def empty(cls, facts: FactStore, candidates, area_hints, task_results):
        return cls(
            stop_reason="target_met",
            fact_store=facts,
            candidates=tuple(candidates),
            area_hints=tuple(area_hints),
            task_results=tuple(task_results),
        )


class DayRevisionRequest(ContractModel):
    planning_input: PlanningInput
    draft: PlanDraft
    day: DraftDay
    report: ValidationReport
    issues: tuple[ValidationIssue, ...]
    assignments: tuple[IssueDayAssignment, ...]
    round_id: str
    round_number: int = 1
    remaining_seconds: float | None = None
    preserve_activity_ids: tuple[str, ...] = ()
    modifiable_activity_ids: tuple[str, ...] = ()
    removable_activity_ids: tuple[str, ...] = ()
    collection_stop_reason: CollectionStop = "target_met"
    fact_store: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()


class DayRepairRecalculationRequest(ContractModel):
    round_number: int = 1
    planning_input: PlanningInput
    draft: PlanDraft
    facts: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    repair_states: tuple[RepairState, ...] = ()
    day_id: str
    round_id: str
    remaining_seconds: float | None = None


class DayRepairRecalculation(ContractModel):
    """目标日交通摘要与地点覆盖报告的重算结果。"""

    draft: PlanDraft
    facts: FactStore
    report: ValidationReport


CollectCallback = Callable[[DayRepairCollectionRequest], Awaitable[DayRepairCollectionResult]]
ReviseCallback = Callable[[DayRevisionRequest], Awaitable[DayRevision]]
RecalculateCallback = Callable[[DayRepairRecalculationRequest], Awaitable[DayRepairRecalculation]]


@dataclass(frozen=True)
class _Policy:
    preserve_ids: tuple[str, ...]
    modifiable_ids: tuple[str, ...]
    removable_ids: tuple[str, ...]


class DayRepairService:
    """按日期推进修复状态；I/O 适配器由调用方注入，单次失败不会提交半份日修订。"""

    def __init__(
        self,
        *,
        collect: CollectCallback,
        revise: ReviseCallback,
        recalculate: RecalculateCallback,
        id_factory: Callable[[], str] | None = None,
    ):
        self.collect = collect
        self.revise = revise
        self.recalculate = recalculate
        self.id_factory = id_factory or (lambda: str(uuid4()))

    async def run_async(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        facts: FactStore,
        candidates: tuple[CandidateRef, ...],
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        report: ValidationReport,
        *,
        global_deadline_monotonic: float | None = None,
    ) -> DayRepairResult:
        self._validate_run_binding(planning_input, draft, report)
        if global_deadline_monotonic is not None and (
            isinstance(global_deadline_monotonic, bool)
            or not isinstance(global_deadline_monotonic, (int, float))
            or not math.isfinite(global_deadline_monotonic)
        ):
            raise DayRepairInputError("global_deadline_monotonic must be a finite timestamp")
        max_rounds = min(MAX_REPAIR_ROUNDS_PER_DAY, planning_input.rules.max_repair_rounds_per_day)
        states = self._state_map(draft, report)
        attempts: list[DayRepairAttempt] = []
        assignments_log: list[IssueDayAssignment] = []
        unassigned: set[str] = set()

        # 中断属于技术失败；保留已完成的业务轮次，不扣未完成的尝试。
        interrupted_assignments = assign_repair_issues(planning_input, report, facts, candidates)
        assignments_log.extend(interrupted_assignments)
        for day_id, state in tuple(states.items()):
            if state.status != "active":
                continue
            completed = state.completed_rounds
            prior_attempt_id = state.active_round_id or self.id_factory()
            interrupted_issues = self._issues_for_day(report, interrupted_assignments, day_id)
            policy = derive_activity_policy(draft, day_id, tuple(item.issue for item in interrupted_issues))
            attempts.append(self._attempt(
                day_id,
                prior_attempt_id,
                state.completed_rounds + 1,
                interrupted_issues,
                policy,
                "deadline",
                "deadline",
                "INTERRUPTED_REPAIR_ATTEMPT",
            ))
            states[day_id] = RepairState(
                day_id=day_id,
                completed_rounds=completed,
                status="stopped",
                stop_reason="deadline",
            )

        for constraint in planning_input.days:
            day_id = constraint.day_id
            while True:
                if _deadline_expired(global_deadline_monotonic):
                    return self._deadline_result(
                        planning_input, draft, facts, candidates, area_hints,
                        task_results, report, states, assignments_log,
                        attempts, unassigned,
                    )
                day = self._day(draft, day_id)
                day_assignments = assign_repair_issues(planning_input, report, facts, candidates)
                assignments_log.extend(item for item in day_assignments if item not in assignments_log)
                unassigned.clear()
                unassigned.update(item.issue_id for item in day_assignments if item.assigned_day_id is None)
                issues = self._issues_for_day(report, day_assignments, day_id)
                state = states[day_id]

                if not issues:
                    state = RepairState(
                        day_id=day_id,
                        completed_rounds=state.completed_rounds,
                        status="complete",
                        stop_reason="complete",
                    )
                    states[day_id] = state
                    break
                if state.status == "stopped":
                    break
                if state.completed_rounds >= max_rounds:
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=state.completed_rounds,
                        status="stopped",
                        stop_reason="round_limit",
                    )
                    break

                round_number = state.completed_rounds + 1
                round_id = self.id_factory()
                repair_metadata = {
                    "plan_id": draft.plan_id, "day_id": day_id, "round_id": round_id,
                    "round_number": round_number, "base_revision": draft.revision,
                    "issue_count": len(issues),
                }
                active = RepairState(
                    day_id=day_id,
                    completed_rounds=state.completed_rounds,
                    active_round_id=round_id,
                    status="active",
                )
                assigned_issues = tuple(item.issue for item in issues)
                policy = derive_activity_policy(draft, day_id, assigned_issues)
                needs_collection, collection_targets = _collection_scope(
                    planning_input, constraint, day, assigned_issues, facts, candidates,
                )

                if needs_collection:
                    collection_issue_ids = {
                        issue_id
                        for target in collection_targets
                        for issue_id in target.issue_ids
                    }
                    try:
                        with repair_stage("collect", repair_metadata) as collection_record:
                            collected = await _await(self.collect(DayRepairCollectionRequest(
                                planning_input=planning_input,
                                draft=draft,
                                day=day,
                                issues=tuple(issue for issue in assigned_issues if issue.issue_id in collection_issue_ids),
                                assignments=tuple(
                                    item.assignment for item in issues
                                    if item.issue.issue_id in collection_issue_ids
                                ),
                                round_id=round_id,
                                round_number=round_number,
                                remaining_seconds=_remaining_seconds(global_deadline_monotonic),
                                targets=collection_targets,
                                known_fact_ids=tuple(_known_fact_ids(facts)),
                                fact_store=facts,
                                candidates=candidates,
                                area_hints=area_hints,
                                task_results=task_results,
                            )))
                            collection_record.output = {
                                "valid": collected.stop_reason != "technical_error",
                                "stop_reason": collected.stop_reason,
                                "error_code": safe_error_code(collected.error_code) if collected.error_code else None,
                                "candidate_count": len(collected.candidates),
                            }
                    except DayRepairError as exc:
                        if exc.global_deadline or _deadline_expired(global_deadline_monotonic):
                            return self._deadline_result(
                                planning_input, draft, facts, candidates, area_hints,
                                task_results, report, states, assignments_log,
                                attempts, unassigned,
                                active=(day_id, round_id, round_number, issues, policy,
                                        state.completed_rounds + 1, exc.code),
                            )
                        if exc.fatal:
                            return self._technical_failure(
                                draft, facts, candidates, area_hints, task_results,
                                report, states, attempts, assignments_log,
                                unassigned, day_id, round_id, round_number, issues,
                                policy, state.completed_rounds + 1, exc.code,
                            )
                        if exc.deadline:
                            collected = DayRepairCollectionResult(
                                stop_reason="technical_error", fact_store=facts,
                                candidates=candidates, area_hints=area_hints,
                                task_results=task_results,
                                error_code="REPAIR_COLLECTION_TIMEOUT",
                            )
                        else:
                            if self._record_failed_round(
                                states, attempts, day_id, round_id, round_number,
                                issues, policy, state.completed_rounds + 1,
                                max_rounds, collected_stop="technical_error",
                                error_code=exc.code,
                            ):
                                break
                            continue
                    except DayRepairInputError as exc:
                        return self._technical_failure(
                            draft, facts, candidates, area_hints, task_results,
                            report, states, attempts, assignments_log,
                            unassigned, day_id, round_id, round_number, issues,
                            policy, state.completed_rounds + 1,
                            _safe_revision_error(exc),
                        )
                    except Exception as exc:
                        if _deadline_expired(global_deadline_monotonic):
                            return self._deadline_result(
                                planning_input, draft, facts, candidates, area_hints,
                                task_results, report, states, assignments_log,
                                attempts, unassigned,
                                active=(day_id, round_id, round_number, issues, policy,
                                        state.completed_rounds + 1, _safe_callback_error(exc)),
                            )
                        if self._record_failed_round(
                            states, attempts, day_id, round_id, round_number,
                            issues, policy, state.completed_rounds + 1,
                            max_rounds, collected_stop="technical_error",
                            error_code=_safe_callback_error(exc),
                        ):
                            break
                        continue
                    try:
                        with repair_stage("merge_collection", repair_metadata):
                            next_facts = _merge_facts(facts, collected.fact_store)
                            next_candidates = _merge_by_id(candidates, collected.candidates, "candidate_id", "candidate")
                            next_areas = _merge_by_id(area_hints, collected.area_hints, "area_hint_id", "area hint")
                            next_tasks = _merge_by_id(task_results, collected.task_results, "task_id", "task result", replace=True)
                    except DayRepairInputError:
                        return self._technical_failure(
                            draft, facts, candidates, area_hints, task_results,
                            report, states, attempts, assignments_log,
                            unassigned, day_id, round_id, round_number, issues,
                            policy, state.completed_rounds + 1, "DAY_REPAIR_FACT_CONFLICT",
                        )
                    if collected.stop_reason == "deadline":
                        collected = collected.model_copy(update={
                            "stop_reason": "technical_error",
                            "error_code": "REPAIR_COLLECTION_TIMEOUT",
                        })
                    if collected.stop_reason == "technical_error":
                        # 保留通过合并校验的快照；接口或可恢复引用错误若新增有效覆盖，继续修订。
                        facts, candidates, area_hints, task_results = (
                            next_facts, next_candidates, next_areas, next_tasks,
                        )
                        can_revise = False
                        if collected.error_code in RECOVERABLE_COLLECTION_CODES:
                            _, remaining_targets = _collection_scope(
                                planning_input, constraint, day, assigned_issues, facts, candidates,
                            )
                            can_revise = len(remaining_targets) < len(collection_targets)
                            with span("repair.collection_recovery", "validation", repair_metadata) as recovery_record:
                                recovery_record.output = {
                                    "schema_version": 1, "stage": "repair.collect",
                                    "outcome": "continue_revision" if can_revise else "skip_revision",
                                    "error_code": collected.error_code,
                                    "targets_with_new_usable_candidates": len(collection_targets) - len(remaining_targets),
                                    "remaining_collection_targets": len(remaining_targets),
                                }
                        if not can_revise:
                            if self._record_failed_round(
                                states, attempts, day_id, round_id, round_number,
                                issues, policy, state.completed_rounds + 1,
                                max_rounds, collected_stop="technical_error",
                                error_code=collected.error_code or "DAY_REPAIR_COLLECT_FAILED",
                            ):
                                break
                            continue
                else:
                    collected = DayRepairCollectionResult.empty(facts, candidates, area_hints, task_results)
                    next_facts, next_candidates, next_areas, next_tasks = facts, candidates, area_hints, task_results

                if _deadline_expired(global_deadline_monotonic):
                    return self._deadline_result(
                        planning_input, draft, next_facts, next_candidates,
                        next_areas, next_tasks, report, states, assignments_log,
                        attempts, unassigned,
                        active=(day_id, round_id, round_number, issues, policy,
                                state.completed_rounds + 1, "GLOBAL_TECHNICAL_DEADLINE"),
                    )

                # A per-call collection timeout can still contribute its verified partial
                # facts to this date-only model call. Only fatal input corruption stops all work.
                try:
                    with repair_stage("revise", repair_metadata):
                        revision = await _await(self.revise(DayRevisionRequest(
                            planning_input=planning_input,
                            draft=draft,
                            day=day,
                            report=report,
                            issues=assigned_issues,
                            assignments=tuple(item.assignment for item in issues),
                            round_id=round_id,
                            round_number=round_number,
                            remaining_seconds=_remaining_seconds(global_deadline_monotonic),
                            preserve_activity_ids=policy.preserve_ids,
                            modifiable_activity_ids=policy.modifiable_ids,
                            removable_activity_ids=policy.removable_ids,
                            collection_stop_reason=collected.stop_reason,
                            fact_store=next_facts,
                            candidates=next_candidates,
                            area_hints=next_areas,
                            task_results=next_tasks,
                        )))
                except DayRepairError as exc:
                    if exc.global_deadline or _deadline_expired(global_deadline_monotonic):
                        return self._deadline_result(
                            planning_input, draft, next_facts, next_candidates,
                            next_areas, next_tasks, report, states,
                            assignments_log, attempts, unassigned,
                            active=(day_id, round_id, round_number, issues, policy,
                                    state.completed_rounds + 1, exc.code),
                        )
                    if exc.fatal:
                        return self._technical_failure(
                            draft, facts, candidates, area_hints, task_results,
                            report, states, attempts, assignments_log,
                            unassigned, day_id, round_id, round_number, issues,
                            policy, state.completed_rounds + 1, exc.code,
                        )
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    if self._record_failed_round(
                        states, attempts, day_id, round_id, round_number,
                        issues, policy, state.completed_rounds + 1, max_rounds,
                        collected_stop=collected.stop_reason, error_code=exc.code,
                    ):
                        break
                    continue
                except Exception as exc:
                    if _deadline_expired(global_deadline_monotonic):
                        return self._deadline_result(
                            planning_input, draft, next_facts, next_candidates,
                            next_areas, next_tasks, report, states,
                            assignments_log, attempts, unassigned,
                            active=(day_id, round_id, round_number, issues, policy,
                                    state.completed_rounds + 1, _safe_callback_error(exc)),
                        )
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    if self._record_failed_round(
                        states, attempts, day_id, round_id, round_number,
                        issues, policy, state.completed_rounds + 1, max_rounds,
                        collected_stop=collected.stop_reason,
                        error_code=_safe_callback_error(exc),
                    ):
                        break
                    continue

                try:
                    with repair_stage("apply_revision", repair_metadata):
                        revised_draft, changed = apply_day_revision(
                            revision,
                            draft,
                            report,
                            planning_input,
                            next_candidates,
                            next_facts,
                            expected_day_id=day_id,
                            expected_source_issue_ids=tuple(item.issue.issue_id for item in issues),
                            preserve_activity_ids=policy.preserve_ids,
                            allowed_change_activity_ids=policy.modifiable_ids,
                            removable_activity_ids=policy.removable_ids,
                        )
                except (ContractValidationError, DayRepairInputError, ValueError) as exc:
                    count = state.completed_rounds + 1
                    attempts.append(self._attempt(
                        day_id, round_id, round_number, issues, policy,
                        "invalid_revision", collected.stop_reason,
                        _safe_revision_error(exc),
                    ))
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=min(max_rounds, count),
                        status="open" if count < max_rounds else "stopped",
                        stop_reason=None if count < max_rounds else "round_limit",
                    )
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    if count >= max_rounds:
                        break
                    continue

                count = state.completed_rounds + 1
                if not changed:
                    attempts.append(self._attempt(
                        day_id, round_id, round_number, issues, policy,
                        "no_progress", collected.stop_reason,
                    ))
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=min(max_rounds, count),
                        status="open" if count < max_rounds else "stopped",
                        stop_reason=None if count < max_rounds else "round_limit",
                    )
                    if count >= max_rounds:
                        break
                    continue

                provisional_state = RepairState(
                    day_id=day_id,
                    completed_rounds=state.completed_rounds,
                    active_round_id=round_id,
                    status="active",
                )
                try:
                    with repair_stage("recalculate", repair_metadata):
                        recalculated = await _await(self.recalculate(DayRepairRecalculationRequest(
                            planning_input=planning_input,
                            draft=revised_draft,
                            facts=next_facts,
                            candidates=next_candidates,
                            area_hints=next_areas,
                            task_results=next_tasks,
                            repair_states=tuple(
                                provisional_state if item.day_id == day_id else item
                                for item in self._ordered_states(states, planning_input)
                            ),
                            day_id=day_id,
                            round_id=round_id,
                            round_number=round_number,
                            remaining_seconds=_remaining_seconds(global_deadline_monotonic),
                        )))
                    with repair_stage("accept_recalculation", repair_metadata):
                        self._validate_recalculation(draft, revised_draft, recalculated, day_id)
                except DayRepairError as exc:
                    if exc.global_deadline or _deadline_expired(global_deadline_monotonic):
                        return self._deadline_result(
                            planning_input, draft, next_facts, next_candidates,
                            next_areas, next_tasks, report, states,
                            assignments_log, attempts, unassigned,
                            active=(day_id, round_id, round_number, issues, policy,
                                    count, exc.code),
                        )
                    if exc.fatal:
                        return self._technical_failure(
                            draft, facts, candidates, area_hints, task_results,
                            report, states, attempts, assignments_log,
                            unassigned, day_id, round_id, round_number, issues,
                            policy, count, exc.code,
                        )
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    if self._record_failed_round(
                        states, attempts, day_id, round_id, round_number,
                        issues, policy, count, max_rounds,
                        collected_stop=collected.stop_reason, error_code=exc.code,
                    ):
                        break
                    continue
                except Exception as exc:
                    if _deadline_expired(global_deadline_monotonic):
                        return self._deadline_result(
                            planning_input, draft, next_facts, next_candidates,
                            next_areas, next_tasks, report, states,
                            assignments_log, attempts, unassigned,
                            active=(day_id, round_id, round_number, issues, policy,
                                    count, _safe_callback_error(exc)),
                        )
                    facts, candidates, area_hints, task_results = (
                        next_facts, next_candidates, next_areas, next_tasks,
                    )
                    error_code = (
                        "DAY_REPAIR_RECALCULATION_INVALID"
                        if isinstance(exc, DayRepairInputError)
                        else _safe_callback_error(exc)
                    )
                    if self._record_failed_round(
                        states, attempts, day_id, round_id, round_number,
                        issues, policy, count, max_rounds,
                        collected_stop=collected.stop_reason,
                        error_code=error_code,
                        outcome="technical_error",
                    ):
                        break
                    continue

                try:
                    with repair_stage("merge_recalculated_facts", repair_metadata):
                        next_total_facts = _merge_facts(next_facts, recalculated.facts)
                except DayRepairInputError:
                    return self._technical_failure(
                        draft, facts, candidates, area_hints, task_results,
                        report, states, attempts, assignments_log,
                        unassigned, day_id, round_id, round_number, issues,
                        policy, count, "DAY_REPAIR_FACT_CONFLICT",
                    )
                draft, facts, report = (
                    recalculated.draft, next_total_facts,
                    recalculated.report,
                )
                candidates, area_hints, task_results = next_candidates, next_areas, next_tasks
                remaining = self._issues_for_day(
                    report,
                    assign_repair_issues(
                        planning_input, report, facts, candidates,
                    ),
                    day_id,
                )
                attempts.append(self._attempt(
                    day_id, round_id, round_number, issues, policy,
                    "applied", collected.stop_reason,
                ))
                if not remaining:
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=min(max_rounds, count),
                        status="complete",
                        stop_reason="complete",
                    )
                elif count >= max_rounds:
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=max_rounds,
                        status="stopped",
                        stop_reason="round_limit",
                    )
                    break
                else:
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=count,
                        status="open",
                    )

        final_states = self._ordered_states(states, planning_input)
        report = _replace_report_states(report, final_states)
        unresolved = any(is_required_place_gap(planning_input, issue) for issue in report.issues)
        partial = unresolved or bool(unassigned) or any(
            state.status == "stopped" and state.stop_reason in {"round_limit", "deadline", "technical_error"}
            for state in final_states
        )
        stop_reason: RepairStop | None = None
        if partial:
            stop_reason = next((
                state.stop_reason for state in final_states
                if state.status == "stopped" and state.stop_reason in {"deadline", "round_limit", "technical_error"}
            ), None)
        return self._finish(
            "partial" if partial else "complete",
            draft, facts, candidates, area_hints, task_results, report,
            states, assignments_log, attempts, unassigned, stop_reason,
        )

    def _technical_failure(
        self, draft, facts, candidates, area_hints, task_results, report,
        states, attempts, assignments, unassigned, day_id, round_id, round_number,
        issues, policy, completed_rounds, error_code,
    ):
        attempts.append(self._attempt(
            day_id, round_id, round_number, issues, policy,
            "technical_error", "technical_error", error_code,
        ))
        states[day_id] = RepairState(
            day_id=day_id,
            completed_rounds=states[day_id].completed_rounds,
            status="stopped",
            stop_reason="technical_error",
        )
        return self._finish(
            "technical_error", draft, facts, candidates, area_hints, task_results,
            report, states, assignments, attempts, unassigned,
            "technical_error", error_code,
        )

    def _record_failed_round(
        self, states, attempts, day_id, round_id, round_number, issues, policy,
        completed_rounds, max_rounds, *, collected_stop, error_code,
        outcome="technical_error",
    ) -> bool:
        """技术失败独立计数；业务无进展/非法修订仍消耗业务轮次。"""
        technical = outcome == "technical_error"
        count = states[day_id].completed_rounds if technical else min(max_rounds, completed_rounds)
        attempts.append(self._attempt(
            day_id, round_id, round_number, issues, policy,
            outcome, collected_stop, error_code,
        ))
        failures = sum(item.day_id == day_id and item.outcome == "technical_error" for item in attempts)
        stopped = failures >= MAX_TECHNICAL_FAILURES_PER_DAY if technical else count >= max_rounds
        reason = ("technical_error" if technical else "round_limit") if stopped else None
        states[day_id] = RepairState(
            day_id=day_id, completed_rounds=count,
            status="stopped" if stopped else "open", stop_reason=reason,
        )
        if technical:
            with span("repair.technical_recovery", "validation", {
                "day_id": day_id, "round_id": round_id, "round_number": round_number,
            }) as record:
                record.output = {
                    "outcome": "exhausted" if stopped else "retry",
                    "error_code": safe_error_code(error_code),
                    "technical_failure_count": failures,
                    "technical_failure_limit": MAX_TECHNICAL_FAILURES_PER_DAY,
                    "completed_business_rounds": count,
                    "business_round_consumed": False,
                    "next_business_round": None if stopped else count + 1,
                }
        return stopped

    def _deadline_result(
        self, planning_input, draft, facts, candidates, area_hints, task_results,
        report, states, assignments, attempts, unassigned, *, active=None,
    ):
        current_assignments = assign_repair_issues(planning_input, report, facts, candidates)
        assignments.extend(item for item in current_assignments if item not in assignments)
        unassigned.clear()
        unassigned.update(item.issue_id for item in current_assignments if item.assigned_day_id is None)
        active_day_id = active[0] if active else None
        for constraint in planning_input.days:
            day_id = constraint.day_id
            current_issues = self._issues_for_day(report, current_assignments, day_id)
            state = states[day_id]
            if day_id == active_day_id:
                _, round_id, round_number, issues, policy, completed_rounds, error_code = active
                attempts.append(self._attempt(
                    day_id, round_id, round_number, issues, policy,
                    "deadline", "deadline", error_code,
                ))
                if not current_issues:
                    states[day_id] = RepairState(
                        day_id=day_id,
                        completed_rounds=state.completed_rounds,
                        status="complete",
                        stop_reason="complete",
                    )
                    continue
                states[day_id] = RepairState(
                    day_id=day_id,
                    completed_rounds=state.completed_rounds,
                    status="stopped",
                    stop_reason="deadline",
                )
                continue
            if not current_issues:
                states[day_id] = RepairState(
                    day_id=day_id,
                    completed_rounds=state.completed_rounds,
                    status="complete",
                    stop_reason="complete",
                )
                continue
            if state.status != "stopped":
                states[day_id] = RepairState(
                    day_id=day_id,
                    completed_rounds=state.completed_rounds,
                    status="stopped",
                    stop_reason="deadline",
                )
        return self._finish(
            "partial", draft, facts, candidates, area_hints, task_results,
             report, states, assignments, attempts, unassigned, "deadline",
        )

    @staticmethod
    def _attempt(day_id, round_id, round_number, issues, policy, outcome,
                 collection_stop_reason, error_code=None):
        attempt = DayRepairAttempt(
            day_id=day_id,
            round_id=round_id,
            round_number=round_number,
            issue_ids=tuple(
                item.issue.issue_id if isinstance(item, _AssignedIssue) else item.issue_id
                for item in issues
            ),
            outcome=outcome,
            collection_stop_reason=collection_stop_reason,
            preserve_activity_ids=policy.preserve_ids,
            modifiable_activity_ids=policy.modifiable_ids,
            removable_activity_ids=policy.removable_ids,
            error_code=error_code,
        )
        # 每轮结束即持久化；不等 pipeline 返回后再保存内存中的 attempts。
        with span("repair.round_result", "validation", {
            "day_id": day_id, "round_id": round_id, "round_number": round_number,
        }) as record:
            record.output = {
                "schema_version": 1, "stage": "repair.round_result",
                "outcome": outcome, "accepted": outcome == "applied",
                "business_round_consumed": outcome in {"applied", "no_progress", "invalid_revision"},
                "collection_stop_reason": collection_stop_reason,
                "error_code": safe_error_code(error_code) if error_code else None,
                "issue_count": len(attempt.issue_ids),
                "preserved_activity_count": len(policy.preserve_ids),
                "modifiable_activity_count": len(policy.modifiable_ids),
                "removable_activity_count": len(policy.removable_ids),
            }
        return attempt

    @staticmethod
    def _finish(status, draft, facts, candidates, area_hints, task_results,
                report, states, assignments, attempts, unassigned, stop_reason,
                error_code=None):
        final_states = tuple(states.values())
        return DayRepairResult(
            status=status,
            draft=draft,
            facts=facts,
            candidates=tuple(candidates),
            area_hints=tuple(area_hints),
            task_results=tuple(task_results),
            report=_replace_report_states(report, final_states),
            repair_states=final_states,
            assignments=tuple(assignments),
            attempts=tuple(attempts),
            unassigned_issue_ids=tuple(sorted(unassigned)),
            stop_reason=stop_reason,
            error_code=error_code,
        )

    @staticmethod
    def _validate_run_binding(planning_input, draft, report):
        if draft.request_id != planning_input.request.request_id:
            raise DayRepairInputError("draft request_id does not match PlanningInput")
        if draft.rule_version != planning_input.rules.rule_version:
            raise DayRepairInputError("draft rule_version does not match PlanningInput")
        if not isinstance(draft.source_run_id, str) or not draft.source_run_id.strip():
            raise DayRepairInputError("draft source_run_id must be present")
        input_days = {day.day_id: day.day_date for day in planning_input.days}
        draft_days = {day.day_id: day.day_date for day in draft.days}
        if input_days != draft_days:
            raise DayRepairInputError("PlanningInput and PlanDraft day IDs/dates must match")
        if report.plan_id != draft.plan_id or report.based_on_revision != draft.revision:
            raise DayRepairInputError("ValidationReport must bind the current PlanDraft revision")

    @staticmethod
    def _state_map(draft, report):
        by_id = {item.day_id: item for item in report.repair_states}
        return {
            day.day_id: by_id.get(day.day_id, RepairState(
                day_id=day.day_id, completed_rounds=0, status="open",
            ))
            for day in draft.days
        }

    @staticmethod
    def _ordered_states(states, planning_input):
        return tuple(states[day.day_id] for day in planning_input.days if day.day_id in states)

    @staticmethod
    def _day(draft, day_id):
        day = next((item for item in draft.days if item.day_id == day_id), None)
        if day is None:
            raise DayRepairInputError("repair day is absent from PlanDraft")
        return day

    @staticmethod
    def _issues_for_day(report, assignments, day_id):
        by_id = {item.issue_id: item for item in report.issues}
        assigned_ids = {
            assignment.issue_id for assignment in assignments
            if assignment.assigned_day_id == day_id
        }
        return tuple(
            _AssignedIssue(by_id[issue_id], assignment)
            for issue_id in sorted(assigned_ids)
            if issue_id in by_id
            for assignment in assignments
            if assignment.issue_id == issue_id and assignment.assigned_day_id == day_id
        )

    @staticmethod
    def _validate_recalculation(old_draft, proposed_draft, recalculated, day_id):
        result_draft = recalculated.draft
        if (
            result_draft.plan_id != proposed_draft.plan_id
            or result_draft.request_id != proposed_draft.request_id
            or result_draft.source_run_id != proposed_draft.source_run_id
            or result_draft.rule_version != proposed_draft.rule_version
            or result_draft.revision != proposed_draft.revision
            or result_draft.base_revision != proposed_draft.base_revision
        ):
            raise DayRepairInputError("recalculation returned the wrong plan revision")
        if recalculated.report.plan_id != result_draft.plan_id or recalculated.report.based_on_revision != result_draft.revision:
            raise DayRepairInputError("recalculated report does not bind the new revision")
        proposed_target = next((day for day in proposed_draft.days if day.day_id == day_id), None)
        recalculated_target = next((day for day in result_draft.days if day.day_id == day_id), None)
        if proposed_target is None or recalculated_target != proposed_target:
            raise DayRepairInputError("recalculation modified the target day after DayRevision validation")
        old_other = {day.day_id: day for day in old_draft.days if day.day_id != day_id}
        next_other = {day.day_id: day for day in result_draft.days if day.day_id != day_id}
        if old_other != next_other:
            raise DayRepairInputError("recalculation modified a non-target day")
        old_legs = {leg.leg_id: leg.model_copy(update={"plan_revision": proposed_draft.revision})
                    for leg in old_draft.legs if leg.day_id != day_id}
        next_legs = {leg.leg_id: leg for leg in result_draft.legs if leg.day_id != day_id}
        if old_legs != next_legs:
            raise DayRepairInputError("recalculation modified non-target route legs")


@dataclass(frozen=True)
class _AssignedIssue:
    issue: ValidationIssue
    assignment: IssueDayAssignment


def assign_repair_issues(
    planning_input: PlanningInput,
    report: ValidationReport,
    facts: FactStore,
    candidates: tuple[CandidateRef, ...],
) -> tuple[IssueDayAssignment, ...]:
    """只把必需时段的真实地点缺项分配给该日期。"""
    return tuple(
        IssueDayAssignment(
            issue_id=issue.issue_id,
            assigned_day_id=issue.day_id,
            reason="issue_day",
        )
        for issue in report.issues
        if is_required_place_gap(planning_input, issue)
    )


def derive_activity_policy(draft: PlanDraft, day_id: str, issues: tuple[ValidationIssue, ...]) -> _Policy:
    """地点缺项只允许填空；已有真实活动及相对顺序全部保留。"""
    day = next((item for item in draft.days if item.day_id == day_id), None)
    if day is None:
        raise DayRepairInputError("repair day is absent from PlanDraft")
    return _Policy(
        tuple(activity.activity_id for activity in day.activities if activity.kind != "free_time"),
        (),
        tuple(activity.activity_id for activity in day.activities if activity.kind == "free_time"),
    )


def apply_day_revision(
    revision: DayRevision,
    draft: PlanDraft,
    report: ValidationReport,
    planning_input: PlanningInput,
    candidates: tuple[CandidateRef, ...],
    facts: FactStore,
    *,
    expected_day_id: str,
    expected_source_issue_ids: tuple[str, ...],
    preserve_activity_ids: tuple[str, ...] = (),
    allowed_change_activity_ids: tuple[str, ...] = (),
    removable_activity_ids: tuple[str, ...] = (),
) -> tuple[PlanDraft, bool]:
    """验证并原子替换单日；目标日路线作废，其他日期活动及路线保留。"""
    if revision.day_id != expected_day_id:
        raise DayRepairInputError("DayRevision target does not match the active repair day")
    if set(revision.source_issue_ids) != set(expected_source_issue_ids):
        raise DayRepairInputError("DayRevision source_issue_ids do not match the assigned repair issues")
    day_by_id = {day.day_id: day for day in draft.days}
    old_day = day_by_id.get(revision.day_id)
    if old_day is None:
        raise DayRepairInputError("DayRevision target does not exist")
    if revision.revision != draft.revision + 1:
        raise DayRepairInputError("DayRevision revision must be the next PlanDraft revision")
    if not set(allowed_change_activity_ids).issubset({item.activity_id for item in old_day.activities}):
        raise DayRepairInputError("modifiable activity IDs must belong to the target day")
    if set(revision.preserve_activity_ids) != set(preserve_activity_ids):
        raise DayRepairInputError("DayRevision preserve_activity_ids do not match the repair policy")
    old_ids = {item.activity_id for item in old_day.activities}
    if set(removable_activity_ids) - old_ids:
        raise DayRepairInputError("removable activity IDs must belong to the target day")
    if set(preserve_activity_ids).intersection(allowed_change_activity_ids):
        raise DayRepairInputError("an activity cannot be both preserved and modifiable")

    replacement = _drop_replaced_free_time(revision.replacement_day)
    normalized = DayRevision.model_validate({
        **revision.model_dump(mode="python"),
        "replacement_day": replacement,
    })
    validate_day_revision(normalized, draft, report, planning_input, candidates, facts)
    _validate_revision_activity_ids(normalized, old_day)

    old_by_id = {activity.activity_id: activity for activity in old_day.activities}
    replacement_existing_ids = {
        activity.existing_activity_id
        for activity in normalized.replacement_day.activities
        if activity.existing_activity_id is not None
    }
    proposed_preserved_order = tuple(
        activity.existing_activity_id
        for activity in normalized.replacement_day.activities
        if activity.existing_activity_id in set(preserve_activity_ids)
    )
    original_preserved_order = tuple(
        activity.activity_id for activity in old_day.activities
        if activity.activity_id in set(preserve_activity_ids)
    )
    if proposed_preserved_order != original_preserved_order:
        raise DayRepairInputError("DayRevision changed the relative order of preserved activities")
    omitted_ids = old_ids - replacement_existing_ids
    if omitted_ids - set(removable_activity_ids):
        raise DayRepairInputError("DayRevision omitted an activity that is not removable")
    real_ids = {activity.activity_id for activity in old_day.activities if activity.kind != "free_time"}
    if real_ids != set(preserve_activity_ids).union(allowed_change_activity_ids):
        raise DayRepairInputError("repair policy must account for every real target-day activity")
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    new_activities: list[DraftActivity] = []
    for proposal in normalized.replacement_day.activities:
        if proposal.existing_activity_id is not None:
            old = old_by_id[proposal.existing_activity_id]
            if old.kind == "free_time":
                new_activities.append(old)
                continue
            can_change = old.activity_id in allowed_change_activity_ids
            if can_change:
                activity_changed = proposal.slot_id != old.slot_id
                if old.activity_source == "user" and not activity_changed:
                    new_activities.append(old)
                else:
                    new_activities.append(old.model_copy(update={
                        "slot_id": proposal.slot_id,
                        "activity_source": "model",
                    }))
            else:
                # Existing valid activities are copied from the trusted draft. The model
                # cannot change their content during a missing-only repair.
                new_activities.append(old)
            continue
        if proposal.kind == "free_time":
            new_activities.append(DraftActivity(
                activity_id=_new_activity_id(draft, revision, proposal),
                slot_id=proposal.slot_id,
                kind="free_time",
                activity_source="model",
                recommendation_reason=proposal.recommendation_reason,
            ))
            continue
        candidate = candidate_by_id.get(proposal.candidate_ref or "")
        if candidate is None:
            raise DayRepairInputError("revision candidate is absent")
        new_activities.append(DraftActivity(
            activity_id=_new_activity_id(draft, revision, proposal),
            candidate_ref=candidate.candidate_id,
            slot_id=proposal.slot_id,
            kind=proposal.kind,
            poi_id=candidate.poi_id,
            place_fact_id=candidate.place_fact_id,
            activity_source="model",
            requirement_ids=proposal.requirement_ids,
            recommendation_reason=proposal.recommendation_reason,
        ))

    new_day = DraftDay(
        day_id=old_day.day_id,
        day_date=old_day.day_date,
        theme=old_day.theme,
        activities=tuple(new_activities),
        unfilled_slot_ids=normalized.replacement_day.unfilled_slot_ids,
        weather_fact_id=old_day.weather_fact_id,
        weather_advice=old_day.weather_advice,
        lodging=old_day.lodging,
    )
    if new_day.model_dump(mode="json") == old_day.model_dump(mode="json"):
        return draft, False
    new_revision = draft.revision + 1
    next_days = tuple(new_day if item.day_id == revision.day_id else item for item in draft.days)
    next_legs = tuple(
        leg.model_copy(update={"plan_revision": new_revision})
        for leg in draft.legs if leg.day_id != revision.day_id
    )
    updated = PlanDraft.model_validate({
        **draft.model_dump(mode="python"),
        "revision": new_revision,
        "base_revision": draft.revision,
        "days": next_days,
        "legs": next_legs,
    })
    return updated, True


def _drop_replaced_free_time(day: ProposalDay) -> ProposalDay:
    real_slots = {activity.slot_id for activity in day.activities if activity.kind != "free_time"}
    activities = tuple(
        activity for activity in day.activities
        if activity.kind != "free_time" or activity.slot_id not in real_slots
    )
    return day.model_copy(update={"activities": activities})


def _validate_revision_activity_ids(revision: DayRevision, old_day: DraftDay) -> None:
    existing_ids = [
        item.existing_activity_id for item in revision.replacement_day.activities
        if item.existing_activity_id is not None
    ]
    if len(existing_ids) != len(set(existing_ids)):
        raise DayRepairInputError("an existing activity can appear only once in a DayRevision")
    proposal_refs = [item.proposal_ref for item in revision.replacement_day.activities]
    if len(proposal_refs) != len(set(proposal_refs)):
        raise DayRepairInputError("proposal_ref values must be unique in a DayRevision")
    old_ids = {item.activity_id for item in old_day.activities}
    if set(existing_ids) - old_ids:
        raise DayRepairInputError("DayRevision references an activity outside the target day")


def _new_activity_id(draft: PlanDraft, revision: DayRevision, activity: ProposalActivity) -> str:
    from uuid import NAMESPACE_URL, uuid5

    return str(uuid5(NAMESPACE_URL, "|".join((
        "day-revision-activity", draft.plan_id, str(revision.revision),
        revision.day_id, activity.proposal_ref,
    ))))


def _collection_scope(planning_input, constraint, day, issues, facts, candidates):
    targets: list[DayRepairCollectionTarget] = []
    slots = {slot.slot_id: slot for slot in constraint.slots}
    for issue in issues:
        if not is_required_place_gap(planning_input, issue):
            continue
        if issue.code == "missing_meal":
            role = "dining"
        elif issue.code == "missing_sightseeing":
            role = "attractions"
        else:
            continue
        if issue.target_id in slots:
            target_slots = (slots[issue.target_id],)
        else:
            target_slots = ()
        available = [slot for slot in target_slots if slot.eligibility == "required"]
        if not available:
            continue
        category = "meal" if role == "dining" else "sightseeing"
        relevant_candidates = [
            candidate for candidate in candidates
            if candidate.category == category
            and any(candidate_allows_slot(candidate, slot.slot_id) for slot in available)
        ]
        usable = any(
            candidate.category == category
            and _candidate_has_verified_place(candidate, facts, city_for_days(planning_input, (day.day_id,)))
            and any(candidate_allows_slot(candidate, slot.slot_id) for slot in available)
            for candidate in candidates
        )
        if not usable:
            targets.append(DayRepairCollectionTarget(
                day_id=day.day_id,
                role=role,
                issue_ids=(issue.issue_id,),
                slot_ids=tuple(slot.slot_id for slot in available),
                candidate_ids=tuple(candidate.candidate_id for candidate in relevant_candidates),
            ))
    return bool(targets), tuple(targets)


def _candidate_has_verified_place(candidate, facts, city):
    place = next((item for item in facts.places if item.fact_id == candidate.place_fact_id), None)
    if place is None or place.poi_id != candidate.poi_id or place.location is None:
        return False
    source_ids = {source.tool_call_id for source in facts.sources}
    if not place.source_refs or not set(place.source_refs).issubset(source_ids):
        return False
    if not _city_matches(city, place.city):
        return False
    return (
        _category_status(place) == "sightseeing" if candidate.category == "sightseeing"
        else _dining_category(place) == "dining" if candidate.category == "meal"
        else False
    )


def _known_fact_ids(facts: FactStore) -> list[str]:
    return [item.fact_id for item in (*facts.places, *facts.weather, *facts.routes)]


def _merge_facts(existing: FactStore, returned: FactStore) -> FactStore:
    sources = _merge_by_id(existing.sources, returned.sources, "tool_call_id", "fact source")
    places = _merge_by_id(existing.places, returned.places, "fact_id", "place fact")
    weather = _merge_by_id(existing.weather, returned.weather, "fact_id", "weather fact")
    routes = _merge_by_id(existing.routes, returned.routes, "fact_id", "route fact")
    return FactStore(sources=sources, places=places, weather=weather, routes=routes)


def _merge_by_id(existing, returned, id_field, label, *, replace=False):
    merged = OrderedDict((getattr(item, id_field), item) for item in existing)
    for item in returned:
        key = getattr(item, id_field)
        previous = merged.get(key)
        if previous is not None and previous != item and not replace:
            raise DayRepairInputError(f"conflicting {label} identity")
        merged[key] = item
    return tuple(merged.values())


def _replace_report_states(report: ValidationReport, states: tuple[RepairState, ...]) -> ValidationReport:
    return ValidationReport.model_validate({
        **report.model_dump(mode="python"),
        "repair_states": states,
    })


def _safe_revision_error(exc: Exception) -> str:
    if isinstance(exc, ContractValidationError):
        codes = sorted({item.code for item in exc.issues})
        return "INVALID_DAY_REVISION:" + ",".join(codes[:8])
    if isinstance(exc, DayRepairInputError):
        return "INVALID_DAY_REVISION:contract"
    return "INVALID_DAY_REVISION:validation"


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _safe_callback_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code and code.isascii() and all(
        character.isupper() or character.isdigit() or character == "_"
        for character in code
    ):
        return code[:96]
    if isinstance(exc, TimeoutError):
        return "CALLBACK_TIMEOUT"
    if isinstance(exc, ValidationError):
        locations = []
        for item in exc.errors():
            path = ".".join(str(part) for part in item.get("loc", ()))
            locations.append(f"{path}:{item.get('type', 'invalid')}")
        return "CALLBACK_VALIDATION_ERROR:" + ",".join(locations[:6])
    exception_type = type(exc).__name__.upper()
    if exception_type.isascii() and all(
        character.isupper() or character.isdigit() or character == "_"
        for character in exception_type
    ):
        return f"CALLBACK_{exception_type}"[:96]
    return "DAY_REPAIR_CALLBACK_FAILED"


async def _await(result):
    return await result if inspect.isawaitable(result) else result
