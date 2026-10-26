"""按日修订的公开值类型；旧服务导入路径保留兼容。"""
from typing import Literal
from app.models.planning_v4 import (PlanDraft, FactStore, CandidateRef, AreaHint, TaskResult, ValidationReport, RepairState)
from app.models.planning_v4.base import ContractModel
RepairStop = Literal["complete", "round_limit", "deadline", "technical_error"]
CollectionStop = Literal["target_met", "no_progress", "deadline", "technical_error"]

class IssueDayAssignment(ContractModel):
    issue_id: str
    assigned_day_id: str | None
    reason: Literal[
        "issue_day",
        "candidate_applicable_day",
        "first_schedulable_sightseeing_day",
        "no_eligible_sightseeing_day",
    ]
    candidate_id: str | None = None


class DayRepairAttempt(ContractModel):
    day_id: str
    round_id: str
    round_number: int = 1
    issue_ids: tuple[str, ...] = ()
    outcome: Literal["applied", "no_progress", "invalid_revision", "deadline", "technical_error"]
    collection_stop_reason: CollectionStop = "target_met"
    preserve_activity_ids: tuple[str, ...] = ()
    modifiable_activity_ids: tuple[str, ...] = ()
    removable_activity_ids: tuple[str, ...] = ()
    error_code: str | None = None


class DayRepairResult(ContractModel):
    status: Literal["complete", "partial", "technical_error"]
    draft: PlanDraft
    facts: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    report: ValidationReport
    repair_states: tuple[RepairState, ...] = ()
    assignments: tuple[IssueDayAssignment, ...] = ()
    attempts: tuple[DayRepairAttempt, ...] = ()
    unassigned_issue_ids: tuple[str, ...] = ()
    stop_reason: RepairStop | None = None
    error_code: str | None = None
