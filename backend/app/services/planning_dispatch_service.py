"""F05b 主智能体派发、子任务调度与事实收集。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from threading import Event
from typing import Any, Callable, Literal, Mapping

from pydantic import Field, model_validator

from app.agents.execution import PlanningError
from app.agents.candidate_identity import version_candidate_ref
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.facts import FactSource, FactStore, PlaceFact, RouteFact, WeatherFact
from app.models.planning_v4.input import PlanningInput
from app.models.planning_v4.dispatch_scope import (
    DispatchScope,
    derived_dispatch_anchors,
    dispatch_scope_supports_area,
)
from app.models.planning_v4.plan import AreaHint
from app.models.planning_v4.tasks import CandidateRef, Subtask, TaskResult
from app.services.endpoint_verification_service import (
    EndpointVerificationContext,
    EndpointVerificationExecution,
    EndpointVerificationService,
)
from app.services.observation_service import span
from app.services.weather_query_service import (
    WeatherQueryContext,
    WeatherQueryExecution,
    WeatherQueryService,
)


_ROLE_CATEGORY = {"attractions": "sightseeing", "dining": "meal", "lodging": "lodging"}
_ROLE_PERIODS = {
    "attractions": {"morning", "afternoon", "evening"},
    "dining": {"breakfast", "lunch", "dinner"},
    "lodging": set(),
}


class DispatchError(ContractModel):
    code: str = Field(min_length=1)
    step: str = Field(min_length=1)
    message: str = Field(min_length=1)


class TaskDispatchState(ContractModel):
    task_id: str = Field(min_length=1)
    role: Literal["attractions", "dining", "lodging"]
    status: Literal["complete", "partial", "failed", "waiting", "blocked", "duplicate"]
    reason: str | None = None


class DispatchIssue(ContractModel):
    scope: Literal["task", "weather", "endpoint", "dependency", "scheduler"]
    reference_id: str | None = None
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class PlanningDispatchContext(ContractModel):
    """一次规划的固定身份和当前已收集快照。版本在整次调度中保持不变。"""

    planning_input: PlanningInput
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    base_revision: int | None = Field(default=None, ge=1)
    fact_store: FactStore = Field(default_factory=FactStore)
    candidate_refs: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    existing_tasks: tuple[Subtask, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    dispatch_scope: DispatchScope | None = None

    @property
    def request_id(self) -> str:
        return self.planning_input.request.request_id

    @model_validator(mode="after")
    def validate_snapshot_identity(self):
        request_id = self.planning_input.request.request_id
        if any(item.run_id != self.run_id or item.context_revision != self.context_revision
               or item.base_revision != self.base_revision for item in self.existing_tasks):
            raise ValueError("初始子任务必须属于本次固定规划版本")
        if any(item.run_id != self.run_id or item.context_revision != self.context_revision
               or item.base_revision != self.base_revision
               or (item.request_id is not None and item.request_id != request_id)
               for item in self.task_results):
            raise ValueError("初始任务结果必须属于本次固定规划版本")
        return self


class PlanningDispatchResult(ContractModel):
    request_id: str
    run_id: str
    context_revision: int
    base_revision: int | None
    action: Literal["delegate", "plan"] | None
    stop_reason: Literal["plan", "no_progress", "deadline", "technical_error"]
    reason: str
    missing_information: tuple[str, ...] = ()
    fact_store: FactStore = Field(default_factory=FactStore)
    candidate_refs: tuple[CandidateRef, ...] = ()
    area_hints: tuple[AreaHint, ...] = ()
    existing_tasks: tuple[Subtask, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    weather_results: tuple[WeatherQueryExecution, ...] = ()
    endpoint_results: tuple[EndpointVerificationExecution, ...] = ()
    weather_attempted_dates: tuple[date, ...] = ()
    endpoint_attempted: tuple[Literal["arrival", "departure"], ...] = ()
    task_states: tuple[TaskDispatchState, ...] = ()
    issues: tuple[DispatchIssue, ...] = ()
    error: DispatchError | None = None


class _DispatchFault(RuntimeError):
    def __init__(self, code: str, step: str, message: str):
        super().__init__(message)
        self.code = code
        self.step = step
        self.message = message


@dataclass
class _MutableState:
    context: PlanningDispatchContext
    fact_store: FactStore
    candidate_refs: list[CandidateRef]
    area_hints: list[AreaHint]
    existing_tasks: list[Subtask]
    task_results: list[TaskResult]
    weather_results: list[WeatherQueryExecution] = field(default_factory=list)
    endpoint_results: list[EndpointVerificationExecution] = field(default_factory=list)
    direct_query_summaries: list[dict[str, Any]] = field(default_factory=list)
    weather_attempted_dates: set[date] = field(default_factory=set)
    endpoint_attempted: set[str] = field(default_factory=set)
    task_states: dict[str, TaskDispatchState] = field(default_factory=dict)
    issues: list[DispatchIssue] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    task_fingerprints: dict[str, str] = field(default_factory=dict)
    task_by_id: dict[str, Subtask] = field(default_factory=dict)
    result_by_task_id: dict[str, TaskResult] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Job:
    kind: Literal["task", "weather", "endpoint"]
    reference_id: str
    subtask: Subtask | None = None
    resolved_task: Any | None = None
    dates: tuple[date, ...] = ()
    endpoints: tuple[str, ...] = ()
    snapshot: Any | None = None


class PlanningDispatchService:
    """顺序续派主决定，并发运行就绪查询，返回可由 F06 消费的收集结果。"""

    def __init__(
        self,
        *,
        decider=None,
        decision_context_factory: Callable[..., Any] | None = None,
        runner_factories: Mapping[str, Callable[[], Any]] | None = None,
        weather_service_factory: Callable[[], Any] | None = None,
        endpoint_service_factory: Callable[[], Any] | None = None,
        max_concurrency: int = 3,
        total_timeout_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency 必须是正整数")
        if (isinstance(total_timeout_seconds, bool)
                or not isinstance(total_timeout_seconds, (int, float))
                or not math.isfinite(total_timeout_seconds) or total_timeout_seconds <= 0):
            raise ValueError("total_timeout_seconds 必须是有限正数")
        self.decider = decider
        self.decision_context_factory = decision_context_factory
        self.runner_factories = dict(runner_factories or {})
        self.weather_service_factory = weather_service_factory
        self.endpoint_service_factory = endpoint_service_factory
        self.max_concurrency = max_concurrency
        self.total_timeout_seconds = float(total_timeout_seconds)
        self.clock = clock

    async def run_async(self, context: PlanningDispatchContext) -> PlanningDispatchResult:
        """调度结束返回快照；外部取消会传播 CancelledError。"""
        state = _MutableState(
            context=context,
            fact_store=context.fact_store,
            candidate_refs=list(context.candidate_refs),
            area_hints=list(context.area_hints),
            existing_tasks=list(context.existing_tasks),
            task_results=list(context.task_results),
        )
        state.task_by_id.update((item.task_id, item) for item in state.existing_tasks)
        state.result_by_task_id.update((item.task_id, item) for item in state.task_results)
        initial_error = None
        try:
            self._validate_initial_snapshot(state)
            for task in state.existing_tasks:
                result = state.result_by_task_id.get(task.task_id)
                if result is None:
                    state.task_states[task.task_id] = TaskDispatchState(
                        task_id=task.task_id, role=task.role, status="waiting",
                        reason="当前快照尚无任务结果。",
                    )
                    continue
                state.task_states[task.task_id] = TaskDispatchState(
                    task_id=task.task_id, role=task.role, status=result.status,
                    reason=result.stop_reason,
                )
                if result.status != "complete":
                    self._append_missing(state, self._missing_from_task(task, result))
        except _DispatchFault as exc:
            initial_error = exc
        state.task_fingerprints.update(
            (self._task_fingerprint(task, state), task.task_id) for task in state.existing_tasks
        )
        started = self.clock()
        deadline = started + self.total_timeout_seconds
        safe_input = {
            "request_id": context.request_id,
            "run_id": context.run_id,
            "context_revision": context.context_revision,
            "base_revision": context.base_revision,
            "initial_fact_count": len(context.fact_store.places) + len(context.fact_store.weather) + len(context.fact_store.routes),
            "initial_candidate_count": len(context.candidate_refs),
        }

        with span("planning.dispatch", "planning", safe_input) as dispatch_span:
            try:
                if initial_error is not None:
                    raise initial_error
                result = await self._run_loop(state, deadline)
            except asyncio.CancelledError:
                dispatch_span.output = {"status": "cancelled", "late_results_written": False}
                raise
            except _DispatchFault as exc:
                result = self._result(
                    state,
                    action=None,
                    stop_reason="technical_error",
                    reason="调度遇到结构或身份校验错误，保留此前已核实结果。",
                    error=DispatchError(code=exc.code, step=exc.step, message=exc.message),
                )
            except Exception:
                result = self._result(
                    state,
                    action=None,
                    stop_reason="technical_error",
                    reason="调度发生未分类技术错误，保留此前已核实结果。",
                    error=DispatchError(
                        code="DISPATCH_INTERNAL_ERROR",
                        step="planning_dispatch",
                        message="规划调度执行失败",
                    ),
                )
            dispatch_span.output = {
                "action": result.action,
                "stop_reason": result.stop_reason,
                "place_fact_count": len(result.fact_store.places),
                "weather_fact_count": len(result.fact_store.weather),
                "route_fact_count": len(result.fact_store.routes),
                "candidate_count": len(result.candidate_refs),
                "task_result_count": len(result.task_results),
                "issue_count": len(result.issues),
                "error_code": result.error.code if result.error else None,
            }
            return result

    async def _run_loop(self, state: _MutableState, deadline: float) -> PlanningDispatchResult:
        last_action: str | None = None
        while True:
            if self.clock() >= deadline:
                return self._deadline_result(state, last_action)
            try:
                decision_execution = await self._decide(state, deadline)
            except asyncio.TimeoutError:
                return self._deadline_result(state, last_action)
            decision = getattr(decision_execution, "decision", decision_execution)
            self._validate_decision_identity(state.context, decision)
            last_action = decision.action
            if decision.action == "plan":
                self._append_missing(state, decision.missing_information)
                return self._result(
                    state, action="plan", stop_reason="plan",
                    reason=decision.reason,
                )

            self._validate_delegate_identity(
                state.context,
                decision,
                area_hints=tuple(state.area_hints),
                live_context=self._make_decision_context(state),
            )
            self._merge_areas(state, decision.areas)
            ran, timed_out = await self._run_delegate(state, decision, deadline)
            if timed_out:
                return self._deadline_result(state, "delegate")
            if state.issues and any(issue.scope == "scheduler" for issue in state.issues):
                issue = next(issue for issue in state.issues if issue.scope == "scheduler")
                raise _DispatchFault(issue.code, "planning_dispatch", issue.message)
            if not ran:
                return self._result(
                    state, action="delegate", stop_reason="no_progress",
                    reason="本轮没有可执行的新查询或子任务，已保留可用信息并结束收集。",
                )

    async def _decide(self, state: _MutableState, deadline: float):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise asyncio.TimeoutError
        decision_context = self._make_decision_context(state)
        decider = self.decider or self._new_decider()
        cancel_event = Event()
        try:
            from app.agents.master_decider import MasterDecider

            if isinstance(decider, MasterDecider):
                invocation = asyncio.to_thread(
                    decider.run,
                    decision_context,
                    request_timeout_seconds=min(120.0, remaining),
                    cancel_event=cancel_event,
                )
            else:
                invocation = asyncio.to_thread(decider.run, decision_context)
            return await asyncio.wait_for(
                invocation,
                timeout=remaining,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except asyncio.TimeoutError:
            cancel_event.set()
            raise
        except PlanningError as exc:
            if (getattr(exc, "code", None) == "MASTER_DECISION_TIMEOUT"
                    and self.clock() >= deadline):
                raise asyncio.TimeoutError from None
            raise _DispatchFault(
                getattr(exc, "code", "MASTER_DECISION_FAILED"),
                getattr(exc, "step", None) or "master_decision",
                "主智能体决策未能完成。",
            ) from None
        except Exception:
            raise _DispatchFault(
                "MASTER_DECISION_FAILED", "master_decision", "主智能体决策未能完成。",
            ) from None

    def _make_decision_context(self, state: _MutableState):
        from app.agents.master_decider import (
            DirectQuerySummary,
            MasterDecisionContext,
        )

        query_summaries = tuple(DirectQuerySummary(**item) for item in state.direct_query_summaries)
        fields = {
            "planning_input": state.context.planning_input,
            "fact_store": state.fact_store,
            "candidates": tuple(state.candidate_refs),
            "area_hints": tuple(state.area_hints),
            "existing_tasks": tuple(state.existing_tasks),
            "task_results": tuple(state.task_results),
            "run_id": state.context.run_id,
            "context_revision": state.context.context_revision,
            "base_revision": state.context.base_revision,
            "request_id": state.context.request_id,
            "weather_attempted_dates": tuple(sorted(state.weather_attempted_dates)),
            "endpoint_attempted": tuple(sorted(state.endpoint_attempted)),
            "direct_query_summaries": query_summaries,
            "dispatch_scope": state.context.dispatch_scope,
        }
        factory = self.decision_context_factory
        if factory is not None:
            return factory(**fields)
        return MasterDecisionContext(**fields)

    def _new_decider(self):
        from app.agents.master_decider import MasterDecider

        return MasterDecider()

    def _validate_decision_identity(self, context: PlanningDispatchContext, decision: Any) -> None:
        if (getattr(decision, "request_id", None) != context.request_id
                or getattr(decision, "run_id", None) != context.run_id
                or getattr(decision, "context_revision", None) != context.context_revision
                or getattr(decision, "base_revision", None) != context.base_revision):
            raise _DispatchFault(
                "MASTER_DECISION_IDENTITY_MISMATCH", "master_decision",
                "主智能体决策身份或版本与当前请求不匹配。",
            )
        if getattr(decision, "rule_version", None) != context.planning_input.rules.rule_version:
            raise _DispatchFault(
                "MASTER_DECISION_RULE_MISMATCH", "master_decision",
                "主智能体决策规则版本与当前输入不匹配。",
            )
        if getattr(decision, "action", None) not in {"delegate", "plan"}:
            raise _DispatchFault("MASTER_DECISION_INVALID", "master_decision", "主智能体决策类型无效。")

    @staticmethod
    def _validate_delegate_identity(
        context: PlanningDispatchContext,
        decision: Any,
        *,
        area_hints: tuple[AreaHint, ...] = (),
        live_context: Any | None = None,
    ) -> None:
        valid_dates = {item.day_date for item in context.planning_input.days}
        dates = tuple(decision.weather_dates)
        if context.dispatch_scope is not None and (
            dates or decision.verify_arrival_place or decision.verify_departure_place
        ):
            raise _DispatchFault(
                "DISPATCH_SCOPE_DIRECT_QUERY_INVALID", "master_decision",
                "定向补查不能包含天气或到离端点查询。",
            )
        if len(dates) != len(set(dates)) or not set(dates).issubset(valid_dates):
            raise _DispatchFault("MASTER_WEATHER_SCOPE_INVALID", "master_decision", "天气任务超出旅行日期范围。")
        request = context.planning_input.request
        if decision.verify_arrival_place and not request.arrival_place_query:
            raise _DispatchFault("MASTER_ENDPOINT_SCOPE_INVALID", "master_decision", "决策要求核实未填写的到达地点。")
        if decision.verify_departure_place and not request.departure_place_query:
            raise _DispatchFault("MASTER_ENDPOINT_SCOPE_INVALID", "master_decision", "决策要求核实未填写的离开地点。")
        if context.dispatch_scope is not None:
            live_anchors = list(context.dispatch_scope.anchors)
            if live_context is not None:
                live_anchors.extend(derived_dispatch_anchors(
                    context.dispatch_scope,
                    context.planning_input,
                    live_context.fact_store,
                    tuple(live_context.candidates),
                    tuple(live_context.existing_tasks),
                    tuple(live_context.task_results),
                    run_id=context.run_id,
                    context_revision=context.context_revision,
                    base_revision=context.base_revision,
                ))
            for area in decision.areas:
                if not dispatch_scope_supports_area(
                    context.dispatch_scope,
                    tuple(live_anchors),
                    area.basis_fact_ids,
                    area.anchor_poi_ids,
                ):
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_AREA_INVALID", "master_decision",
                        "新区域的全部事实依据必须属于同一个定向目标日期。",
                    )
        local_refs: set[str] = set()
        task_ids: set[str] = set()
        for resolved in decision.tasks:
            task = resolved.subtask
            if (task.run_id != context.run_id or task.context_revision != context.context_revision
                    or task.base_revision != context.base_revision):
                raise _DispatchFault("MASTER_TASK_IDENTITY_MISMATCH", "master_decision", "派发子任务身份或版本不匹配。")
            if resolved.local_ref in local_refs or task.task_id in task_ids:
                raise _DispatchFault("MASTER_TASK_DUPLICATE_ID", "master_decision", "派发子任务标识重复。")
            local_refs.add(resolved.local_ref)
            task_ids.add(task.task_id)
            if context.dispatch_scope is not None:
                live_anchors = list(context.dispatch_scope.anchors)
                if live_context is not None:
                    live_anchors.extend(derived_dispatch_anchors(
                        context.dispatch_scope,
                        context.planning_input,
                        live_context.fact_store,
                        tuple(live_context.candidates),
                        tuple(live_context.existing_tasks),
                        tuple(live_context.task_results),
                        run_id=context.run_id,
                        context_revision=context.context_revision,
                        base_revision=context.base_revision,
                    ))
                live_anchors_by_key = {
                    (item.day_id, item.place_fact_id): item for item in live_anchors
                }
                targets = tuple(
                    target for target in context.dispatch_scope.targets
                    if target.role == task.role
                )
                if not targets:
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_ROLE_INVALID", "master_decision",
                        "子任务角色超出当前定向目标。",
                    )
                allowed_days = {target.day_id for target in targets}
                allowed_slots = {slot_id for target in targets for slot_id in target.slot_ids}
                allowed_requirements = {
                    requirement_id for target in targets for requirement_id in target.requirement_ids
                }
                if (not set(task.scope.day_ids).issubset(allowed_days)
                        or not set(task.scope.slot_ids).issubset(allowed_slots)
                        or not set(task.requirement_ids).issubset(allowed_requirements)):
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_TASK_INVALID", "master_decision",
                        "子任务日期、时段或硬性要求超出当前定向目标。",
                    )
                slot_by_id = {
                    slot.slot_id: slot
                    for day in context.planning_input.days
                    for slot in day.slots
                }
                for day_id in task.scope.day_ids:
                    if not any(target.day_id == day_id for target in targets):
                        raise _DispatchFault(
                            "DISPATCH_SCOPE_DAY_INVALID", "master_decision",
                            "子任务日期没有对应的定向缺项。",
                        )
                for slot_id in task.scope.slot_ids:
                    slot = slot_by_id.get(slot_id)
                    if (slot is None or slot.day_id not in task.scope.day_ids
                            or slot.period not in _ROLE_PERIODS[task.role]
                            or slot.eligibility == "skipped"
                            or not any(
                                target.day_id == slot.day_id and slot_id in target.slot_ids
                                for target in targets
                            )):
                        raise _DispatchFault(
                            "DISPATCH_SCOPE_SLOT_INVALID", "master_decision",
                            "子任务时段与定向目标日期不匹配。",
                        )
                for requirement_id in task.requirement_ids:
                    if not any(
                        target.day_id in task.scope.day_ids
                        and requirement_id in target.requirement_ids
                        and (not target.slot_ids or set(target.slot_ids) & set(task.scope.slot_ids))
                        for target in targets
                    ):
                        raise _DispatchFault(
                            "DISPATCH_SCOPE_REQUIREMENT_INVALID", "master_decision",
                            "子任务硬性要求与定向日期或时段不匹配。",
                        )
                if task.scope.slot_ids or task.requirement_ids:
                    if not any(
                        target.day_id in task.scope.day_ids
                        and (
                            set(target.slot_ids) & set(task.scope.slot_ids)
                            or set(target.requirement_ids) & set(task.requirement_ids)
                        )
                        for target in targets
                    ):
                        raise _DispatchFault(
                            "DISPATCH_SCOPE_EMPTY", "master_decision",
                            "子任务没有覆盖任何定向缺项。",
                        )
                elif not any(
                    target.role == "lodging" and target.day_id in task.scope.day_ids
                    for target in targets
                ):
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_EMPTY", "master_decision",
                        "子任务没有覆盖任何定向缺项。",
                    )

                scoped_days = set(task.scope.day_ids)
                scoped_anchor_facts = {
                    anchor.place_fact_id for anchor in live_anchors_by_key.values()
                    if anchor.day_id in scoped_days
                }
                scoped_anchor_pois = {
                    anchor.poi_id for anchor in live_anchors_by_key.values()
                    if anchor.day_id in scoped_days
                }
                allowed_area_ids = {
                    area_id for target in targets if target.day_id in scoped_days
                    for area_id in target.area_hint_ids
                }
                area_by_id = {
                    item.area_hint_id: item for item in (*area_hints, *decision.areas)
                }
                for area_id in tuple(allowed_area_ids):
                    area = area_by_id.get(area_id)
                    if area is not None:
                        scoped_anchor_facts.update(area.basis_fact_ids)
                        scoped_anchor_pois.update(area.anchor_poi_ids)
                for area in area_by_id.values():
                    if (area.basis_fact_ids
                            and set(area.basis_fact_ids).issubset(scoped_anchor_facts)
                            and set(area.anchor_poi_ids).issubset(scoped_anchor_pois)):
                        allowed_area_ids.add(area.area_hint_id)
                if not set(task.scope.area_hint_ids).issubset(allowed_area_ids):
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_AREA_INVALID", "master_decision",
                        "子任务区域超出当前定向锚点。",
                    )
                if any(
                    binding.anchor_poi_id not in scoped_anchor_pois
                    or binding.area_hint_id not in allowed_area_ids
                    for binding in resolved.meal_areas
                ):
                    raise _DispatchFault(
                        "DISPATCH_SCOPE_ANCHOR_INVALID", "master_decision",
                        "餐次区域或锚点超出当前定向范围。",
                    )

    async def _run_delegate(self, state: _MutableState, decision: Any, deadline: float) -> tuple[bool, bool]:
        pending: dict[str, Any] = {}
        ready_to_run: list[Any] = []
        for resolved in decision.tasks:
            subtask = resolved.subtask
            fingerprint = self._task_fingerprint(subtask, state, resolved)
            prior_id = state.task_fingerprints.get(fingerprint)
            if prior_id is not None:
                state.aliases[subtask.task_id] = self._resolve_alias(state, prior_id)
                state.task_states[subtask.task_id] = TaskDispatchState(
                    task_id=subtask.task_id, role=subtask.role, status="duplicate",
                    reason="同一语义任务已尝试，本轮跳过该任务。",
                )
                continue
            state.task_fingerprints[fingerprint] = subtask.task_id
            state.task_by_id[subtask.task_id] = subtask
            state.existing_tasks.append(subtask)
            pending[subtask.task_id] = resolved
            ready_to_run.append(resolved)

        weather_dates = tuple(sorted(set(decision.weather_dates) - state.weather_attempted_dates))
        for day in decision.weather_dates:
            state.weather_attempted_dates.add(day)
        endpoints: list[str] = []
        request = state.context.planning_input.request
        if decision.verify_arrival_place and request.arrival_place_query and "arrival" not in state.endpoint_attempted:
            endpoints.append("arrival")
        if decision.verify_departure_place and request.departure_place_query and "departure" not in state.endpoint_attempted:
            endpoints.append("departure")
        state.endpoint_attempted.update(endpoints)

        ran = False
        direct_jobs = self._direct_jobs(state, weather_dates, tuple(endpoints))
        first_wave = True
        while pending or (first_wave and direct_jobs):
            if self.clock() >= deadline:
                self._mark_pending_deadline(state, pending)
                return ran, True
            eligible: list[Any] = []
            blocked: list[tuple[str, str]] = []
            for task_id, resolved in tuple(pending.items()):
                disposition, reason = self._dependency_disposition(state, resolved.subtask, pending)
                if disposition == "ready":
                    eligible.append(resolved)
                elif disposition in {"blocked", "waiting"} and disposition == "blocked":
                    blocked.append((task_id, reason))
            for task_id, reason in blocked:
                resolved = pending.pop(task_id)
                task = resolved.subtask
                state.task_states[task_id] = TaskDispatchState(
                    task_id=task_id, role=task.role, status="blocked", reason=reason,
                )
                self._append_missing(state, (f"{task.role}任务依赖未满足：{reason}",))
                state.issues.append(DispatchIssue(
                    scope="dependency", reference_id=task_id, code="DEPENDENCY_BLOCKED", message=reason,
                ))

            jobs = [
                _Job(kind="task", reference_id=item.subtask.task_id,
                     subtask=item.subtask, resolved_task=item)
                for item in eligible
            ]
            if first_wave:
                jobs.extend(direct_jobs)
                direct_jobs = []
            first_wave = False
            if not jobs:
                for task_id, resolved in pending.items():
                    task = resolved.subtask
                    state.task_states[task_id] = TaskDispatchState(
                        task_id=task_id, role=task.role, status="waiting",
                        reason="依赖事实或前置任务尚未就绪。",
                    )
                    self._append_missing(state, (f"{task.role}任务等待未就绪依赖。",))
                return ran, False

            snapshot = self._execution_snapshot(state)
            jobs = [
                _Job(
                    kind=job.kind,
                    reference_id=job.reference_id,
                    subtask=job.subtask,
                    resolved_task=job.resolved_task,
                    dates=job.dates,
                    endpoints=job.endpoints,
                    snapshot=snapshot if job.kind == "task" else None,
                )
                for job in jobs
            ]
            results, timed_out = await self._run_wave(state, jobs, deadline)
            ran = ran or bool(jobs)
            faults: list[_DispatchFault] = []
            deadline_hit = False
            for job, value in results:
                if isinstance(value, _DispatchFault):
                    self._record_fault(state, job, value)
                    faults.append(value)
                    continue
                if isinstance(value, BaseException):
                    if isinstance(value, asyncio.CancelledError):
                        raise value
                    if isinstance(value, asyncio.TimeoutError):
                        if self.clock() >= deadline:
                            deadline_hit = True
                        else:
                            self._record_local_failure(state, job, value)
                            if job.kind == "task":
                                pending.pop(job.reference_id, None)
                        continue
                    self._record_local_failure(state, job, value)
                    if job.kind == "task":
                        pending.pop(job.reference_id, None)
                    continue
                try:
                    if job.kind == "task":
                        self._accept_task_execution(state, job, value)
                        pending.pop(job.reference_id, None)
                    elif job.kind == "weather":
                        self._accept_weather_execution(state, job, value)
                    else:
                        self._accept_endpoint_execution(state, job, value)
                except _DispatchFault as exc:
                    self._record_fault(state, job, exc)
                    faults.append(exc)
            if faults:
                raise faults[0]
            if timed_out or deadline_hit:
                self._mark_pending_deadline(state, pending)
                return ran, True
        return ran, False

    @staticmethod
    def _mark_pending_deadline(state: _MutableState, pending: Mapping[str, Any]) -> None:
        for task_id, resolved in pending.items():
            task = resolved.subtask
            state.task_states[task_id] = TaskDispatchState(
                task_id=task_id,
                role=task.role,
                status="waiting",
                reason="技术期限前未完成执行。",
            )
            state.issues.append(DispatchIssue(
                scope="task", reference_id=task_id,
                code="TASK_DEADLINE", message="子任务未在技术期限内完成。",
            ))
            PlanningDispatchService._append_missing(
                state, (f"{task.role}子任务未在技术期限内完成。",),
            )

    def _direct_jobs(self, state: _MutableState, dates: tuple[date, ...], endpoints: tuple[str, ...]) -> list[_Job]:
        jobs: list[_Job] = []
        if dates:
            jobs.append(_Job(kind="weather", reference_id=",".join(item.isoformat() for item in dates), dates=dates))
        if endpoints:
            jobs.append(_Job(kind="endpoint", reference_id=",".join(endpoints), endpoints=endpoints))
        return jobs

    async def _run_wave(self, state: _MutableState, jobs: list[_Job], deadline: float):
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def invoke(job: _Job):
            async with semaphore:
                if self.clock() >= deadline:
                    raise asyncio.TimeoutError
                if job.kind == "task":
                    return await self._run_task_job(state, job)
                if job.kind == "weather":
                    return await self._run_weather_job(state, job)
                return await self._run_endpoint_job(state, job)

        tasks = [asyncio.create_task(invoke(job)) for job in jobs]
        remaining = max(0.0, deadline - self.clock())
        try:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        ordered: list[tuple[_Job, Any]] = []
        for job, task in zip(jobs, tasks):
            if task in done:
                try:
                    ordered.append((job, task.result()))
                except asyncio.CancelledError as exc:
                    ordered.append((job, exc))
                except _DispatchFault as exc:
                    ordered.append((job, exc))
                except BaseException as exc:
                    ordered.append((job, exc))
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return ordered, True
        return ordered, False

    async def _run_task_job(self, state: _MutableState, job: _Job):
        subtask = job.subtask
        execution_context = self._make_execution_context(state, job)
        runner = self._new_runner(subtask.role)
        return await runner.run_async(subtask, execution_context)

    def _new_runner(self, role: str):
        factory = self.runner_factories.get(role)
        if factory is not None:
            return factory()
        if role == "attractions":
            from app.agents.attraction_subtask import AttractionSubtaskRunner

            return AttractionSubtaskRunner()
        if role == "dining":
            from app.agents.dining_subtask import DiningSubtaskRunner

            return DiningSubtaskRunner()
        if role == "lodging":
            from app.agents.lodging_subtask import LodgingSubtaskRunner

            return LodgingSubtaskRunner()
        raise _DispatchFault("ROLE_NOT_SUPPORTED", "planning_dispatch", "子任务角色不受支持。")

    def _make_execution_context(self, state: _MutableState, job: _Job):
        from app.agents.attraction_subtask import AttractionExecutionContext

        subtask = job.subtask
        role_candidates = self._project_candidates(
            state, subtask, resolved_task=job.resolved_task,
        )
        values = {
            "request_id": state.context.request_id,
            "run_id": state.context.run_id,
            "context_revision": state.context.context_revision,
            "base_revision": state.context.base_revision,
            "planning_input": state.context.planning_input,
            "fact_store": job.snapshot.fact_store,
            "candidate_refs": role_candidates,
            "area_hints": job.snapshot.area_hints,
            "ready_task_ids": self._ready_task_ids(state, subtask),
        }
        if subtask.role == "dining":
            from app.agents.dining_subtask import (
                DiningExecutionContext,
                DiningMealAreaBinding,
                DiningRestaurantRequirement,
            )

            resolved = job.resolved_task
            values["meal_areas"] = tuple(DiningMealAreaBinding(
                slot_id=item.slot_id,
                area_hint_id=item.area_hint_id,
                anchor_poi_id=item.anchor_poi_id,
            ) for item in resolved.meal_areas)
            values["must_eat_restaurants"] = tuple(DiningRestaurantRequirement(
                requirement_id=item.requirement_id,
                restaurant_name=item.restaurant_name,
            ) for item in resolved.must_eat_restaurants)
            return DiningExecutionContext(**values)
        if subtask.role == "lodging":
            from app.agents.lodging_subtask import LodgingExecutionContext

            return LodgingExecutionContext(**values)
        return AttractionExecutionContext(**values)

    def _execution_snapshot(self, state: _MutableState):
        @dataclass(frozen=True)
        class Snapshot:
            fact_store: FactStore
            area_hints: tuple[AreaHint, ...]

        return Snapshot(state.fact_store, tuple(state.area_hints))

    def _project_candidates(
        self, state: _MutableState, subtask: Subtask, *, resolved_task=None,
    ) -> tuple[CandidateRef, ...]:
        expected_category = _ROLE_CATEGORY[subtask.role]
        allowed_slots = self._allowed_slots(state.context.planning_input, subtask)
        allowed_requirements = set(subtask.requirement_ids)
        excluded = set(subtask.excluded_poi_ids)
        projected_by_poi: dict[str, list[tuple[CandidateRef, CandidateRef, PlaceFact]]] = {}
        fact_by_id = {item.fact_id: item for item in state.fact_store.places}
        source_by_id = {item.tool_call_id: item for item in state.fact_store.sources}
        dining_eligibility = None
        named_eligibility = None
        meal_bindings = ()
        named_restaurants = ()
        blocked_requirements = ()
        if subtask.role == "dining" and resolved_task is not None:
            from app.agents.dining_subtask import (
                DiningMealAreaBinding,
                eligible_dining_slot_ids,
                eligible_named_restaurant_requirement_ids,
            )
            meal_bindings = tuple(DiningMealAreaBinding(
                slot_id=item.slot_id,
                area_hint_id=item.area_hint_id,
                anchor_poi_id=item.anchor_poi_id,
            ) for item in resolved_task.meal_areas)
            named_restaurants = tuple(resolved_task.must_eat_restaurants)
            blocked_requirements = tuple(
                item for item in state.context.planning_input.requirements.hard_requirements
                if item.kind == "must_avoid"
            )
            dining_eligibility = eligible_dining_slot_ids
            named_eligibility = eligible_named_restaurant_requirement_ids

        for candidate in state.candidate_refs:
            if candidate.category != expected_category or candidate.poi_id in excluded:
                continue
            place = fact_by_id.get(candidate.place_fact_id)
            if place is None or place.poi_id != candidate.poi_id:
                continue
            if (candidate.area_hint_id is not None
                    and candidate.area_hint_id not in subtask.scope.area_hint_ids):
                continue

            projected_slots: set[str]
            projected_requirements = set(candidate.requirement_ids) & allowed_requirements
            if subtask.role == "dining" and dining_eligibility is not None:
                eligible_slots = dining_eligibility(
                    place,
                    city=subtask.scope.city,
                    fact_store=state.fact_store,
                    area_hints=getattr(state, "area_hints", state.context.area_hints),
                    meal_areas=meal_bindings,
                    allowed_slot_ids=allowed_slots,
                    excluded_poi_ids=excluded,
                    blocked_requirements=blocked_requirements,
                )
                projected_slots = (
                    eligible_slots if not candidate.slot_ids
                    else set(candidate.slot_ids) & eligible_slots
                )
                exact_named_ids = named_eligibility(
                    place,
                    must_eat_restaurants=named_restaurants,
                    allowed_requirement_ids=allowed_requirements,
                )
                projected_requirements &= exact_named_ids
                if not projected_slots and not projected_requirements:
                    continue
            else:
                projected_slots = (
                    set(allowed_slots) if not candidate.slot_ids
                    else set(candidate.slot_ids) & allowed_slots
                )
                if allowed_slots and not projected_slots:
                    continue

            if (candidate.slot_ids and set(candidate.slot_ids) == projected_slots
                    and len(candidate.slot_ids) == len(projected_slots)):
                projected_slot_ids = candidate.slot_ids
            else:
                projected_slot_ids = tuple(sorted(projected_slots))
            if (candidate.requirement_ids and set(candidate.requirement_ids) == projected_requirements
                    and len(candidate.requirement_ids) == len(projected_requirements)):
                projected_requirement_ids = candidate.requirement_ids
            else:
                projected_requirement_ids = tuple(sorted(projected_requirements))

            projected = version_candidate_ref(
                candidate,
                slot_ids=projected_slot_ids,
                requirement_ids=projected_requirement_ids,
                source_candidate_ids=(candidate.candidate_id,),
            )
            projected_by_poi.setdefault(candidate.poi_id, []).append((projected, candidate, place))

        projected: list[CandidateRef] = []
        for poi_id, entries in sorted(projected_by_poi.items()):
            entries.sort(key=lambda item: (item[0].candidate_id, item[0].place_fact_id))

            def place_score(entry):
                place = entry[2]
                fields = sum(value not in (None, "", ()) for value in (
                    place.name, place.location, place.address, place.city, place.adcode,
                    place.type_name, place.typecode, place.business_area,
                    place.cost.raw, place.cost.value, place.opening_hours_raw, place.photos,
                ))
                return (
                    place.location is not None,
                    bool(place.typecode or place.type_name),
                    fields,
                    len(set(place.field_sources.values())),
                    len(set(place.source_refs)),
                    max((source_by_id[source_id].queried_at.timestamp()
                         for source_id in place.source_refs if source_id in source_by_id), default=0),
                )

            selected, _, selected_place = max(entries, key=place_score)
            source_candidates = [item[1] for item in entries]
            merged_slots_set = {slot for item, _, _ in entries for slot in item.slot_ids}
            merged_requirements_set = {
                req for item, _, _ in entries for req in item.requirement_ids if req in allowed_requirements
            }
            if subtask.role == "dining" and dining_eligibility is not None:
                # Recheck the union against the chosen latest complete fact. A prior
                # version for this POI must not lend an invalid area binding to it.
                merged_slots_set &= dining_eligibility(
                    selected_place,
                    city=subtask.scope.city,
                    fact_store=state.fact_store,
                    area_hints=getattr(state, "area_hints", state.context.area_hints),
                    meal_areas=meal_bindings,
                    allowed_slot_ids=allowed_slots,
                    excluded_poi_ids=excluded,
                    blocked_requirements=blocked_requirements,
                )
                merged_requirements_set &= named_eligibility(
                    selected_place,
                    must_eat_restaurants=named_restaurants,
                    allowed_requirement_ids=allowed_requirements,
                )
                if not merged_slots_set and not merged_requirements_set:
                    continue
            merged_slots = tuple(sorted(merged_slots_set))
            merged_requirements = tuple(sorted(merged_requirements_set))
            if (selected.slot_ids and set(selected.slot_ids) == set(merged_slots)
                    and len(selected.slot_ids) == len(merged_slots)):
                merged_slots = selected.slot_ids
            if (selected.requirement_ids and set(selected.requirement_ids) == set(merged_requirements)
                    and len(selected.requirement_ids) == len(merged_requirements)):
                merged_requirements = selected.requirement_ids
            candidate = version_candidate_ref(
                selected,
                source_candidate_ids=(item.candidate_id for item in source_candidates),
                slot_ids=merged_slots,
                requirement_ids=merged_requirements,
            )
            projected.append(candidate)
        return tuple(projected)

    @staticmethod
    def _allowed_slots(planning_input: PlanningInput, subtask: Subtask) -> set[str]:
        target_periods = _ROLE_PERIODS[subtask.role]
        scope_days = set(subtask.scope.day_ids)
        explicit_slots = set(subtask.scope.slot_ids)
        return {
            slot.slot_id
            for day in planning_input.days
            if not scope_days or day.day_id in scope_days
            for slot in day.slots
            if slot.eligibility != "skipped"
            and slot.period in target_periods
            and (not explicit_slots or slot.slot_id in explicit_slots)
        }

    async def _run_weather_job(self, state: _MutableState, job: _Job):
        service = (self.weather_service_factory or WeatherQueryService)()
        context = WeatherQueryContext.from_planning_input(
            state.context.planning_input,
            run_id=state.context.run_id,
            context_revision=state.context.context_revision,
            base_revision=state.context.base_revision,
        )
        execution = await service.run_async(context)
        return self._project_weather_execution(execution, job.dates)

    @staticmethod
    def _project_weather_execution(execution: WeatherQueryExecution, dates: tuple[date, ...]):
        """Keep only dates explicitly requested by the current master decision."""
        requested = set(dates)
        coverage = tuple(item for item in execution.coverage if item.forecast_date in requested)
        projected_facts = []
        for fact in execution.fact_store.weather:
            forecasts = tuple(item for item in fact.forecasts if item.forecast_date in requested)
            missing_dates = tuple(item for item in fact.missing_dates if item in requested)
            if forecasts or missing_dates:
                projected_facts.append(fact.model_copy(update={
                    "forecasts": forecasts,
                    "missing_dates": missing_dates,
                }))
        source_ids = {fact.source.tool_call_id for fact in projected_facts}
        projected_store = FactStore(
            sources=tuple(item for item in execution.fact_store.sources if item.tool_call_id in source_ids),
            weather=tuple(projected_facts),
        )
        return WeatherQueryExecution.model_validate({
            **execution.model_dump(),
            "travel_dates": tuple(dates),
            "coverage": coverage,
            "fact_store": projected_store,
        })

    async def _run_endpoint_job(self, state: _MutableState, job: _Job):
        service = (self.endpoint_service_factory or EndpointVerificationService)()
        request = state.context.planning_input.request
        request_snapshot = request.model_copy(update={
            "arrival_place_query": request.arrival_place_query if "arrival" in job.endpoints else None,
            "departure_place_query": request.departure_place_query if "departure" in job.endpoints else None,
        })
        input_snapshot = state.context.planning_input.model_copy(update={"request": request_snapshot})
        context = EndpointVerificationContext.from_planning_input(
            input_snapshot,
            run_id=state.context.run_id,
            context_revision=state.context.context_revision,
            base_revision=state.context.base_revision,
        )
        return await service.run_async(context)

    def _accept_task_execution(self, state: _MutableState, job: _Job, execution: Any) -> None:
        task = job.subtask
        if getattr(execution, "request_id", None) != state.context.request_id:
            raise _DispatchFault("TASK_EXECUTION_REQUEST_MISMATCH", "task_result", "子任务执行结果请求身份不匹配。")
        result = getattr(execution, "task_result", None)
        if not isinstance(result, TaskResult):
            raise _DispatchFault("TASK_RESULT_INVALID", "task_result", "子任务未返回有效 TaskResult。")
        if (result.task_id != task.task_id or result.run_id != state.context.run_id
                or result.context_revision != state.context.context_revision
                or result.base_revision != state.context.base_revision
                or result.request_id != state.context.request_id):
            raise _DispatchFault("TASK_RESULT_IDENTITY_MISMATCH", "task_result", "子任务结果身份或版本不匹配。")
        fact_store = getattr(execution, "fact_store", None)
        if not isinstance(fact_store, FactStore):
            raise _DispatchFault("TASK_FACT_STORE_INVALID", "task_result", "子任务事实集合结构无效。")
        self._validate_fact_closure(fact_store)
        self._validate_task_candidates(state, task, result, fact_store)
        try:
            merged_facts = self._merge_fact_stores(state.fact_store, fact_store)
            merged_candidates = self._merge_candidates(state.candidate_refs, result.candidates)
        except _DispatchFault:
            raise
        state.fact_store = merged_facts
        state.candidate_refs = list(merged_candidates)
        state.task_results.append(result)
        state.result_by_task_id[result.task_id] = result
        state.task_states[task.task_id] = TaskDispatchState(
            task_id=task.task_id, role=task.role, status=result.status,
            reason=result.stop_reason,
        )
        if result.status != "complete":
            self._append_missing(state, self._missing_from_task(task, result))

    def _validate_task_candidates(
        self, state: _MutableState, task: Subtask, result: TaskResult, child_facts: FactStore,
    ) -> None:
        all_places = {item.fact_id: item for item in (*state.fact_store.places, *child_facts.places)}
        sources = {item.tool_call_id for item in (*state.fact_store.sources, *child_facts.sources)}
        expected_category = _ROLE_CATEGORY[task.role]
        allowed_slots = self._allowed_slots(state.context.planning_input, task)
        if task.role == "lodging":
            allowed_slots = set()
        for candidate in result.candidates:
            fact = all_places.get(candidate.place_fact_id)
            if (candidate.category != expected_category or fact is None or fact.poi_id != candidate.poi_id
                    or fact.location is None or not _city_matches(state.context.planning_input.request.city, fact.city)
                    or not fact.source_refs or not set(fact.source_refs).issubset(sources)
                    or not set(fact.field_sources.values()).issubset(sources)):
                raise _DispatchFault("TASK_CANDIDATE_SOURCE_INVALID", "task_result", "TaskResult 候选事实或来源未闭合。")
            if not set(candidate.slot_ids).issubset(allowed_slots):
                raise _DispatchFault("TASK_CANDIDATE_SCOPE_INVALID", "task_result", "TaskResult 候选超出任务时段范围。")
            if not set(candidate.requirement_ids).issubset(set(task.requirement_ids)):
                raise _DispatchFault("TASK_CANDIDATE_REQUIREMENT_INVALID", "task_result", "TaskResult 候选关联了任务范围外的需求。")
            if candidate.area_hint_id is not None:
                hint = next((item for item in state.area_hints if item.area_hint_id == candidate.area_hint_id), None)
                if task.role != "lodging" or hint is None or candidate.area_hint_id not in task.scope.area_hint_ids:
                    raise _DispatchFault("TASK_CANDIDATE_AREA_INVALID", "task_result", "住宿候选区域关联不在任务范围内。")
            if task.role == "lodging":
                specified_query = state.context.planning_input.request.lodging_query
                if specified_query and specified_query.strip() and candidate.area_hint_id is not None:
                    raise _DispatchFault("TASK_CANDIDATE_AREA_INVALID", "task_result", "指定住处不能关联推荐区域。")
                if not specified_query or not specified_query.strip():
                    if candidate.area_hint_id is None:
                        raise _DispatchFault("TASK_CANDIDATE_AREA_INVALID", "task_result", "推荐住宿候选必须保留区域关联。")

    def _accept_weather_execution(self, state: _MutableState, job: _Job, execution: Any) -> None:
        self._validate_direct_identity(state.context, execution, "weather")
        if (tuple(execution.travel_dates) != tuple(job.dates)
                or not _city_matches(state.context.planning_input.request.city, execution.city)):
            raise _DispatchFault(
                "WEATHER_RESULT_SCOPE_MISMATCH", "weather_query",
                "天气查询结果日期或城市与本次派发范围不匹配。",
            )
        fact_store = getattr(execution, "fact_store", None)
        if not isinstance(fact_store, FactStore):
            raise _DispatchFault("WEATHER_FACT_STORE_INVALID", "weather_query", "天气事实集合结构无效。")
        self._validate_fact_closure(fact_store)
        state.fact_store = self._merge_fact_stores(state.fact_store, fact_store)
        state.weather_results.append(execution)
        state.direct_query_summaries.append({
            "kind": "weather",
            "status": execution.tool_status,
            "dates": execution.travel_dates,
            "error_category": execution.tool_error_category,
            "weather_coverage": tuple({
                "forecast_date": item.forecast_date,
                "status": item.status,
                "reason": item.reason,
            } for item in execution.coverage),
        })
        for coverage in execution.coverage:
            if coverage.status == "unknown":
                self._append_missing(state, (f"{coverage.forecast_date.isoformat()}天气未知：{coverage.reason}",))
            elif coverage.status == "partial":
                self._append_missing(state, (f"{coverage.forecast_date.isoformat()}天气仅有部分字段可用。",))

    def _accept_endpoint_execution(self, state: _MutableState, job: _Job, execution: Any) -> None:
        self._validate_direct_identity(state.context, execution, "endpoint")
        request = state.context.planning_input.request
        requested = set(job.endpoints)
        expected_queries = {
            "arrival": request.arrival_place_query if "arrival" in requested else None,
            "departure": request.departure_place_query if "departure" in requested else None,
        }
        if any(getattr(getattr(execution, kind), "user_query", None) != expected
               for kind, expected in expected_queries.items()):
            raise _DispatchFault(
                "ENDPOINT_RESULT_SCOPE_MISMATCH", "endpoint_verify",
                "端点核实结果与本次派发范围不匹配。",
            )
        fact_store = getattr(execution, "fact_store", None)
        if not isinstance(fact_store, FactStore):
            raise _DispatchFault("ENDPOINT_FACT_STORE_INVALID", "endpoint_verify", "端点事实集合结构无效。")
        self._validate_fact_closure(fact_store)
        state.fact_store = self._merge_fact_stores(state.fact_store, fact_store)
        state.endpoint_results.append(execution)
        for kind in sorted(job.endpoints):
            item = getattr(execution, kind)
            state.direct_query_summaries.append({"kind": kind, "status": item.status})
            if item.status not in {"missing", "verified"}:
                self._append_missing(state, (f"用户填写的{kind}地点未能唯一核实：{item.reason}",))

    @staticmethod
    def _validate_direct_identity(context: PlanningDispatchContext, execution: Any, label: str) -> None:
        if (getattr(execution, "request_id", None) != context.request_id
                or getattr(execution, "run_id", None) != context.run_id
                or getattr(execution, "context_revision", None) != context.context_revision
                or getattr(execution, "base_revision", None) != context.base_revision):
            raise _DispatchFault(
                f"{label.upper()}_RESULT_IDENTITY_MISMATCH", f"{label}_query",
                f"{label}查询结果身份或版本不匹配。",
            )

    def _validate_initial_snapshot(self, state: _MutableState) -> None:
        self._validate_fact_closure(state.fact_store)
        tasks_by_id: dict[str, Subtask] = {}
        for task in state.existing_tasks:
            if task.task_id in tasks_by_id:
                raise _DispatchFault("INITIAL_TASK_DUPLICATE", "initial_snapshot", "初始任务标识重复。")
            tasks_by_id[task.task_id] = task
        results_by_id: dict[str, TaskResult] = {}
        for result in state.task_results:
            task = tasks_by_id.get(result.task_id)
            if task is None or result.task_id in results_by_id:
                raise _DispatchFault("INITIAL_RESULT_TASK_INVALID", "initial_snapshot", "初始任务结果未唯一关联已知任务。")
            results_by_id[result.task_id] = result
            self._validate_task_candidates(state, task, result, state.fact_store)
        state.candidate_refs = list(self._merge_candidates(
            state.candidate_refs,
            tuple(candidate for result in state.task_results for candidate in result.candidates),
        ))
        self._validate_candidate_closure(state.candidate_refs, state.fact_store)
        known_area_ids = {item.area_hint_id for item in state.area_hints}
        if any(item.area_hint_id is not None and item.area_hint_id not in known_area_ids
               for item in state.candidate_refs):
            raise _DispatchFault("CANDIDATE_AREA_INVALID", "initial_snapshot", "初始住宿候选区域不存在。")
        area_ids: set[str] = set()
        for hint in state.area_hints:
            if hint.area_hint_id in area_ids:
                raise _DispatchFault("AREA_HINT_ID_DUPLICATE", "initial_snapshot", "初始区域标识重复。")
            area_ids.add(hint.area_hint_id)
            self._validate_area_hint(hint, state.fact_store, state.context.planning_input.request.city)

    @staticmethod
    def _validate_candidate_closure(candidates, facts: FactStore) -> None:
        places = {item.fact_id: item for item in facts.places}
        sources = {item.tool_call_id for item in facts.sources}
        for candidate in candidates:
            place = places.get(candidate.place_fact_id)
            if (place is None or place.poi_id != candidate.poi_id
                    or not place.source_refs or not set(place.source_refs).issubset(sources)
                    or not set(place.field_sources.values()).issubset(sources)):
                raise _DispatchFault(
                    "CANDIDATE_SOURCE_INVALID", "candidate_validation", "候选地点事实或来源未闭合。",
                )

    @staticmethod
    def _validate_fact_closure(facts: FactStore) -> None:
        sources: dict[str, FactSource] = {}
        for source in facts.sources:
            existing = sources.get(source.tool_call_id)
            if existing is not None and existing != source:
                raise _DispatchFault("FACT_SOURCE_ID_CONFLICT", "fact_merge", "同一来源标识包含冲突内容。")
            sources[source.tool_call_id] = source
        collections = (
            (facts.places, "fact_id", "地点"),
            (facts.weather, "fact_id", "天气"),
            (facts.routes, "fact_id", "路线"),
        )
        global_fact_ids: dict[str, Any] = {}
        for rows, _, label in collections:
            for row in rows:
                prior = global_fact_ids.get(row.fact_id)
                if prior is not None and prior != row:
                    raise _DispatchFault("FACT_ID_CONFLICT", "fact_merge", f"同一{label}事实标识与其他事实内容冲突。")
                global_fact_ids[row.fact_id] = row
        for rows, id_field, label in collections:
            seen: dict[str, Any] = {}
            for row in rows:
                row_id = getattr(row, id_field)
                existing = seen.get(row_id)
                if existing is not None and existing != row:
                    raise _DispatchFault("FACT_ID_CONFLICT", "fact_merge", f"同一{label}事实标识包含冲突内容。")
                seen[row_id] = row
        for fact in facts.places:
            if (not fact.source_refs or not set(fact.source_refs).issubset(sources)
                    or not set(fact.field_sources.values()).issubset(sources)):
                raise _DispatchFault("PLACE_FACT_SOURCE_INVALID", "fact_merge", "地点事实来源引用未闭合。")
        for fact in facts.weather:
            if fact.source.tool_call_id not in sources or sources[fact.source.tool_call_id] != fact.source:
                raise _DispatchFault("WEATHER_FACT_SOURCE_INVALID", "fact_merge", "天气事实来源引用未闭合。")
        for fact in facts.routes:
            if fact.source.tool_call_id not in sources or sources[fact.source.tool_call_id] != fact.source:
                raise _DispatchFault("ROUTE_FACT_SOURCE_INVALID", "fact_merge", "路线事实来源引用未闭合。")

    @classmethod
    def _merge_fact_stores(cls, left: FactStore, right: FactStore) -> FactStore:
        cls._validate_fact_closure(left)
        cls._validate_fact_closure(right)
        sources = cls._merge_id_rows(left.sources, right.sources, "tool_call_id", "FACT_SOURCE_ID_CONFLICT", "事实来源")
        places = cls._merge_id_rows(left.places, right.places, "fact_id", "FACT_ID_CONFLICT", "地点事实")
        weather = cls._merge_id_rows(left.weather, right.weather, "fact_id", "FACT_ID_CONFLICT", "天气事实")
        routes = cls._merge_id_rows(left.routes, right.routes, "fact_id", "FACT_ID_CONFLICT", "路线事实")
        all_fact_ids: dict[str, Any] = {}
        for label, rows in (("地点", places), ("天气", weather), ("路线", routes)):
            for row in rows:
                previous = all_fact_ids.get(row.fact_id)
                if previous is not None and previous != row:
                    raise _DispatchFault("FACT_ID_CONFLICT", "fact_merge", f"同一事实标识不能跨类型冲突：{label}。")
                all_fact_ids[row.fact_id] = row
        merged = FactStore(sources=tuple(sources), places=tuple(places), weather=tuple(weather), routes=tuple(routes))
        cls._validate_fact_closure(merged)
        return merged

    @staticmethod
    def _merge_id_rows(left, right, id_field: str, code: str, label: str):
        result: list[Any] = []
        by_id: dict[str, Any] = {}
        for row in (*left, *right):
            row_id = getattr(row, id_field)
            prior = by_id.get(row_id)
            if prior is None:
                by_id[row_id] = row
                result.append(row)
            elif prior != row:
                raise _DispatchFault(code, "fact_merge", f"相同{label}标识的内容冲突，拒绝合并。")
        return result

    @staticmethod
    def _merge_candidates(left, right):
        result: list[CandidateRef] = []
        by_id: dict[str, CandidateRef] = {}
        for candidate in (*left, *right):
            prior = by_id.get(candidate.candidate_id)
            if prior is None:
                by_id[candidate.candidate_id] = candidate
                result.append(candidate)
            elif prior != candidate:
                raise _DispatchFault(
                    "CANDIDATE_ID_CONFLICT", "candidate_merge",
                    "相同 candidate_id 的内容冲突，拒绝合并。",
                )
        return tuple(result)

    def _merge_areas(self, state: _MutableState, areas: tuple[AreaHint, ...]) -> None:
        merged = list(state.area_hints)
        by_id = {item.area_hint_id: item for item in merged}
        for hint in areas:
            prior = by_id.get(hint.area_hint_id)
            if prior is not None:
                if prior != hint:
                    raise _DispatchFault("AREA_HINT_ID_CONFLICT", "area_merge", "相同区域标识的内容冲突，拒绝合并。")
                continue
            self._validate_area_hint(hint, state.fact_store, state.context.planning_input.request.city)
            by_id[hint.area_hint_id] = hint
            merged.append(hint)
        state.area_hints = merged

    @staticmethod
    def _validate_area_hint(hint: AreaHint, facts: FactStore, requested_city: str) -> None:
        if not hint.basis_fact_ids:
            raise _DispatchFault("AREA_HINT_SOURCE_INVALID", "area_merge", "区域必须引用已核实地点事实。")
        by_fact = {item.fact_id: item for item in facts.places}
        cited = [by_fact.get(item) for item in hint.basis_fact_ids]
        if any(item is None or item.location is None or not item.city
               or not _city_matches(requested_city, item.city) for item in cited):
            raise _DispatchFault("AREA_HINT_SOURCE_INVALID", "area_merge", "区域依据必须是有坐标的地点事实。")
        if len(hint.basis_fact_ids) != len(set(hint.basis_fact_ids)):
            raise _DispatchFault("AREA_HINT_SOURCE_INVALID", "area_merge", "区域依据事实不能重复。")
        if not hint.anchor_poi_ids or len(hint.anchor_poi_ids) != len(set(hint.anchor_poi_ids)):
            raise _DispatchFault("AREA_HINT_ANCHOR_INVALID", "area_merge", "区域必须有唯一的地点锚点。")
        cited_pois = {item.poi_id for item in cited}
        if not set(hint.anchor_poi_ids).issubset(cited_pois):
            raise _DispatchFault("AREA_HINT_ANCHOR_INVALID", "area_merge", "区域锚点必须由所列地点事实证明。")
        if hint.center is None:
            raise _DispatchFault("AREA_HINT_CENTER_INVALID", "area_merge", "区域中心必须由地点锚点计算。")
        points: dict[str, Any] = {}
        for fact in cited:
            if fact.poi_id in hint.anchor_poi_ids:
                points.setdefault(fact.poi_id, fact.location)
        if set(points) != set(hint.anchor_poi_ids):
            raise _DispatchFault("AREA_HINT_ANCHOR_INVALID", "area_merge", "区域锚点缺少可用坐标。")
        expected_lng = sum(point.lng for point in points.values()) / len(points)
        expected_lat = sum(point.lat for point in points.values()) / len(points)
        if abs(hint.center.lng - expected_lng) > 1e-8 or abs(hint.center.lat - expected_lat) > 1e-8:
            raise _DispatchFault("AREA_HINT_CENTER_INVALID", "area_merge", "区域中心与来源化锚点不一致。")
        if hint.basis != "verified_place_facts":
            raise _DispatchFault("AREA_HINT_BASIS_INVALID", "area_merge", "区域来源必须是已核实地点事实。")

    def _dependency_disposition(self, state: _MutableState, task: Subtask, pending: Mapping[str, Any]):
        fact_ids = self._all_fact_ids(state.fact_store)
        area_by_id = {item.area_hint_id: item for item in state.area_hints}
        required_facts = set(task.known_fact_ids) | {
            item.reference_id for item in task.dependencies if item.kind == "fact"
        }
        waiting = False
        blocked_reason = None
        for dependency in task.dependencies:
            reference = dependency.reference_id
            if dependency.kind == "fact":
                if reference not in fact_ids:
                    waiting = True
            elif dependency.kind == "area_hint":
                hint = area_by_id.get(reference)
                if hint is None:
                    waiting = True
                elif (not set(hint.basis_fact_ids).issubset(fact_ids)
                      or not set(hint.anchor_poi_ids).issubset({item.poi_id for item in state.fact_store.places})):
                    waiting = True
            else:
                task_id = self._resolve_alias(state, reference)
                if task_id in pending:
                    waiting = True
                    continue
                prior = state.result_by_task_id.get(task_id)
                if prior is None:
                    waiting = True
                elif prior.status == "failed":
                    blocked_reason = f"前置任务 {task_id} 已失败"
                elif prior.status == "partial":
                    if not self._partial_task_has_required_facts(prior, state, required_facts):
                        blocked_reason = f"前置任务 {task_id} 只有部分结果，缺少可用候选事实"
        if blocked_reason:
            return "blocked", blocked_reason
        if waiting or not required_facts.issubset(fact_ids):
            return "waiting", "事实或前置任务依赖尚未就绪"
        for hint_id in task.scope.area_hint_ids:
            hint = area_by_id.get(hint_id)
            if hint is None or not set(hint.basis_fact_ids).issubset(fact_ids):
                return "waiting", "任务范围引用的住宿或用餐区域尚未就绪"
        return "ready", ""

    @staticmethod
    def _partial_task_has_required_facts(result: TaskResult, state: _MutableState, required_facts: set[str]) -> bool:
        fact_ids = PlanningDispatchService._all_fact_ids(state.fact_store)
        if required_facts:
            return required_facts.issubset(fact_ids)
        place_ids = {item.fact_id for item in state.fact_store.places}
        return any(candidate.place_fact_id in place_ids for candidate in result.candidates)

    def _ready_task_ids(self, state: _MutableState, task: Subtask) -> frozenset[str]:
        ready: set[str] = set()
        required_facts = set(task.known_fact_ids) | {
            item.reference_id for item in task.dependencies if item.kind == "fact"
        }
        for task_id, result in state.result_by_task_id.items():
            if result.status == "complete":
                ready.add(task_id)
            elif result.status == "partial" and self._partial_task_has_required_facts(result, state, required_facts):
                ready.add(task_id)
        for dependency in task.dependencies:
            if dependency.kind != "task":
                continue
            task_id = self._resolve_alias(state, dependency.reference_id)
            result = state.result_by_task_id.get(task_id)
            if result and (result.status == "complete" or (
                result.status == "partial" and self._partial_task_has_required_facts(result, state, required_facts)
            )):
                ready.add(task_id)
        return frozenset(ready)

    @staticmethod
    def _all_fact_ids(facts: FactStore) -> set[str]:
        return ({item.fact_id for item in facts.places}
                | {item.fact_id for item in facts.weather}
                | {item.fact_id for item in facts.routes})

    @staticmethod
    def _resolve_alias(state: _MutableState, task_id: str) -> str:
        seen: set[str] = set()
        current = task_id
        while current in state.aliases and current not in seen:
            seen.add(current)
            current = state.aliases[current]
        return current

    def _task_fingerprint(self, task: Subtask, state: _MutableState, resolved: Any | None = None) -> str:
        areas = {item.area_hint_id: item for item in state.area_hints}
        area_semantics = []
        for area_id in task.scope.area_hint_ids:
            hint = areas.get(area_id)
            area_semantics.append((
                _normalize(hint.label) if hint else "missing-area",
                tuple(sorted(hint.anchor_poi_ids)) if hint else (),
                tuple(sorted(hint.basis_fact_ids)) if hint else (),
            ))
        dependencies = []
        for item in task.dependencies:
            if item.kind == "area_hint":
                hint = areas.get(item.reference_id)
                dependencies.append((item.kind, tuple(sorted(hint.anchor_poi_ids)) if hint else ()))
            elif item.kind == "task":
                target_id = self._resolve_alias(state, item.reference_id)
                target = state.task_by_id.get(target_id)
                dependencies.append((item.kind, self._task_core_signature(target, state) if target else "pending"))
            else:
                dependencies.append((item.kind, item.reference_id))
        meal_bindings = []
        if resolved is not None:
            meal_bindings = sorted((
                item.slot_id,
                tuple(sorted(areas[item.area_hint_id].anchor_poi_ids)) if item.area_hint_id in areas else (),
                tuple(sorted(areas[item.area_hint_id].basis_fact_ids)) if item.area_hint_id in areas else (),
                item.anchor_poi_id,
            ) for item in resolved.meal_areas)
        payload = self._task_core_signature(task, state)
        payload["areas"] = sorted(area_semantics)
        payload["dependencies"] = sorted(dependencies, key=repr)
        payload["meal_bindings"] = meal_bindings
        payload["restaurant_names"] = sorted((item.requirement_id, _normalize(item.restaurant_name)) for item in getattr(resolved, "must_eat_restaurants", ()))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _task_core_signature(task: Subtask | None, state: _MutableState):
        if task is None:
            return "missing-task"
        return {
            "role": task.role,
            "objective": _normalize(task.objective),
            "city": _normalize(task.scope.city),
            "days": sorted(task.scope.day_ids),
            "slots": sorted(task.scope.slot_ids),
            "requirements": sorted(task.requirement_ids),
            "excluded_pois": sorted(task.excluded_poi_ids),
            "known_facts": sorted(task.known_fact_ids),
            "candidate_target": task.candidate_target,
            "required_fields": sorted(task.required_fields),
            "constraints": sorted(_normalize(value) for value in task.constraints),
            "preferences": sorted(_normalize(value) for value in task.preferences),
        }

    def _record_local_failure(self, state: _MutableState, job: _Job, exc: BaseException) -> None:
        if job.kind == "task":
            task = job.subtask
            status = (
                "deadline" if isinstance(exc, asyncio.TimeoutError)
                else "tool_error" if isinstance(exc, PlanningError)
                else "model_error"
            )
            result = TaskResult(
                task_id=task.task_id,
                run_id=task.run_id,
                context_revision=task.context_revision,
                request_id=state.context.request_id,
                base_revision=task.base_revision,
                status="failed",
                stop_reason=status,
            )
            state.task_results.append(result)
            state.result_by_task_id[result.task_id] = result
            state.task_states[task.task_id] = TaskDispatchState(
                task_id=task.task_id, role=task.role, status="failed", reason=status,
            )
            self._append_missing(state, (f"{task.role}子任务执行失败，保留其他有效查询结果。",))
            code = exc.code if isinstance(exc, PlanningError) else status.upper()
            state.issues.append(DispatchIssue(scope="task", reference_id=task.task_id, code=code, message="子任务局部执行失败。"))
        elif job.kind == "weather":
            timeout_category = "timeout" if isinstance(exc, asyncio.TimeoutError) else None
            state.direct_query_summaries.append({
                "kind": "weather",
                "status": "error",
                "dates": job.dates,
                "error_category": timeout_category,
                "weather_coverage": tuple({
                    "forecast_date": day,
                    "status": "unknown",
                    "reason": "query_error",
                } for day in job.dates),
            })
            self._append_missing(state, ("天气查询未能完成，天气保持未知。",))
            state.issues.append(DispatchIssue(scope="weather", reference_id=job.reference_id, code="WEATHER_QUERY_FAILED", message="天气查询未能完成。"))
        else:
            for kind in job.endpoints:
                state.direct_query_summaries.append({"kind": kind, "status": "error"})
            self._append_missing(state, ("用户填写的到离地点未能完成核实。",))
            state.issues.append(DispatchIssue(scope="endpoint", reference_id=job.reference_id, code="ENDPOINT_QUERY_FAILED", message="到离地点核实未能完成。"))

    @staticmethod
    def _record_fault(state: _MutableState, job: _Job, fault: _DispatchFault) -> None:
        state.issues.append(DispatchIssue(
            scope="scheduler", reference_id=job.reference_id, code=fault.code, message=fault.message,
        ))

    def _missing_from_task(self, task: Subtask, result: TaskResult) -> tuple[str, ...]:
        rows = []
        for item in result.missing:
            text = item.reason or item.status
            rows.append(f"{task.role}任务缺少 {item.target_id}：{text}")
        if not rows:
            rows.append(f"{task.role}任务未完整满足目标：{result.stop_reason}")
        return tuple(rows)

    @staticmethod
    def _append_missing(state: _MutableState, values) -> None:
        for value in values:
            text = str(value).strip()
            if text and text not in state.missing_information:
                state.missing_information.append(text)

    def _deadline_result(self, state: _MutableState, action: str | None) -> PlanningDispatchResult:
        self._append_missing(state, (f"收集达到{self.total_timeout_seconds:g}秒技术期限，未完成的信息保留为未知。",))
        return self._result(
            state,
            action=action if action in {"delegate", "plan"} else None,
            stop_reason="deadline",
            reason="达到收集技术期限，返回已完成的有效结果和缺项。",
        )

    @staticmethod
    def _result(
        state: _MutableState,
        *,
        action: str | None,
        stop_reason: str,
        reason: str,
        error: DispatchError | None = None,
    ) -> PlanningDispatchResult:
        context = state.context
        return PlanningDispatchResult(
            request_id=context.request_id,
            run_id=context.run_id,
            context_revision=context.context_revision,
            base_revision=context.base_revision,
            action=action,
            stop_reason=stop_reason,
            reason=reason,
            missing_information=tuple(state.missing_information),
            fact_store=state.fact_store,
            candidate_refs=tuple(state.candidate_refs),
            area_hints=tuple(state.area_hints),
            existing_tasks=tuple(state.existing_tasks),
            task_results=tuple(state.task_results),
            weather_results=tuple(state.weather_results),
            endpoint_results=tuple(state.endpoint_results),
            weather_attempted_dates=tuple(sorted(state.weather_attempted_dates)),
            endpoint_attempted=tuple(sorted(state.endpoint_attempted)),
            task_states=tuple(state.task_states.values()),
            issues=tuple(state.issues),
            error=error,
        )


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _city_matches(expected: str, actual: str | None) -> bool:
    def normalize(value: str | None) -> str:
        text = _normalize(value or "")
        for suffix in ("特别行政区", "自治州", "地区", "市"):
            if text.endswith(suffix):
                return text[:-len(suffix)]
        return text

    return bool(normalize(actual)) and normalize(expected) == normalize(actual)
