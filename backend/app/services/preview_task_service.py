"""异步 v4 预览任务编排；不把预览写入 legacy task_results。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol
from uuid import uuid4

from pydantic import Field

from app.models.planning_v4.base import ContractModel
from app.models.planning_v4 import TripRequest
from app.models.planning_v4_preview import PreviewEnvelope, PreviewSnapshot
from app.services.observation_service import span
from app.services.task_service import TaskError


class PreviewProgress(ContractModel):
    stage: Literal[
        "planning_input", "dispatch", "proposal", "current_routes", "timing",
        "validation", "repair", "cost", "ready",
    ]
    completed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    message: str | None = None


class BackfillProgress(ContractModel):
    completed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


ProgressCallback = Callable[[PreviewProgress], Awaitable[None]]
BackfillProgressCallback = Callable[[BackfillProgress], Awaitable[None]]
BackfillRunner = Callable[[BackfillProgressCallback], Awaitable[None]]


@dataclass
class PreviewPipelineResult:
    snapshot: PreviewSnapshot
    route_context: object | None = None
    backfill: BackfillRunner | None = None


class PlanningV4Pipeline(Protocol):
    """在结果返回前若失败，pipeline 必须关闭自己创建的路线 context。"""

    async def run(
        self,
        request: TripRequest,
        *,
        task_id: str,
        plan_id: str,
        progress: ProgressCallback,
    ) -> PreviewPipelineResult: ...


class PreviewTaskService:
    """提供可注入 pipeline 的任务生命周期和同进程路线上下文保留。"""

    def __init__(self, task_service, repository, pipeline: PlanningV4Pipeline | None = None):
        self.task_service = task_service
        self.repository = repository
        self.pipeline = pipeline
        self.route_contexts: dict[str, object] = {}
        self.backfills: dict[str, asyncio.Task] = {}
        self.accepting = True
        self.closing = False

    async def submit(self, request: TripRequest) -> dict:
        if not self.accepting:
            raise TaskError("SERVICE_STOPPING", "服务暂不接收任务", 503)
        if self.pipeline is None:
            raise TaskError("PREVIEW_PIPELINE_UNAVAILABLE", "v4 规划链尚未接入", 503)
        plan_id = "plan_" + uuid4().hex
        access_token = self.repository.issue_access_token()

        async def prepare(task_id: str) -> None:
            try:
                await asyncio.to_thread(self.repository.create_job, task_id, plan_id, access_token)
            except Exception:
                raise

        async def execute(task_id: str) -> None:
            await self._execute(task_id, plan_id, request)

        accepted = await self.task_service.submit_managed(
            request, prepare=prepare, executor=execute,
        )
        return {
            "task_id": accepted["task_id"],
            "plan_id": plan_id,
            "access_token": access_token,
            "status": "accepted",
        }

    async def _execute(self, task_id: str, plan_id: str, request: TripRequest) -> None:
        async def progress(update: PreviewProgress) -> None:
            await self._update_job_best_effort(
                task_id,
                generation_state="running",
                generation_stage=update.stage,
                generation_completed=update.completed,
                generation_total=update.total,
                generation_message=update.message,
            )

        result: PreviewPipelineResult | None = None
        try:
            await self._update_job_best_effort(
                task_id,
                generation_state="running", generation_stage="planning_input",
            )
            observation = self.task_service.observation
            with observation.task(task_id), span("planning.v4", "planning", request.model_dump(mode="json")):
                result = await self.pipeline.run(
                    request, task_id=task_id, plan_id=plan_id, progress=progress,
                )
            if result.snapshot.preview.draft.plan_id != plan_id:
                raise ValueError("pipeline 返回了其他计划的预览")
            if result.snapshot.preview.planning_input.request != request:
                raise ValueError("预览必须引用提交的原始 TripRequest")
            if result.backfill is not None and result.route_context is None:
                raise ValueError("后台路线补齐必须复用当前计划的 PlanRouteContext")
            if result.route_context is not None:
                assert_binding = getattr(result.route_context, "assert_binding", None)
                register_facts = getattr(result.route_context, "register_facts", None)
                if assert_binding is None or register_facts is None:
                    raise ValueError("pipeline 返回的 route_context 不符合 PlanRouteContext 接口")
                assert_binding(result.snapshot.preview.planning_input, result.snapshot.preview.draft)
                register_facts(result.snapshot.preview.facts)
            from app.services.planning_v4_validator import validate_preview

            validate_preview(result.snapshot.preview)
            await asyncio.to_thread(self.repository.complete_job, task_id, result.snapshot)
        except asyncio.CancelledError:
            await self._close_unretained_context(result)
            await self._update_job_best_effort(
                task_id, generation_state="interrupted", generation_stage="interrupted",
                error_code="PROCESS_INTERRUPTED",
            )
            raise
        except Exception as exc:
            await self._close_unretained_context(result)
            await self._update_job_best_effort(
                task_id, generation_state="failed", generation_stage="failed",
                generation_message="规划失败", error_code=getattr(exc, "code", "PLANNING_FAILED"),
            )
            raise

        # The preview is durable now. Context bookkeeping or background scheduling failures
        # must not turn this successful preview into a failed generation task.
        self.task_service.release_active(task_id)
        if result.route_context is not None:
            if self.closing:
                try:
                    await self._close_context(result.route_context)
                except Exception:
                    pass
            else:
                self.route_contexts[plan_id] = result.route_context
        if result.backfill is not None and not self.closing:
            try:
                await self._update_job_best_effort(
                    task_id,
                    backfill_state="running", backfill_completed=0, backfill_total=0,
                )
                task = asyncio.create_task(self._run_backfill(task_id, result.backfill))
                self.backfills[task_id] = task
                task.add_done_callback(lambda done, key=task_id: self._backfill_done(key, done))
            except Exception:
                await self._update_job_best_effort(task_id, backfill_state="failed")
        elif result.backfill is not None:
            await self._update_job_best_effort(task_id, backfill_state="interrupted")

    async def _close_unretained_context(self, result: PreviewPipelineResult | None) -> None:
        context = result.route_context if result is not None else None
        if context is None or any(context is retained for retained in self.route_contexts.values()):
            return
        try:
            await self._close_context(context)
        except Exception:
            pass

    async def _update_job_best_effort(self, task_id: str, **updates) -> None:
        try:
            await asyncio.to_thread(self.repository.update_job, task_id, **updates)
        except Exception:
            observation = getattr(self.task_service, "observation", None)
            missing = getattr(observation, "missing", None)
            if missing is not None:
                try:
                    missing(task_id)
                except Exception:
                    pass

    def _backfill_done(self, task_id: str, task: asyncio.Task) -> None:
        if self.backfills.get(task_id) is task:
            self.backfills.pop(task_id, None)
        try:
            task.exception()
        except BaseException:
            # Retrieve cancellation/late exceptions so asyncio does not report an
            # unobserved task failure from a best-effort background coordinator.
            pass

    async def _run_backfill(self, task_id: str, runner: BackfillRunner) -> None:
        async def progress(update: BackfillProgress) -> None:
            await self._update_job_best_effort(
                task_id,
                backfill_state="running", backfill_completed=update.completed,
                backfill_total=update.total,
            )

        try:
            observation = self.task_service.observation
            with observation.task(task_id), span("planning.v4.backfill", "planning"):
                await runner(progress)
            await self._update_job_best_effort(task_id, backfill_state="completed")
        except asyncio.CancelledError:
            await self._update_job_best_effort(task_id, backfill_state="interrupted")
            raise
        except BackfillSuperseded:
            await self._update_job_best_effort(task_id, backfill_state="interrupted")
        except Exception:
            await self._update_job_best_effort(task_id, backfill_state="failed")

    async def status(self, task_id: str, access_token: str) -> dict | None:
        try:
            return await asyncio.to_thread(self.repository.job_status, task_id, access_token)
        except Exception as exc:
            raise TaskError("PREVIEW_STORAGE_UNAVAILABLE", "预览状态暂不可用", 503) from exc

    async def preview(self, task_id: str, access_token: str) -> PreviewEnvelope | None:
        status = await self.status(task_id, access_token)
        if status is None or not status["preview_ready"]:
            return None
        try:
            stored = await asyncio.to_thread(self.repository.get_preview, access_token)
        except Exception as exc:
            raise TaskError("PREVIEW_STORAGE_UNAVAILABLE", "预览暂不可用", 503) from exc
        if stored is None or stored.snapshot is None:
            return None
        return PreviewEnvelope(
            task_id=task_id,
            plan_id=stored.preview.draft.plan_id,
            store_version=stored.store_version,
            plan_revision=stored.plan_revision,
            preview=stored.preview,
            timing=stored.timing,
            cost=stored.cost,
        )

    async def close(self) -> None:
        """取消后台协调器，再关闭保留的 context；前台任务由 TaskService 等待。"""
        self.accepting = False
        self.closing = True
        tasks = list(self.backfills.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        contexts, self.route_contexts = list(self.route_contexts.values()), {}
        for context in contexts:
            await self._close_context(context)

    @staticmethod
    async def _close_context(context: object) -> None:
        close = getattr(context, "aclose", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


class BackfillSuperseded(RuntimeError):
    """计划版本已变化；结果只留在共享事实缓存中，不回写预览。"""


def make_route_backfill_runner(
    backfill_service,
    planning_input,
    draft,
    facts,
    route_context,
    *,
    current_revision: Callable[[], int],
) -> BackfillRunner:
    """把 F11b 协调器接入 v4 任务进度；从不把旧版本结果合并进预览。"""
    async def run(progress: BackfillProgressCallback) -> None:
        from app.services.route_backfill_service import RouteBackfillProgress

        async def relay(update: RouteBackfillProgress) -> None:
            await progress(BackfillProgress(
                completed=update.completed_queries,
                total=update.total_queries,
            ))

        result = await backfill_service.run_async(
            planning_input,
            draft,
            facts,
            route_context,
            progress_callback=relay,
            current_revision=current_revision,
        )
        if result.status == "cancelled":
            raise BackfillSuperseded("计划版本已变化")

    return run
