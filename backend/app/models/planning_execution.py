"""仅规划模式的值契约，不携带客户端、缓存或协议历史。"""
from copy import deepcopy
from typing import Any, Literal
from pydantic import Field, model_validator
from app.models.planning_runtime import PlanningRunIdentity
from app.models.planning_evidence import WeatherEvidence, EndpointEvidence
from app.models.planning_v4 import (
    PlanningInput, FactStore, CandidateRef, AreaHint, TaskResult,
    ProposalDay, PlanDraft, ValidationReport,
)
from app.models.planning_v4.base import ContractModel


class CollectionTaskNote(ContractModel):
    task_id: str = Field(min_length=1)
    unmet_needs: tuple[str, ...] = ()


class PlanningSnapshot(ContractModel):
    schema_version: Literal[2] = 2
    identity: PlanningRunIdentity
    planning_input: PlanningInput
    facts: FactStore
    candidates: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    task_notes: tuple[CollectionTaskNote, ...] = ()
    weather_evidence: WeatherEvidence | None = None
    endpoint_evidence: EndpointEvidence | None = None

    @model_validator(mode='before')
    @classmethod
    def isolate_nested_values(cls, value):
        return deepcopy(value)


class ProposalSubmission(ContractModel):
    days: tuple[ProposalDay, ...] = Field(min_length=1)
    missing_notes: tuple[str, ...] = ()


class ProposalContext(ContractModel):
    system_instruction: str
    reference_information: dict[str, Any]


class DraftResult(ContractModel):
    identity: PlanningRunIdentity
    planning_input: PlanningInput
    draft: PlanDraft
    report: ValidationReport
    facts: FactStore
    candidates: tuple[CandidateRef, ...]
    area_hints: tuple[AreaHint, ...]


class DayRevisionSubmission(ContractModel):
    replacement_day: ProposalDay
