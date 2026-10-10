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
    excluded_poi_ids: tuple[str, ...] = ()
    candidate_target: int = Field(ge=1, le=20)
    required_fields: tuple[Literal["poi_id", "name", "location", "cost", "opening_hours"], ...] = (
        "poi_id",
        "name",
        "location",
    )
    constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    deadline_at: AwareDatetime | None = None


class CandidateRef(ContractModel):
    candidate_id: str = Field(min_length=1)
    category: Literal["sightseeing", "meal", "lodging"]
    poi_id: str = Field(min_length=1)
    place_fact_id: str = Field(min_length=1)
    slot_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    recommendation_reason: str = ""


class TaskCoverage(ContractModel):
    target_id: str = Field(min_length=1)
    status: Literal["covered", "missing", "unknown"]
    reason: str | None = None


class TaskResult(ContractModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    status: Literal["complete", "partial", "failed"]
    stop_reason: Literal[
        "target_met",
        "no_match",
        "scope_exhausted",
        "no_progress",
        "deadline",
        "tool_error",
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
