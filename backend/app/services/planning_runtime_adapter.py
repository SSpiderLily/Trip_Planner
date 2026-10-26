"""运行端口适配；异步桥接沿用 NativePlanningRuntime 的线程调度。"""
from threading import Event
from app.models.planning_errors import PlanningError
from app.models.planning_runtime import PreviewProgress

class SynchronousPlanningRuntime:
    def __init__(self, *, cancel_event=None, progress=None):
        self.cancel_event = cancel_event if cancel_event is not None else Event()
        self.progress = progress

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise PlanningError('CANCELLED', '规划已取消')

    def report(self, stage, completed=0, total=1, message=None):
        value = PreviewProgress(stage=stage, completed=completed, total=total, message=message)
        self.check_cancelled()
        if self.progress is not None:
            self.progress(value)
        self.check_cancelled()

class NativePlanningRuntimeAdapter:
    """在 native_runtime.blocking 内使用；不在其事件循环线程中调用。"""
    def __init__(self, native_runtime):
        self.runtime = native_runtime
        self.cancel_event = native_runtime.cancel_event

    def check_cancelled(self):
        self.runtime.check_cancelled()

    def report(self, stage, completed=0, total=1, message=None):
        PreviewProgress(stage=stage, completed=completed, total=total, message=message)
        self.runtime.report_from_thread(stage, completed, total, message)
