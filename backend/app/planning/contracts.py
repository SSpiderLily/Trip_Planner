"""编排只依赖端口，模型与运行环境在入口装配。"""
from typing import Callable, Protocol
from app.models.planning_execution import ProposalContext, ProposalSubmission, DraftResult

class PlanningRuntimePort(Protocol):
    def check_cancelled(self) -> None: ...
    def report(self, stage: str, completed: int = 0, total: int = 1,
               message: str | None = None) -> None: ...

class ProposalPort(Protocol):
    def generate(self, context: ProposalContext, *,
                 accept: Callable[[ProposalSubmission], DraftResult],
                 runtime: PlanningRuntimePort) -> DraftResult: ...
