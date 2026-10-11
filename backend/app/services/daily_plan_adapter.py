"""将同步 F06 每日提案生成器接入异步规划任务。"""
from __future__ import annotations

import asyncio
from threading import Event

from app.agents.daily_plan_generator import (
    DailyDayRevisionExecution,
    DailyPlanExecution,
    DailyPlanGenerator,
)
from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    FactStore,
    PlanDraft,
    PlanningInput,
    TaskResult,
    ValidationReport,
)


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
        plan_id: str | None = None,
        cancel_event: Event | None = None,
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
            plan_id=plan_id,
            cancel_event=cancel_event,
        )

    async def revise_day(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        report: ValidationReport,
        day_id: str,
        issues,
        assignments,
        candidates: tuple[CandidateRef, ...],
        facts: FactStore,
        area_hints: tuple[AreaHint, ...],
        task_results: tuple[TaskResult, ...],
        *,
        preserve_activity_ids: tuple[str, ...],
        modifiable_activity_ids: tuple[str, ...] = (),
        removable_activity_ids: tuple[str, ...] = (),
        source_run_id: str,
        round_id: str,
        round_number: int,
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ) -> DailyDayRevisionExecution:
        """在工作线程执行 F06 的单日修订，避免模型等待占用 event loop。"""
        return await asyncio.to_thread(
            self.generator.revise_day,
            planning_input,
            draft,
            report,
            day_id,
            issues,
            assignments,
            candidates,
            facts,
            area_hints,
            task_results,
            preserve_activity_ids=preserve_activity_ids,
            modifiable_activity_ids=modifiable_activity_ids,
            removable_activity_ids=removable_activity_ids,
            source_run_id=source_run_id,
            round_id=round_id,
            round_number=round_number,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
