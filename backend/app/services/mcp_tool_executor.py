"""限制 planning-v4 实际运行中的同步 MCP 调用并保留安全观测。"""
from __future__ import annotations

from app.services.planning_timeout_policy import deadline_after, wait_timeout, timeouts_enabled
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import math
import re
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Mapping

from .observation_service import span
from .tool_priority import (
    PriorityLease,
    ToolPriority,
    current_priority_lease,
    reset_priority_lease,
    set_priority_lease,
)


ToolCallable = Callable[[str, dict[str, Any]], Any]
_before_actual_call: ContextVar[Callable[[], bool] | None] = ContextVar("mcp_before_actual_call", default=None)
_SENSITIVE_KEY = re.compile(r"key|token|secret|authorization|password|credential", re.I)
_SAFE_ARGUMENT_KEYS = {
    "keywords", "city", "citylimit", "types", "page", "offset", "extensions",
    "location", "radius", "sortrule", "id",
}


def _set_explicit_priority(priority: ToolPriority):
    if priority not in {"interactive", "background"}:
        raise ValueError("priority 必须是 interactive 或 background")
    return set_priority_lease(PriorityLease(priority))


def _reset_explicit_priority(token) -> None:
    reset_priority_lease(token)


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
        self.ticket: _AdmissionTicket | None = None

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


@dataclass(eq=False, slots=True)
class _AdmissionTicket:
    sequence: int
    priority: PriorityLease
    ready: Future[None]
    state: str = "waiting"
    wake: Callable[[], None] | None = None


class _PriorityAdmission:
    """在线程池前按优先级准入；待办请求不会占用4个worker。"""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._lock = Lock()
        self._next_sequence = 0
        self._active = 0
        self._tickets: list[_AdmissionTicket] = []

    def enqueue(self, priority: PriorityLease) -> _AdmissionTicket:
        with self._lock:
            ticket = _AdmissionTicket(self._next_sequence, priority, Future())
            self._next_sequence += 1
            self._tickets.append(ticket)
        # Promotion can happen while the ticket waits behind interactive work.
        # Register before draining so a concurrent promotion either wakes this
        # queue or is observed by the drain below.
        ticket.wake = self._drain
        priority.add_waker(ticket.wake)
        self._drain()
        return ticket

    def has_interactive_work(self) -> bool:
        with self._lock:
            return any(
                ticket.priority.priority == "interactive"
                and ticket.state in {"waiting", "granted", "dispatched"}
                for ticket in self._tickets
            )

    def has_interactive_waiter(self) -> bool:
        with self._lock:
            return any(
                ticket.priority.priority == "interactive" and ticket.state == "waiting"
                for ticket in self._tickets
            )

    def mark_dispatched(self, ticket: _AdmissionTicket) -> bool:
        with self._lock:
            if ticket.state != "granted":
                return False
            ticket.state = "dispatched"
            return True

    def cancel_before_dispatch(self, ticket: _AdmissionTicket) -> None:
        should_drain = False
        with self._lock:
            if ticket.state == "waiting":
                ticket.state = "cancelled"
                self._tickets.remove(ticket)
                should_drain = True
            elif ticket.state == "granted":
                ticket.state = "cancelled"
                self._tickets.remove(ticket)
                self._active -= 1
                should_drain = True
        ticket.ready.cancel()
        self._remove_waker(ticket)
        if should_drain:
            self._drain()

    def release(self, ticket: _AdmissionTicket) -> None:
        should_drain = False
        with self._lock:
            if ticket.state in {"granted", "dispatched"}:
                ticket.state = "released"
                self._tickets.remove(ticket)
                self._active -= 1
                should_drain = True
        self._remove_waker(ticket)
        if should_drain:
            self._drain()

    @staticmethod
    def _remove_waker(ticket: _AdmissionTicket) -> None:
        wake = ticket.wake
        if wake is not None:
            ticket.priority.remove_waker(wake)
            ticket.wake = None

    def _drain(self) -> None:
        granted: list[_AdmissionTicket] = []
        with self._lock:
            while self._active < self._capacity:
                waiting = [ticket for ticket in self._tickets if ticket.state == "waiting"]
                if not waiting:
                    break
                interactive_active = any(
                    ticket.state in {"granted", "dispatched"}
                    and ticket.priority.priority == "interactive"
                    for ticket in self._tickets
                )
                interactive_waiting = [
                    ticket for ticket in waiting if ticket.priority.priority == "interactive"
                ]
                if interactive_waiting:
                    selected = min(interactive_waiting, key=lambda item: item.sequence)
                elif interactive_active:
                    # Backfill waits until the interactive route batch is idle.
                    break
                else:
                    selected = min(waiting, key=lambda item: item.sequence)
                selected.state = "granted"
                self._active += 1
                granted.append(selected)
        for ticket in granted:
            self._remove_waker(ticket)
        for ticket in granted:
            try:
                ticket.ready.set_result(None)
            except Exception:
                # A cancelled waiter may race a grant. Its state/slot is released
                # by cancel_before_dispatch; no worker is submitted here.
                pass


