"""按任务传播超时策略；不改变取消、重试次数或业务时间窗口。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_enabled = ContextVar("planning_timeouts_enabled", default=True)


def timeouts_enabled() -> bool:
    return _enabled.get()


@contextmanager
def timeout_policy(enabled: bool):
    token = _enabled.set(enabled)
    try:
        yield
    finally:
        _enabled.reset(token)


def planning_timeout_scope(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        with timeout_policy(self.enforce_timeouts):
            result = await method(self, *args, **kwargs)
            if result.backfill is not None:
                runner = result.backfill
                async def backfill(*runner_args, **runner_kwargs):
                    with timeout_policy(self.enforce_timeouts):
                        return await runner(*runner_args, **runner_kwargs)
                result.backfill = backfill
            return result
    return wrapped


def deadline_after(start: float, seconds: float) -> float:
    # infinity 仅用于内部比较，绝不传入线程等待、HTTP参数或JSON。
    return start + seconds if timeouts_enabled() else float("inf")


def wait_timeout(seconds: float | None) -> float | None:
    return seconds if timeouts_enabled() else None
