"""正式规划输出：只携带公开值，不携带采集器或模型协议。"""
from typing import Literal
from pydantic import Field
from app.models.planning_v4.base import ContractModel
from app.models.planning_execution import DraftResult, PlanningSnapshot
from app.models.planning_repair import DayRepairResult

class PlanningOutcome(ContractModel):
    schema_version: Literal[1] = 1
    status: Literal['complete', 'partial', 'technical_error']
    error_code: str | None = Field(default=None, pattern=r'^[A-Z][A-Z_0-9]{0,95}$')
    initial_result: DraftResult
    snapshot: PlanningSnapshot
    repair: DayRepairResult

    @property
    def final_result(self):
        return DraftResult(identity=self.snapshot.identity, planning_input=self.snapshot.planning_input,
            draft=self.repair.draft, report=self.repair.report, facts=self.repair.facts,
            candidates=self.repair.candidates, area_hints=self.repair.area_hints)

    @property
    def planning_input(self): return self.snapshot.planning_input
    @property
    def initial_draft(self): return self.initial_result.draft
    @property
    def draft(self): return self.repair.draft
    @property
    def fact_store(self): return self.repair.facts
    @property
    def report(self): return self.repair.report