class MCPToolExecutor:
    """最多4个实际调用并发；等待方取消不会提前释放已准入的调用名额。"""

    def __init__(
        self, tool_callable: ToolCallable, *, max_workers: int = 4, defer_start: bool = False,
    ):
        if type(max_workers) is not int or max_workers != 4:
            raise ValueError("planning-v4 MCP 执行器固定使用4个并发名额")
        self._tool_callable = tool_callable
        self._defer_start = defer_start
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="planning-mcp")
        self._admission = _PriorityAdmission(max_workers)

    @property
    def has_interactive_work(self) -> bool:
        """是否存在已准入或排队的交互MCP工作。"""
        return self._admission.has_interactive_work()

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
        # Copy the caller's observation context through priority admission and
        # into the worker. Pending requests do not occupy a pool worker.
        context = copy_context()
        deadline = deadline_after(monotonic(), float(timeout_seconds))
        state = _CallState(cancel_event)
        lease = current_priority_lease() or PriorityLease("interactive")
        ticket = self._admission.enqueue(lease)
        state.ticket = ticket
        future: Future[Any] = Future()

        def dispatch(_ready: Future[None]) -> None:
            if _ready.cancelled():
                self._admission.cancel_before_dispatch(ticket)
                return
            if future.cancelled():
                self._admission.cancel_before_dispatch(ticket)
                return
            if not self._admission.mark_dispatched(ticket):
                if not future.done():
                    future.set_exception(TimeoutError("地图工具调用已超时或取消，未启动"))
                return
            if not state.can_start(deadline):
                self._admission.release(ticket)
                if not future.done():
                    future.set_exception(TimeoutError("地图工具调用已超时或取消，未启动"))
                return
            try:
                actual = self._pool.submit(
                    context.run, self._run_actual, tool_name, dict(arguments), deadline, state,
                )
            except BaseException as exc:
                self._admission.release(ticket)
                if not future.done():
                    future.set_exception(exc)
                return

            def transfer_result(completed: Future[Any]) -> None:
                try:
                    if completed.cancelled():
                        if not future.done():
                            future.cancel()
                    else:
                        error = completed.exception()
                        if error is not None:
                            if not future.done():
                                future.set_exception(error)
                        elif not future.done():
                            future.set_result(completed.result())
                finally:
                    # The synchronous MCP operation has exited before the
                    # global slot can be reused.
                    self._admission.release(ticket)

            actual.add_done_callback(transfer_result)

        ticket.ready.add_done_callback(dispatch)
        return future, deadline, state

    def run(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
        cancel_event: Event | None = None,
        priority: ToolPriority | None = None,
    ) -> Any:
        token = None
        if priority is not None:
            token = _set_explicit_priority(priority)
        try:
            future, deadline, state = self._submit(
                tool_name, arguments, timeout_seconds, cancel_event,
            )
        finally:
            if token is not None:
                _reset_explicit_priority(token)
        ticket = state.ticket
        while True:
            if cancel_event is not None and cancel_event.is_set():
                state.cancel_before_start()
                future.cancel()
                if ticket is not None:
                    self._admission.cancel_before_dispatch(ticket)
                raise TimeoutError("地图工具调用已取消")
            remaining = deadline - monotonic()
            if remaining <= 0:
                state.cancel_before_start()
                future.cancel()
                if ticket is not None:
                    self._admission.cancel_before_dispatch(ticket)
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
        priority: ToolPriority | None = None,
    ) -> Any:
        token = None
        if priority is not None:
            token = _set_explicit_priority(priority)
        try:
            future, deadline, state = self._submit(tool_name, arguments, timeout_seconds)
        finally:
            if token is not None:
                _reset_explicit_priority(token)
        ticket = state.ticket
        wrapped = asyncio.wrap_future(future)

        def consume_unawaited(done: asyncio.Future[Any]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except asyncio.CancelledError:
                pass

        # Cancellation can detach the caller after the worker has already
        # completed with an exception. Retrieving it here suppresses only the
        # event-loop warning; an active awaiter still receives the exception.
        wrapped.add_done_callback(consume_unawaited)
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped), timeout=wait_timeout(max(0.0, deadline - monotonic())),
            )
        except asyncio.TimeoutError as exc:
            if future.done():
                return future.result()
            state.cancel_before_start()
            future.cancel()
            if ticket is not None:
                self._admission.cancel_before_dispatch(ticket)
            raise TimeoutError("地图工具调用超时") from exc
        except asyncio.CancelledError:
            state.cancel_before_start()
            future.cancel()
            if ticket is not None:
                self._admission.cancel_before_dispatch(ticket)
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
