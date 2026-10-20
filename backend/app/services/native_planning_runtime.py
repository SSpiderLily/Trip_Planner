"""单次原生规划的身份、进度和协作取消；不读取配置或业务数据库。"""
from __future__ import annotations

import asyncio
from threading import Event

from pydantic import ValidationError

from app.models.planning_runtime import PlanningRunIdentity, PreviewProgress, ProgressCallback
from app.services.native_tool_loop import NativeToolLoopError


class NativePlanningRuntime:
    def __init__(self, *, task_id=None, plan_id=None, progress: ProgressCallback | None = None,
                 cancel_event: Event | None = None):
        self.identity = None
        if task_id is not None or plan_id is not None:
            try:
                self.identity = PlanningRunIdentity(task_id=task_id, plan_id=plan_id)
            except ValidationError:
                raise NativeToolLoopError("PLANNING_IDENTITY_INVALID") from None
        self.progress = progress
        self.cancel_event = cancel_event if cancel_event is not None else Event()
        self.loop = asyncio.get_running_loop()
        self.failure_code = None

    def check_cancelled(self):
        if self.failure_code is not None:
            raise NativeToolLoopError(self.failure_code)
        if self.cancel_event.is_set():
            raise NativeToolLoopError("CANCELLED")

    async def report(self, stage, completed=0, total=1, message=None):
        self.check_cancelled()
        if self.progress is not None:
            try:
                await self.progress(PreviewProgress(stage=stage, completed=completed,
                                                    total=total, message=message))
            except asyncio.CancelledError:
                self.cancel_event.set()
                raise
            except Exception:
                self.failure_code = "PROGRESS_CALLBACK_FAILED"
                self.cancel_event.set()
                raise NativeToolLoopError("PROGRESS_CALLBACK_FAILED") from None
        self.check_cancelled()

    def report_from_thread(self, stage, completed=0, total=1, message=None):
        """模型工具在线程执行；进度回调统一回到调用方事件循环。"""
        self.check_cancelled()
        if self.progress is not None:
            asyncio.run_coroutine_threadsafe(
                self.report(stage, completed, total, message), self.loop,
            ).result()
        self.check_cancelled()

    async def blocking(self, function, *args, **kwargs):
        self.check_cancelled()
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except asyncio.CancelledError:
            self.cancel_event.set()
            raise
        except Exception:
            self.check_cancelled()
            raise
        self.check_cancelled()  # 不采用取消后到达的模型/地图结果。
        return result
