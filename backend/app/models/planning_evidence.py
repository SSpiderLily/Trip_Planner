"""跨阶段事实验证资料；完整保留可重放投影的结构化查询结果。"""
from typing import Literal
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4 import PlanningInput, FactStore
from app.planning.weather_projection import WeatherQueryExecution
from app.planning.endpoint_projection import EndpointVerificationExecution, EndpointVerification


class WeatherEvidence(ContractModel):
    schema_version: Literal[1] = 1
    run_id: str
    planning_input: PlanningInput
    executions: tuple[WeatherQueryExecution, ...]
    fact_store: FactStore


class EndpointEvidence(ContractModel):
    schema_version: Literal[1] = 1
    run_id: str
    planning_input: PlanningInput
    executions: tuple[EndpointVerificationExecution, ...]
    arrival: EndpointVerification
    departure: EndpointVerification
    fact_store: FactStore
