"""离线质量对照契约；人工结论不参与生产接受规则。"""
from typing import Literal
from pydantic import Field
from app.models.planning_runtime import PlanningRunIdentity
from app.models.planning_v4.base import ContractModel

Verdict = Literal['pass', 'fail', 'unknown']

class QualityRatings(ContractModel):
    breakfast_suitability: Verdict = 'unknown'
    repeat_reasonableness: Verdict = 'unknown'
    rest_preference: Verdict = 'unknown'
    specified_requirements: Verdict = 'unknown'
    unsupported_promises: Verdict = 'unknown'

class QualityReview(ContractModel):
    schema_version: Literal[2] = 2
    identity: PlanningRunIdentity
    request_id: str
    rule_version: str
    revision: int = Field(ge=1)
    result_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    ratings: QualityRatings = Field(default_factory=QualityRatings)

class DeterministicMetrics(ContractModel):
    delivered: bool
    references_valid: bool
    required_slots: int = Field(ge=0)
    covered_required_slots: int = Field(ge=0)
    required_coverage_complete: bool
    lodging_bound: bool

class PlanningEvaluation(ContractModel):
    schema_version: Literal[2] = 2
    identity: PlanningRunIdentity
    request_id: str
    rule_version: str
    revision: int = Field(ge=1)
    result_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic: DeterministicMetrics
    human: QualityRatings = Field(default_factory=QualityRatings)
