"""v4 子任务与任务结果类型，不包含调度器。"""
from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .base import ContractModel


class TaskDependency(ContractModel):
    kind: Literal["fact", "area_hint", "task"]
    reference_id: str = Field(min_length=1)


class TaskScope(ContractModel):
    city: str = Field(min_length=1)
    day_ids: tuple[str, ...] = ()
    slot_ids: tuple[str, ...] = ()
    area_hint_ids: tuple[str, ...] = ()


class Subtask(ContractModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    base_revision: int | None = Field(default=None, ge=1)
    role: Literal["attractions", "dining", "lodging"]
    objective: str = Field(min_length=1)
    scope: TaskScope
    dependencies: tuple[TaskDependency, ...] = ()
    known_fact_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    excluded_poi_ids: tuple[str, ...] = ()
    candidate_target: int = Field(ge=1, le=20)
    required_fields: tuple[Literal["poi_id", "name", "location"], ...] = (
        "poi_id",
        "name",
        "location",
    )
    constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    deadline_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_requirement_ids(self):
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("Subtask.requirement_ids 必须唯一")
        return self


class CandidateRef(ContractModel):
    candidate_id: str = Field(min_length=1)
    category: Literal["sightseeing", "meal", "lodging"]
    poi_id: str = Field(min_length=1)
    place_fact_id: str = Field(min_length=1)
    area_hint_id: str | None = Field(default=None, min_length=1)
    lodging_day_ids: tuple[str, ...] = ()
    slot_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    recommendation_reason: str = ""

    @model_validator(mode="after")
    def validate_area_hint(self):
        if self.lodging_day_ids and (self.category != "lodging" or len(self.lodging_day_ids) != len(set(self.lodging_day_ids))):
            raise ValueError("住宿日期范围仅用于住宿候选，且日期不能重复")
        if self.area_hint_id is not None and self.category != "lodging":
            raise ValueError("只有住宿候选可以关联 area_hint_id")
        return self


class TaskCoverage(ContractModel):
    target_id: str = Field(min_length=1)
    status: Literal["covered", "missing", "unknown"]
    reason: str | None = None


class TaskResult(ContractModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    request_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)
    status: Literal["complete", "partial", "failed"]
    stop_reason: Literal[
        "target_met",
        "no_match",
        "scope_exhausted",
        "no_progress",
        "ambiguous",
        "deadline",
        "tool_error",
        "model_error",
        "invalid_output",
    ]
    candidates: tuple[CandidateRef, ...] = ()
    coverage: tuple[TaskCoverage, ...] = ()
    missing: tuple[TaskCoverage, ...] = ()
    next_action_hint: str | None = None

    @model_validator(mode="after")
    def validate_candidate_ids(self):
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("同一 TaskResult 内 candidate_id 必须唯一")
        return self
