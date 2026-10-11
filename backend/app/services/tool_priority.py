"""MCP 请求优先级上下文；同键查询可把排队中的后台请求提升为交互请求。"""
from __future__ import annotations

from contextvars import ContextVar, Token
from threading import Lock
from typing import Callable, Literal


ToolPriority = Literal["interactive", "background"]


class PriorityLease:
    """共享请求的单向优先级租约；只允许从后台提升成交互。"""

    def __init__(self, priority: ToolPriority):
        if priority not in {"interactive", "background"}:
            raise ValueError("priority 必须是 interactive 或 background")
        self._lock = Lock()
        self._priority: ToolPriority = priority
        self._wakers: set[Callable[[], None]] = set()

    @property
    def priority(self) -> ToolPriority:
        with self._lock:
            return self._priority

    def promote(self) -> None:
        with self._lock:
            if self._priority == "interactive":
                return
            self._priority = "interactive"
            wakers = tuple(self._wakers)
        # Never call into the admission scheduler while holding the lease lock.
        # The scheduler reads priority under its own lock, so callbacks must not
        # create an admission-lock -> lease-lock / lease-lock -> admission-lock cycle.
        for wake in wakers:
            wake()

    def add_waker(self, wake: Callable[[], None]) -> None:
        with self._lock:
            self._wakers.add(wake)

    def remove_waker(self, wake: Callable[[], None]) -> None:
        with self._lock:
            self._wakers.discard(wake)


_priority_lease: ContextVar[PriorityLease | None] = ContextVar(
    "planning_mcp_priority_lease", default=None,
)


def current_priority_lease() -> PriorityLease | None:
    return _priority_lease.get()


def set_priority_lease(lease: PriorityLease) -> Token:
    return _priority_lease.set(lease)


def reset_priority_lease(token: Token) -> None:
    _priority_lease.reset(token)
