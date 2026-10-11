"""限制 planning-v4 实际运行中的同步 MCP 调用并保留安全观测。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import ContextVar, copy_context
import math
import re
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Mapping

from .observation_service import span


ToolCallable = Callable[[str, dict[str, Any]], Any]
_before_actual_call: ContextVar[Callable[[], bool] | None] = ContextVar("mcp_before_actual_call", default=None)
_SENSITIVE_KEY = re.compile(r"key|token|secret|authorization|password|credential", re.I)
_SAFE_ARGUMENT_KEYS = {
    "keywords", "city", "citylimit", "types", "page", "offset", "extensions",
    "location", "radius", "sortrule", "id",
}


def _safe_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in arguments.items()
        if str(key).lower() in _SAFE_ARGUMENT_KEYS
        and not _SENSITIVE_KEY.search(str(key))
        and (value is None or isinstance(value, (str, int, float, bool)))
    }


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        status = value.get("result_status")
        if status in {"ok", "empty", "error"}:
            return str(status)
        return "error" if value.get("error") else "returned"
    return "returned"


class _CallState:
    """Linearizes cancellation against the start of the underlying tool call."""

    def __init__(self, cancel_event: Event | None = None):
        self._lock = Lock()
        self._started = False
        self._cancelled = False
        self._cancel_event = cancel_event

    def cancel_before_start(self) -> None:
        with self._lock:
            if not self._started:
                self._cancelled = True

    def can_start(self, deadline: float) -> bool:
        with self._lock:
            return not (
                self._cancelled
                or (self._cancel_event and self._cancel_event.is_set())
                or monotonic() >= deadline
            )

    def try_start(self, deadline: float) -> bool:
        with self._lock:
            if self._started:
                return True
            if (self._cancelled or (self._cancel_event and self._cancel_event.is_set())
                    or monotonic() >= deadline):
                return False
            self._started = True
            return True


class MCPToolExecutor:
    """工作线程拥有并发名额；等待方超时或取消不会提前释放名额。"""

    def __init__(
        self, tool_callable: ToolCallable, *, max_workers: int = 4, defer_start: bool = False,
    ):
        if type(max_workers) is not int or max_workers != 4:
            raise ValueError("planning-v4 MCP 执行器固定使用4个并发名额")
        self._tool_callable = tool_callable
        self._defer_start = defer_start
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="planning-mcp")

    def _run_actual(self, tool_name: str, arguments: dict[str, Any], deadline: float, state: _CallState) -> Any:
        if self._defer_start:
            allowed = state.can_start(deadline)
        else:
            allowed = state.try_start(deadline)
        if not allowed:
            raise TimeoutError("地图工具调用已超时或取消，未启动")
        started = monotonic()
        safe_args = _safe_arguments(arguments)
        token = _before_actual_call.set(lambda: state.try_start(deadline))
        try:
            with span("tool.mcp_call", "tool", {"tool_name": tool_name, "arguments": safe_args}) as record:
                value = self._tool_callable(tool_name, arguments)
                record.output = {
                    "tool_name": tool_name,
                    "status": _status(value),
                    "elapsed_seconds": round(monotonic() - started, 3),
                }
                return value
        finally:
            _before_actual_call.reset(token)

    def _submit(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds 必须是有限正数")
        # Copy the caller's observation context into the worker so the MCP span
        # stays open until the real synchronous operation exits, even if its
        # caller has already timed out.
        context = copy_context()
        deadline = monotonic() + float(timeout_seconds)
        state = _CallState(cancel_event)
        future = self._pool.submit(
            context.run, self._run_actual, tool_name, dict(arguments), deadline, state,
        )
        return future, deadline, state

    def run(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ) -> Any:
        future, deadline, state = self._submit(
            tool_name, arguments, timeout_seconds, cancel_event,
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                state.cancel_before_start()
                # Cancelling a queued future prevents a late provider call.
                # A running provider call keeps its worker slot until it exits.
                future.cancel()
                raise TimeoutError("地图工具调用已取消")
            remaining = deadline - monotonic()
            if remaining <= 0:
                state.cancel_before_start()
                future.cancel()
                raise TimeoutError("地图工具调用超时")
            try:
                return future.result(timeout=min(0.05, remaining))
            except FutureTimeoutError:
                if future.done():
                    # The worker may itself raise TimeoutError. Since
                    # FutureTimeoutError aliases it, surface that exception
                    # instead of treating a completed failure as still queued.
                    return future.result()
                continue

    async def run_async(
        self, tool_name: str, arguments: dict[str, Any], *, timeout_seconds: float,
    ) -> Any:
        future, deadline, state = self._submit(tool_name, arguments, timeout_seconds)
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped), timeout=max(0.0, deadline - monotonic()),
            )
        except asyncio.TimeoutError as exc:
            if future.done():
                return future.result()
            state.cancel_before_start()
            future.cancel()
            raise TimeoutError("地图工具调用超时") from exc
        except asyncio.CancelledError:
            state.cancel_before_start()
            future.cancel()
            raise

    def close(self, *, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=False)


_shared_executor: MCPToolExecutor | None = None
_shared_executor_lock = Lock()


def get_mcp_tool_executor() -> MCPToolExecutor:
    """返回进程级四并发执行器，供 planning-v4 角色共享。"""
    global _shared_executor
    if _shared_executor is not None:
        return _shared_executor
    with _shared_executor_lock:
        if _shared_executor is None:
            # MCP discovery can be slow. Do it after a worker acquires a slot so
            # initialization is covered by the same absolute call deadline.
            tool_lock = Lock()
            tool = None

            def invoke(tool_name: str, arguments: dict[str, Any]) -> Any:
                nonlocal tool
                if tool is None:
                    with tool_lock:
                        if tool is None:
                            from .amap_service import get_amap_mcp_tool

                            tool = get_amap_mcp_tool()
                before_call = _before_actual_call.get()
                if before_call is not None and not before_call():
                    raise TimeoutError("地图工具调用已超时或取消，未启动")
                return tool.run({"action": "call_tool", "tool_name": tool_name, "arguments": arguments})

            _shared_executor = MCPToolExecutor(invoke, defer_start=True)
    return _shared_executor
