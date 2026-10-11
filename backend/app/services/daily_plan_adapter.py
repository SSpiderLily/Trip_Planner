"""将同步 F06 每日提案生成器接入异步规划任务。"""
from __future__ import annotations

import asyncio

from app.agents.daily_plan_generator import DailyPlanExecution, DailyPlanGenerator
from app.models.planning_v4 import AreaHint, CandidateRef, FactStore, PlanningInput, TaskResult


class DailyPlanGeneratorAdapter:
    """在默认线程池运行 F06，避免同步模型等待占住应用 event loop。"""

    def __init__(self, generator: DailyPlanGenerator | None = None):
        self.generator = generator or DailyPlanGenerator()

    async def generate(
        self,
        planning_input: PlanningInput,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        source_run_id: str,
    ) -> DailyPlanExecution:
        """保留 F06 的参数、结果和异常语义，只把同步调用移出事件循环。"""
        return await asyncio.to_thread(
            self.generator.generate,
            planning_input,
            candidates,
            facts,
            area_hints,
            task_results,
            source_run_id=source_run_id,
        )
