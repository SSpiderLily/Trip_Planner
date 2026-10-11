"""从公开 TripRequest 生成同一版本的 planning_v4 预览快照。"""
from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass
from datetime import date
from threading import Event
from typing import Any, Callable
from uuid import uuid4

from app.agents.daily_plan_generator import DailyDayRevisionExecution
from app.agents.execution import PlanningError
from app.models.planning_v4 import (
    AreaHint,
    DispatchActivitySummary,
    DispatchAnchorSummary,
    DispatchScope,
    DispatchTarget,
    DayRevision,
    FactStore,
    PlanDraft,
    PlanningInput,
    RequirementSummary,
    TripRequest,
    ValidationIssue,
)
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.day_repair_service import (
    DayRepairCollectionRequest,
    DayRepairCollectionResult,
    DayRepairError,
    DayRepairRecalculation,
    DayRepairRecalculationRequest,
    DayRepairService,
    DayRepairResult,
    DayRevisionRequest,
)
from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter
from app.services.endpoint_verification_service import EndpointVerificationExecution
from app.services.observation_service import span
from app.services.planning_dispatch_service import (
    PlanningDispatchContext,
    PlanningDispatchResult,
    PlanningDispatchService,
)
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.planning_v4_timing import PlanTimingResult, calculate_plan_timing
from app.services.planning_v4_validation import validate_daily_plan
from app.services.planning_v4_validator import validate_preview
from app.services.preview_task_service import (
    PreviewPipelineResult,
    PreviewProgress,
    make_route_backfill_runner,
)
from app.services.current_route_service import (
    CurrentRouteError,
    CurrentRouteService,
    _build_edges,
    build_pending_route_legs_for_day,
)
from app.services.reference_cost import aggregate_reference_costs
from app.services.route_backfill_service import RouteBackfillService


DEFAULT_PIPELINE_TIMEOUT_SECONDS = 1800.0
DEFAULT_INITIAL_COLLECTION_TIMEOUT_SECONDS = 600.0
MODEL_TIMEOUT_SECONDS = 120.0
GLOBAL_STAGE_GRACE_SECONDS = 2.0


class PlanningV4PipelineError(RuntimeError):
    """对外仅暴露稳定错误代码，不包含底层异常文本。"""

    def __init__(self, code: str, stage: str, *, global_deadline: bool = False):
        self.code = code
        self.stage = stage
        self.global_deadline = global_deadline
        super().__init__(code)


@dataclass
class _RevisionHolder:
    value: int


class PlanningV4Pipeline:
    """串接 F01/F02/F05/F06/F08/F07/F09/F09a/成本，并构造单一快照。"""

    def __init__(
        self,
        *,
        total_timeout_seconds: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
        initial_collection_timeout_seconds: float = DEFAULT_INITIAL_COLLECTION_TIMEOUT_SECONDS,
        rules_factory: Callable[[], Any] = default_planning_v4_rules,
        requirement_interpreter_factory: Callable[[float], Any] | None = None,
        dispatch_service_factory: Callable[[float], Any] | None = None,
        daily_plan_adapter_factory: Callable[[float], Any] | None = None,
        current_route_service_factory: Callable[[], Any] | None = None,
        route_backfill_service_factory: Callable[[], Any] | None = None,
        day_repair_service_factory: Callable[..., Any] | None = None,
        current_revision_lookup: Callable[[str], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        for name, value in (
            ("total_timeout_seconds", total_timeout_seconds),
            ("initial_collection_timeout_seconds", initial_collection_timeout_seconds),
        ):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} 必须是有限正数")
        self.total_timeout_seconds = float(total_timeout_seconds)
        self.initial_collection_timeout_seconds = float(initial_collection_timeout_seconds)
        self.rules_factory = rules_factory
        self.requirement_interpreter_factory = requirement_interpreter_factory or self._requirement_factory
        self.dispatch_service_factory = dispatch_service_factory or self._dispatch_factory
        self.daily_plan_adapter_factory = daily_plan_adapter_factory or self._daily_plan_factory
        self.current_route_service_factory = current_route_service_factory or CurrentRouteService
        self.route_backfill_service_factory = route_backfill_service_factory or RouteBackfillService
        self.day_repair_service_factory = day_repair_service_factory or DayRepairService
        self.current_revision_lookup = current_revision_lookup
        self.clock = clock

    async def run(self, request: TripRequest, *, task_id: str, plan_id: str, progress) -> PreviewPipelineResult:
        if not isinstance(request, TripRequest):
            raise PlanningV4PipelineError("TRIP_REQUEST_INVALID", "request_validation")
        if not task_id or not plan_id:
            raise PlanningV4PipelineError("PIPELINE_IDENTITY_INVALID", "pipeline_start")

        started = self.clock()
        deadline = started + self.total_timeout_seconds
        cancel_event = Event()
        route_context = None
        retain_route_context = False
        initial_dispatch_recovery_code: str | None = None
        await self._progress(progress, "planning_input", 0, 1, "整理原始需求")
        try:
            requirements = await self._interpret_requirements(request, deadline, cancel_event)
            self._check_deadline(deadline, "planning_input")
            planning_input = build_planning_input(PlanningInputBuildRequest(
                request=request,
                requirements=requirements,
                rules=self.rules_factory(),
                explicit_return_buffer_s=request.explicit_return_buffer_s,
            ))
            with span("planning.v4.input", "planning", {
                "request_id": request.request_id,
                "rule_version": planning_input.rules.rule_version,
                "day_count": len(planning_input.days),
                "explicit_return_buffer": request.explicit_return_buffer_s is not None,
            }) as record:
                record.output = {"day_count": len(planning_input.days), "built": True}

            route_service = self.current_route_service_factory()
            route_context = route_service.create_route_context(planning_input, plan_id)

            async def refresh_cancelled_route_context(facts_for_context: FactStore) -> None:
                nonlocal route_context
                if route_context is None or not route_context.cancel_event.is_set():
                    return
                previous_context = route_context
                route_context = None
                try:
                    await previous_context.aclose()
                except Exception:
                    pass
                if self._remaining(deadline) <= 0 or cancel_event.is_set():
                    return
                route_context = route_service.create_route_context(planning_input, plan_id)
                route_context.register_facts(facts_for_context)

            await self._progress(progress, "dispatch", 0, 1, "收集地点、天气与端点事实")
            initial_timeout = min(self.initial_collection_timeout_seconds, self._remaining(deadline))
            if initial_timeout <= 0:
                raise PlanningV4PipelineError("PIPELINE_DEADLINE_BEFORE_COLLECTION", "dispatch")
            dispatch_service = self.dispatch_service_factory(initial_timeout)
            dispatch_context = PlanningDispatchContext(
                planning_input=planning_input,
                run_id=task_id,
                context_revision=1,
                base_revision=None,
            )
            dispatch_result = await self._await_global_deadline(
                dispatch_service.run_async(dispatch_context),
                deadline=deadline,
                stage="dispatch",
                cancel_event=cancel_event,
            )
            self._validate_dispatch_result(dispatch_result, planning_input, task_id, 1, None)
            if dispatch_result.stop_reason == "technical_error":
                error_code = dispatch_result.error.code if dispatch_result.error else "DISPATCH_TECHNICAL_ERROR"
                if self._is_dispatch_data_corruption(error_code):
                    raise PlanningV4PipelineError(error_code, "dispatch")
                try:
                    dispatch_result = PlanningDispatchService.validate_result_snapshot(
                        dispatch_context,
                        dispatch_result,
                    )
                except Exception as exc:
                    raise PlanningV4PipelineError(
                        self._safe_code(exc, "DISPATCH_PARTIAL_SNAPSHOT_INVALID"),
                        "dispatch",
                    ) from None
                initial_dispatch_recovery_code = error_code
                with span("planning.dispatch_partial_recovery", "planning", {
                    "request_id": request.request_id,
                    "error_code": error_code,
                    "place_fact_count": len(dispatch_result.fact_store.places),
                    "weather_fact_count": len(dispatch_result.fact_store.weather),
                    "route_fact_count": len(dispatch_result.fact_store.routes),
                    "candidate_count": len(dispatch_result.candidate_refs),
                    "task_result_count": len(dispatch_result.task_results),
                }) as recovery_span:
                    recovery_span.output = {
                        "snapshot_valid": True,
                        "partial_snapshot_used": True,
                        "prior_facts_preserved": True,
                    }
                await self._progress(
                    progress,
                    "dispatch",
                    1,
                    1,
                    "信息收集未能全部完成；已核实信息已保留，继续生成带缺项的草稿",
                )

            facts = dispatch_result.fact_store
            candidates = dispatch_result.candidate_refs
            area_hints = dispatch_result.area_hints
            task_results = dispatch_result.task_results
            departure_fact_id, endpoint_execution = self._verified_departure(
                dispatch_result, task_id, request,
            )

            await self._progress(progress, "proposal", 0, 1, "生成每日安排草稿")
            proposal_timeout = min(MODEL_TIMEOUT_SECONDS, self._remaining(deadline))
            if proposal_timeout <= 0:
                raise PlanningV4PipelineError("PIPELINE_DEADLINE_BEFORE_DRAFT", "proposal")
            daily_adapter = self.daily_plan_adapter_factory(proposal_timeout)
            proposal_cancel_event = Event()
            try:
                generated = await self._await_stage(
                    daily_adapter.generate(
                        planning_input,
                        candidates,
                        facts,
                        area_hints,
                        task_results,
                        source_run_id=task_id,
                        plan_id=plan_id,
                        cancel_event=proposal_cancel_event,
                    ),
                    timeout=proposal_timeout,
                    stage="proposal",
                    cancel_event=proposal_cancel_event,
                    global_deadline=deadline,
                    global_cancel_event=cancel_event,
                    timeout_code="PLAN_GENERATION_TIMEOUT",
                )
            except PlanningV4PipelineError:
                raise
            except Exception as exc:
                raise self._stage_error(exc, "proposal", "PLAN_GENERATION_FAILED") from None
            draft = getattr(generated, "draft", None)
            if not isinstance(draft, PlanDraft):
                raise PlanningV4PipelineError("PLAN_DRAFT_MISSING", "proposal")
            if (draft.plan_id != plan_id or draft.request_id != request.request_id
                    or draft.source_run_id != task_id or draft.revision != 1
                    or draft.base_revision is not None
                    or draft.rule_version != planning_input.rules.rule_version):
                raise PlanningV4PipelineError("PLAN_DRAFT_IDENTITY_MISMATCH", "proposal")
            # F06 may enrich area hints; these are the exact hints validated with the proposal.
            area_hints = draft.area_hints

            await self._progress(progress, "current_routes", 0, 1, "核算当前路线与活动时间")
            draft, facts, timing, current_routes_status = await self._current_routes(
                route_service,
                planning_input,
                draft,
                facts,
                endpoint_execution=endpoint_execution,
                endpoint_context_revision=1,
                departure_fact_id=departure_fact_id,
                route_context=route_context,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            failed_route_days = self._failed_route_days(draft)
            if current_routes_status == "failed" and not failed_route_days:
                failed_route_days.update(day.day_id for day in planning_input.days)
            await refresh_cancelled_route_context(facts)
            if route_context is not None:
                route_context.register_facts(facts)

            await self._progress(progress, "validation", 0, 1, "检查缺项与已知约束")
            report = validate_daily_plan(planning_input, draft, facts, candidates, timing)
            await self._progress(progress, "repair", 0, 1, "按日补查并修订缺项")
            collection_revision = [1]

            async def collect(request_data: DayRepairCollectionRequest) -> DayRepairCollectionResult:
                return await self._collect_for_repair(
                    request_data,
                    task_id=task_id,
                    deadline=deadline,
                    collection_revision=collection_revision,
                    cancel_event=cancel_event,
                )

            async def revise(request_data: DayRevisionRequest):
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    cancel_event.set()
                    raise DayRepairError("PIPELINE_DEADLINE", deadline=True, global_deadline=True)
                timeout = min(MODEL_TIMEOUT_SECONDS, remaining)
                revision_cancel_event = Event()
                try:
                    execution = await self._await_stage(
                        daily_adapter.revise_day(
                            request_data.planning_input,
                            request_data.draft,
                            request_data.report,
                            request_data.day.day_id,
                            request_data.issues,
                            request_data.assignments,
                            request_data.candidates,
                            request_data.fact_store,
                            request_data.area_hints,
                            request_data.task_results,
                            preserve_activity_ids=request_data.preserve_activity_ids,
                            modifiable_activity_ids=request_data.modifiable_activity_ids,
                            removable_activity_ids=request_data.removable_activity_ids,
                            source_run_id=task_id,
                            round_id=request_data.round_id,
                            round_number=request_data.round_number,
                            timeout_seconds=timeout,
                            cancel_event=revision_cancel_event,
                        ),
                        timeout=timeout,
                        stage="repair_revision",
                        cancel_event=revision_cancel_event,
                        global_deadline=deadline,
                        global_cancel_event=cancel_event,
                        timeout_code="DAY_REVISION_TIMEOUT",
                    )
                    return self._normalize_day_revision_execution(execution)
                except PlanningV4PipelineError as exc:
                    raise DayRepairError(
                        exc.code,
                        deadline=exc.global_deadline,
                        global_deadline=exc.global_deadline,
                    ) from None
                except Exception as exc:
                    code = self._safe_code(exc, "DAY_REVISION_FAILED")
                    raise DayRepairError(code) from None

            async def recalculate(request_data: DayRepairRecalculationRequest) -> DayRepairRecalculation:
                if self._remaining(deadline) <= 0:
                    cancel_event.set()
                    failed_route_days.add(request_data.day_id)
                    return self._failed_day_route_recalculation(
                        request_data, departure_fact_id, error_category="timeout",
                    )
                route_timeout = min(MODEL_TIMEOUT_SECONDS, self._remaining(deadline))
                route_stage_cancel_event = Event()
                try:
                    await refresh_cancelled_route_context(request_data.facts)
                    if route_context is None:
                        failed_route_days.add(request_data.day_id)
                        return self._failed_day_route_recalculation(
                            request_data, departure_fact_id, error_category="provider_error",
                        )
                    routed = await self._await_stage(
                        route_service.run_async(
                            planning_input,
                            request_data.draft,
                            request_data.facts,
                            verified_departure_fact_id=departure_fact_id,
                            route_context=route_context,
                            only_day_id=request_data.day_id,
                        ),
                        timeout=route_timeout,
                        stage="repair_routes",
                        cancel_event=route_stage_cancel_event,
                        global_deadline=deadline,
                        global_cancel_event=cancel_event,
                        timeout_code="REPAIR_ROUTE_TIMEOUT",
                    )
                except PlanningV4PipelineError as exc:
                    if exc.global_deadline:
                        cancel_event.set()
                    await refresh_cancelled_route_context(request_data.facts)
                    failed_route_days.add(request_data.day_id)
                    return self._failed_day_route_recalculation(
                        request_data,
                        departure_fact_id,
                        error_category=(
                            "timeout" if exc.global_deadline or "TIMEOUT" in exc.code
                            else "provider_error"
                        ),
                    )
                except CurrentRouteError as exc:
                    raise DayRepairError(exc.code, fatal=True) from None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._remaining(deadline) <= 0:
                        cancel_event.set()
                        category = "timeout"
                    else:
                        category = "provider_error"
                    await refresh_cancelled_route_context(request_data.facts)
                    failed_route_days.add(request_data.day_id)
                    return self._failed_day_route_recalculation(
                        request_data, departure_fact_id, error_category=category,
                    )

                if not isinstance(routed.draft, PlanDraft):
                    raise DayRepairError("REPAIR_ROUTE_DRAFT_INVALID", fatal=True)
                if (routed.draft.plan_id != request_data.draft.plan_id
                        or routed.draft.revision != request_data.draft.revision
                        or routed.draft.base_revision != request_data.draft.base_revision):
                    raise DayRepairError("REPAIR_ROUTE_IDENTITY_MISMATCH", fatal=True)
                route_draft = routed.draft
                next_timing = routed.timing
                if self._has_pending_route_legs(route_draft):
                    route_context.cancel_event.set()
                    await refresh_cancelled_route_context(request_data.facts)
                    failed_route_days.add(request_data.day_id)
                    return self._failed_day_route_recalculation(
                        request_data, departure_fact_id, error_category="provider_error",
                    )
                next_report = validate_daily_plan(
                    planning_input,
                    route_draft,
                    routed.fact_store,
                    request_data.candidates,
                    next_timing,
                    repair_states=request_data.repair_states,
                )
                if route_context is not None:
                    route_context.register_facts(routed.fact_store)
                await refresh_cancelled_route_context(routed.fact_store)
                self._record_route_day_result(
                    failed_route_days, route_draft, request_data.day_id, succeeded=True,
                )
                return DayRepairRecalculation(
                    draft=route_draft,
                    facts=routed.fact_store,
                    timing=next_timing,
                    report=next_report,
                )

            repair_service = self.day_repair_service_factory(
                collect=collect,
                revise=revise,
                recalculate=recalculate,
            )
            try:
                repair = await repair_service.run_async(
                    planning_input,
                    draft,
                    facts,
                    candidates,
                    area_hints,
                    task_results,
                    timing,
                    report,
                    global_deadline_monotonic=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self._stage_error(exc, "repair", "DAY_REPAIR_FAILED") from None
            if not isinstance(repair, DayRepairResult):
                raise PlanningV4PipelineError("DAY_REPAIR_RESULT_INVALID", "repair")
            if repair.status == "technical_error":
                raise PlanningV4PipelineError(
                    repair.error_code or "DAY_REPAIR_TECHNICAL_ERROR",
                    "repair",
                )
            draft, facts = repair.draft, repair.facts
            candidates, area_hints, task_results = (
                repair.candidates, repair.area_hints, repair.task_results,
            )
            timing = repair.timing or calculate_plan_timing(
                planning_input, draft, facts,
                verified_departure_fact_id=departure_fact_id,
            )
            report = repair.report
            if self._has_pending_route_legs(draft):
                failed_route_days.update(
                    leg.day_id for leg in draft.legs
                    if any(mode.status == "pending" for mode in leg.mode_results)
                )
                draft = self._mark_pending_route_modes(draft, error_category="provider_error")
                timing = calculate_plan_timing(
                    planning_input, draft, facts,
                    verified_departure_fact_id=departure_fact_id,
                )
                report = validate_daily_plan(
                    planning_input, draft, facts, candidates, timing,
                    repair_states=repair.repair_states,
                )
            failed_route_days.update(self._failed_route_days(draft))
            current_routes_status = "failed" if failed_route_days else "completed"
            if initial_dispatch_recovery_code is not None:
                report = report.model_copy(update={
                    "issues": (*report.issues, ValidationIssue(
                        issue_id="dispatch-partial-collection",
                        code="DISPATCH_COLLECTION_PARTIAL",
                        category="unknown_info",
                        severity="warning",
                        scope="plan",
                        message="信息收集阶段提前结束；草稿仅使用已核实结果，仍可能有相关缺项。",
                        triggers_repair=False,
                        based_on_revision=draft.revision,
                    )),
                })
                with span("validation.dispatch_partial_notice", "validation", {
                    "error_code": initial_dispatch_recovery_code,
                    "plan_id": draft.plan_id,
                    "revision": draft.revision,
                }) as notice_span:
                    notice_span.output = {"warning_added": True, "based_on_revision": draft.revision}

            await self._progress(progress, "cost", 0, 1, "汇总已知消费参考")
            cost = aggregate_reference_costs(planning_input, draft, facts)
            summaries = self._day_summaries_with_costs(timing, cost)
            pending_leg_ids = tuple(
                leg.leg_id for leg in draft.legs
                if any(mode.status == "pending" for mode in leg.mode_results)
            )
            calculation_status = (
                "failed" if current_routes_status == "failed"
                else "pending" if pending_leg_ids
                else "completed"
            )
            backfill = None
            if route_context is not None and not route_context.cancel_event.is_set():
                revision_holder = _RevisionHolder(draft.revision)
                backfill_service = self.route_backfill_service_factory()
                backfill = make_route_backfill_runner(
                    backfill_service,
                    planning_input,
                    draft,
                    facts,
                    route_context,
                    current_revision=self._revision_reader(plan_id, revision_holder),
                )
            preview = self._preview(
                planning_input,
                draft,
                facts,
                candidates,
                report,
                summaries,
                calculation_status,
                pending_leg_ids,
                backfill_status="pending" if backfill is not None else "failed",
            )
            snapshot = PreviewSnapshot(preview=preview, timing=timing, cost=cost)
            validate_preview(preview)
            if preview.planning_input.request != request:
                raise PlanningV4PipelineError("PREVIEW_REQUEST_MISMATCH", "preview_validation")
            await self._progress(progress, "ready", 1, 1, "预览已生成")
            result = PreviewPipelineResult(
                snapshot=snapshot,
                route_context=route_context if backfill is not None else None,
                backfill=backfill,
            )
            retain_route_context = result.route_context is not None
            return result
        except asyncio.CancelledError:
            cancel_event.set()
            if route_context is not None:
                route_context.cancel_event.set()
            raise
        except PlanningV4PipelineError:
            cancel_event.set()
            raise
        except PlanningError as exc:
            cancel_event.set()
            raise PlanningV4PipelineError(exc.code, getattr(exc, "step", None) or "planning") from None
        except Exception as exc:
            cancel_event.set()
            raise self._stage_error(exc, "planning", "PLANNING_PIPELINE_FAILED") from None
        finally:
            if route_context is not None and not retain_route_context:
                try:
                    await route_context.aclose()
                except Exception:
                    pass

    async def _interpret_requirements(self, request, deadline, cancel_event) -> RequirementSummary:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise PlanningV4PipelineError("PIPELINE_DEADLINE_BEFORE_REQUIREMENTS", "planning_input")
        timeout = min(MODEL_TIMEOUT_SECONDS, remaining)
        interpreter = self.requirement_interpreter_factory(timeout)
        stage_cancel_event = Event()
        try:
            value = await self._await_stage(
                asyncio.to_thread(
                    interpreter.interpret,
                    request,
                    deadline_monotonic=deadline,
                    cancel_event=stage_cancel_event,
                ),
                timeout=timeout,
                stage="planning_input",
                cancel_event=stage_cancel_event,
                global_deadline=deadline,
                global_cancel_event=cancel_event,
                timeout_code="REQUIREMENT_MODEL_TIMEOUT",
            )
        except PlanningV4PipelineError:
            raise
        except asyncio.CancelledError:
            stage_cancel_event.set()
            raise
        except PlanningError as exc:
            raise PlanningV4PipelineError(exc.code, getattr(exc, "step", None) or "planning_input") from None
        except Exception as exc:
            raise self._stage_error(exc, "planning_input", "REQUIREMENT_INTERPRETATION_FAILED") from None
        if not isinstance(value, RequirementSummary) or value.request_id != request.request_id:
            raise PlanningV4PipelineError("REQUIREMENT_SUMMARY_INVALID", "planning_input")
        return value

    async def _collect_for_repair(
        self,
        request_data: DayRepairCollectionRequest,
        *,
        task_id: str,
        deadline: float,
        collection_revision: list[int],
        cancel_event: Event,
    ) -> DayRepairCollectionResult:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return DayRepairCollectionResult(
                stop_reason="deadline",
                fact_store=request_data.fact_store,
                candidates=request_data.candidates,
                area_hints=request_data.area_hints,
                task_results=request_data.task_results,
            )
        collection_revision[0] = max(collection_revision[0] + 1, request_data.draft.revision + 1)
        context_revision = collection_revision[0]
        scope = self._dispatch_scope(request_data)
        timeout = min(self.initial_collection_timeout_seconds, remaining)
        service = self.dispatch_service_factory(timeout)
        context = PlanningDispatchContext(
            planning_input=request_data.planning_input,
            run_id=task_id,
            context_revision=context_revision,
            base_revision=request_data.draft.revision,
            fact_store=request_data.fact_store,
            candidate_refs=request_data.candidates,
            area_hints=request_data.area_hints,
            # Prior TaskResults use another fixed context_revision and are not valid inputs.
            existing_tasks=(),
            task_results=(),
            dispatch_scope=scope,
        )
        try:
            result = await self._await_global_deadline(
                service.run_async(context),
                deadline=deadline,
                stage="repair_collection",
                cancel_event=cancel_event,
            )
        except PlanningV4PipelineError as exc:
            if exc.global_deadline:
                return DayRepairCollectionResult(
                    stop_reason="deadline", fact_store=request_data.fact_store,
                    candidates=request_data.candidates, area_hints=request_data.area_hints,
                    task_results=request_data.task_results,
                )
            raise DayRepairError(exc.code) from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._remaining(deadline) <= 0:
                cancel_event.set()
                return DayRepairCollectionResult(
                    stop_reason="deadline", fact_store=request_data.fact_store,
                    candidates=request_data.candidates, area_hints=request_data.area_hints,
                    task_results=request_data.task_results,
                )
            raise DayRepairError(self._safe_code(exc, "REPAIR_COLLECTION_FAILED")) from None
        try:
            self._validate_dispatch_result(
                result, request_data.planning_input, task_id,
                context_revision, request_data.draft.revision,
            )
        except PlanningV4PipelineError as exc:
            if self._is_dispatch_data_corruption(exc.code):
                raise DayRepairError(exc.code, fatal=True) from None
            return DayRepairCollectionResult(
                stop_reason="technical_error",
                fact_store=request_data.fact_store,
                candidates=request_data.candidates,
                area_hints=request_data.area_hints,
                task_results=request_data.task_results,
                error_code=exc.code,
            )
        if result.stop_reason == "technical_error":
            code = result.error.code if result.error else "REPAIR_DISPATCH_TECHNICAL_ERROR"
            if self._is_dispatch_data_corruption(code):
                raise DayRepairError(code, fatal=True)
            return DayRepairCollectionResult(
                stop_reason="technical_error",
                fact_store=result.fact_store,
                candidates=result.candidate_refs,
                area_hints=result.area_hints,
                task_results=self._merge_task_results(request_data.task_results, result.task_results),
                error_code=code,
            )
        if result.stop_reason == "deadline":
            if self._remaining(deadline) <= 0:
                return DayRepairCollectionResult(
                    stop_reason="deadline", fact_store=request_data.fact_store,
                    candidates=request_data.candidates, area_hints=request_data.area_hints,
                    task_results=request_data.task_results,
                )
            # The dispatcher's own collection window expired. Keep its verified partial
            # snapshot, consume this repair round, and let F09 continue with other dates.
            return DayRepairCollectionResult(
                stop_reason="technical_error",
                fact_store=result.fact_store,
                candidates=result.candidate_refs,
                area_hints=result.area_hints,
                task_results=self._merge_task_results(request_data.task_results, result.task_results),
                error_code="REPAIR_COLLECTION_TIMEOUT",
            )
        return DayRepairCollectionResult(
            stop_reason="no_progress" if result.stop_reason == "no_progress" else "target_met",
            fact_store=result.fact_store,
            candidates=result.candidate_refs,
            area_hints=result.area_hints,
            task_results=self._merge_task_results(request_data.task_results, result.task_results),
            error_code=result.error.code if result.error else None,
        )

    @staticmethod
    def _dispatch_scope(request_data: DayRepairCollectionRequest) -> DispatchScope:
        targets = []
        for item in request_data.targets:
            issue = next((issue for issue in request_data.issues if issue.issue_id in item.issue_ids), None)
            targets.append(DispatchTarget(
                target_id=f"{item.day_id}:{item.role}:{','.join(item.issue_ids)}",
                issue_code=issue.code if issue else "repair_target",
                issue_ids=item.issue_ids,
                issue_message=issue.message[:500] if issue else None,
                role=item.role,
                day_id=item.day_id,
                slot_ids=item.slot_ids,
                requirement_ids=item.requirement_ids,
                candidate_ids=item.candidate_ids,
                area_hint_ids=(),
            ))
        target_days = {item.day_id for item in targets}
        day_by_id = {item.day_id: item for item in request_data.draft.days}
        place_by_id = {item.fact_id: item for item in request_data.fact_store.places}
        candidate_by_id = {item.candidate_id: item for item in request_data.candidates}
        anchors: dict[tuple[str, str], DispatchAnchorSummary] = {}
        activities = []
        for day_id in target_days:
            day = day_by_id.get(day_id)
            if day is None:
                continue
            for activity in day.activities:
                place = place_by_id.get(activity.place_fact_id or "")
                activities.append(DispatchActivitySummary(
                    day_id=day_id,
                    activity_id=activity.activity_id,
                    kind=activity.kind,
                    slot_id=activity.slot_id,
                    poi_id=activity.poi_id,
                    place_fact_id=activity.place_fact_id,
                    place_name=place.name if place is not None else None,
                ))
                if (place is not None and place.poi_id == activity.poi_id
                        and PlanningV4Pipeline._verified_same_city_place(
                            place, request_data.planning_input.request.city,
                        )):
                    key = (day_id, place.fact_id)
                    anchors[key] = DispatchAnchorSummary(
                        day_id=day_id,
                        poi_id=place.poi_id,
                        place_fact_id=place.fact_id,
                        place_name=place.name,
                    )
            # A verified lodging place can ground a dining search when this date has
            # no sightseeing anchor yet. It remains bound to this target day only.
            lodging = day.lodging
            lodging_place = place_by_id.get(lodging.place_fact_id or "") if lodging else None
            has_sightseeing_anchor = any(
                activity.kind == "sightseeing"
                and (day_id, activity.place_fact_id or "") in anchors
                for activity in day.activities
            ) or any(
                target.day_id == day_id
                and target.role == "attractions"
                and candidate_by_id.get(candidate_id) is not None
                and candidate_by_id[candidate_id].category == "sightseeing"
                and (not candidate_by_id[candidate_id].slot_ids
                     or bool(set(candidate_by_id[candidate_id].slot_ids) & set(target.slot_ids)))
                and place_by_id.get(candidate_by_id[candidate_id].place_fact_id) is not None
                and place_by_id[candidate_by_id[candidate_id].place_fact_id].poi_id
                    == candidate_by_id[candidate_id].poi_id
                and PlanningV4Pipeline._verified_same_city_place(
                    place_by_id[candidate_by_id[candidate_id].place_fact_id],
                    request_data.planning_input.request.city,
                )
                for target in targets
                for candidate_id in target.candidate_ids
            )
            needs_dining_anchor = any(
                target.day_id == day_id and target.role == "dining" for target in targets
            )
            if (needs_dining_anchor and not has_sightseeing_anchor
                    and lodging is not None and lodging_place is not None
                    and lodging_place.poi_id == lodging.poi_id
                    and PlanningV4Pipeline._verified_same_city_place(
                        lodging_place, request_data.planning_input.request.city,
                    )):
                anchors[(day_id, lodging_place.fact_id)] = DispatchAnchorSummary(
                    day_id=day_id,
                    poi_id=lodging_place.poi_id,
                    place_fact_id=lodging_place.fact_id,
                    place_name=lodging_place.name,
                )
        # Existing candidates are eligible as static anchors only when this F09
        # target explicitly names them and their slot bindings include this day.
        for target in targets:
            expected_category = "meal" if target.role == "dining" else "sightseeing"
            for candidate_id in target.candidate_ids:
                candidate = candidate_by_id.get(candidate_id)
                if (candidate is None or candidate.category != expected_category
                        or (candidate.slot_ids and not set(candidate.slot_ids) & set(target.slot_ids))):
                    continue
                place = place_by_id.get(candidate.place_fact_id)
                if (place is None or place.poi_id != candidate.poi_id
                        or not PlanningV4Pipeline._verified_same_city_place(
                            place, request_data.planning_input.request.city,
                        )):
                    continue
                anchors[(target.day_id, place.fact_id)] = DispatchAnchorSummary(
                    day_id=target.day_id,
                    poi_id=place.poi_id,
                    place_fact_id=place.fact_id,
                    place_name=place.name,
                )

        area_ids_by_day: dict[str, set[str]] = {day_id: set() for day_id in target_days}
        for target in targets:
            day_anchor_facts = {
                fact_id for day_id, fact_id in anchors if day_id == target.day_id
            }
            day_anchor_pois = {
                anchor.poi_id for (day_id, _), anchor in anchors.items()
                if day_id == target.day_id
            }
            for hint in request_data.area_hints:
                if (hint.basis_fact_ids and hint.anchor_poi_ids
                        and set(hint.basis_fact_ids).issubset(day_anchor_facts)
                        and set(hint.anchor_poi_ids).issubset(day_anchor_pois)):
                    area_ids_by_day[target.day_id].add(hint.area_hint_id)

        scoped_targets = tuple(target.model_copy(update={
            "area_hint_ids": tuple(sorted(area_ids_by_day.get(target.day_id, ()))),
        }) for target in targets)
        return DispatchScope(
            targets=scoped_targets,
            existing_activities=tuple(activities),
            anchors=tuple(anchors[key] for key in sorted(anchors)),
        )

    @staticmethod
    def _verified_same_city_place(place, city: str) -> bool:
        if place.location is None or not place.city:
            return False

        def key(value: str) -> str:
            normalized = "".join(value.split()).casefold()
            for suffix in ("特别行政区", "自治州", "地区", "市"):
                if normalized.endswith(suffix):
                    return normalized[:-len(suffix)]
            return normalized

        return key(place.city) == key(city)

    async def _current_routes(
        self,
        route_service,
        planning_input: PlanningInput,
        draft: PlanDraft,
        facts: FactStore,
        *,
        endpoint_execution: EndpointVerificationExecution | None,
        endpoint_context_revision: int,
        departure_fact_id: str | None,
        route_context,
        deadline: float,
        cancel_event: Event,
    ) -> tuple[PlanDraft, FactStore, PlanTimingResult, str]:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            cancel_event.set()
            route_context.cancel_event.set()
            failed_draft, timing = self._pending_routes(
                planning_input, draft, facts, departure_fact_id, error_category="timeout",
            )
            return failed_draft, facts, timing, "failed"
        route_stage_cancel_event = Event()
        try:
            routed = await self._await_stage(
                route_service.run_async(
                    planning_input,
                    draft,
                    facts,
                    endpoint_execution=endpoint_execution,
                    verified_departure_fact_id=departure_fact_id,
                    current_context_revision=(endpoint_context_revision if endpoint_execution else None),
                    route_context=route_context,
                ),
                timeout=remaining,
                stage="current_routes",
                cancel_event=route_stage_cancel_event,
                global_deadline=deadline,
                global_cancel_event=cancel_event,
                timeout_code="CURRENT_ROUTE_TIMEOUT",
            )
            if self._has_pending_route_legs(routed.draft):
                route_context.cancel_event.set()
                failed_draft = self._mark_pending_route_modes(
                    routed.draft, error_category="provider_error",
                )
                failed_timing = calculate_plan_timing(
                    planning_input, failed_draft, routed.fact_store,
                    verified_departure_fact_id=departure_fact_id,
                )
                return failed_draft, routed.fact_store, failed_timing, "failed"
            return routed.draft, routed.fact_store, routed.timing, "completed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # F06 already produced a validated draft. A route provider/service error leaves
            # route durations unknown; expose a finished failure state instead of leaving
            # pending legs that no current-route worker will update.
            category = (
                "timeout"
                if isinstance(exc, (PlanningV4PipelineError, asyncio.TimeoutError))
                else "provider_error"
            )
            if isinstance(exc, PlanningV4PipelineError) and exc.global_deadline:
                cancel_event.set()
        route_context.cancel_event.set()
        failed_draft, timing = self._pending_routes(
            planning_input, draft, facts, departure_fact_id, error_category=category,
        )
        return failed_draft, facts, timing, "failed"

    @staticmethod
    def _pending_routes(
        planning_input, draft, facts, departure_fact_id, *, error_category="timeout",
    ):
        try:
            _, legs = _build_edges(planning_input, draft, facts, departure_fact_id)
        except Exception:
            legs = list(draft.legs)
        failed_draft = draft.model_copy(update={"legs": tuple(legs)}, deep=True)
        failed_draft = PlanningV4Pipeline._mark_pending_route_modes(
            failed_draft, error_category=error_category,
        )
        timing = calculate_plan_timing(
            planning_input, failed_draft, facts,
            verified_departure_fact_id=departure_fact_id,
        )
        return failed_draft, timing

    @staticmethod
    def _failed_day_route_recalculation(
        request_data: DayRepairRecalculationRequest,
        departure_fact_id: str | None,
        *,
        error_category: str,
    ) -> DayRepairRecalculation:
        """路线查询失败时，仅把目标日新顺序写成终态未知路线。"""
        try:
            target_legs = build_pending_route_legs_for_day(
                request_data.planning_input,
                request_data.draft,
                request_data.facts,
                request_data.day_id,
                verified_departure_fact_id=departure_fact_id,
            )
            legs_by_day: dict[str, list] = {}
            for leg in request_data.draft.legs:
                if leg.day_id != request_data.day_id:
                    legs_by_day.setdefault(leg.day_id, []).append(
                        leg.model_copy(update={"plan_revision": request_data.draft.revision})
                    )
            legs_by_day[request_data.day_id] = list(target_legs)
            combined = tuple(
                leg
                for day in request_data.planning_input.days
                for leg in legs_by_day.get(day.day_id, ())
            )
            failed_draft = request_data.draft.model_copy(
                update={"legs": combined}, deep=True,
            )
            failed_draft = PlanningV4Pipeline._mark_pending_route_modes(
                failed_draft, error_category=error_category,
            )
            timing = calculate_plan_timing(
                request_data.planning_input,
                failed_draft,
                request_data.facts,
                verified_departure_fact_id=departure_fact_id,
            )
            report = validate_daily_plan(
                request_data.planning_input,
                failed_draft,
                request_data.facts,
                request_data.candidates,
                timing,
                repair_states=request_data.repair_states,
            )
            return DayRepairRecalculation(
                draft=failed_draft,
                facts=request_data.facts,
                timing=timing,
                report=report,
            )
        except CurrentRouteError as exc:
            raise DayRepairError(exc.code, fatal=True) from None
        except DayRepairError:
            raise
        except Exception:
            raise DayRepairError("REPAIR_ROUTE_FALLBACK_INVALID", fatal=True) from None

    @staticmethod
    def _failed_route_days(draft: PlanDraft) -> set[str]:
        return {
            leg.day_id for leg in draft.legs
            if leg.mode_results and all(mode.status == "error" for mode in leg.mode_results)
        }

    @staticmethod
    def _record_route_day_result(
        failed_days: set[str], draft: PlanDraft, day_id: str, *, succeeded: bool,
    ) -> None:
        if succeeded:
            failed_days.discard(day_id)
        else:
            failed_days.add(day_id)
        failed_days.update(PlanningV4Pipeline._failed_route_days(draft))

    @staticmethod
    def _mark_pending_route_modes(draft, *, error_category):
        from app.models.planning_v4.plan import RouteModeResult

        failed_legs = tuple(leg.model_copy(update={
            "mode_results": tuple(
                RouteModeResult(mode=mode.mode, status="error", error_category=error_category)
                if mode.status == "pending" else mode
                for mode in leg.mode_results
            ),
        }, deep=True) for leg in draft.legs)
        return draft.model_copy(update={"legs": failed_legs}, deep=True)

    @staticmethod
    def _preview(
        planning_input,
        draft,
        facts,
        candidates,
        report,
        summaries,
        current_routes,
        pending_leg_ids,
        *,
        backfill_status,
    ):
        from app.models.planning_v4 import ItineraryPreview, PreviewCalculation

        return ItineraryPreview(
            preview_id="preview_" + uuid4().hex,
            planning_input=planning_input,
            draft=draft,
            facts=facts,
            candidates=candidates,
            validation=report,
            day_summaries=summaries,
            calculation=PreviewCalculation(
                based_on_revision=draft.revision,
                calculation_id="calculation_" + uuid4().hex,
                current_routes=current_routes,
                alternative_backfill=backfill_status,
                pending_leg_ids=pending_leg_ids,
            ),
        )

    @staticmethod
    def _day_summaries_with_costs(timing, cost):
        by_cost_day = {day.day_id: day for day in cost.days}
        summaries = []
        for timing_day in timing.days:
            cost_day = by_cost_day[timing_day.day_id]
            cost_status = (
                "available" if cost_day.status in {"all_known", "known_partial"} else "missing"
            )
            summaries.append(timing_day.summary.model_copy(update={
                "cost_status": cost_status,
                "known_cost_subtotal": str(cost_day.known_sightseeing_meal_per_person_subtotal),
            }))
        return tuple(summaries)

    @staticmethod
    def _verified_departure(result: PlanningDispatchResult, task_id: str, request: TripRequest):
        execution = next((item for item in reversed(result.endpoint_results)
                          if item.departure.status == "verified"), None)
        if execution is None:
            return None, None
        if (execution.run_id != task_id or execution.request_id != result.request_id
                or execution.context_revision != 1 or execution.base_revision is not None
                or not request.departure_place_query
                or execution.departure.user_query != request.departure_place_query):
            raise PlanningV4PipelineError("ENDPOINT_RESULT_IDENTITY_MISMATCH", "dispatch")
        return execution.departure.fact_id, execution

    @staticmethod
    def _validate_dispatch_result(result, planning_input, run_id, context_revision, base_revision):
        if not isinstance(result, PlanningDispatchResult):
            raise PlanningV4PipelineError("DISPATCH_RESULT_INVALID", "dispatch")
        if (result.request_id != planning_input.request.request_id or result.run_id != run_id
                or result.context_revision != context_revision or result.base_revision != base_revision):
            raise PlanningV4PipelineError("DISPATCH_RESULT_IDENTITY_MISMATCH", "dispatch")

    @staticmethod
    def _merge_task_results(previous, incoming):
        merged = {item.task_id: item for item in previous}
        for item in incoming:
            existing = merged.get(item.task_id)
            if existing is not None and existing != item:
                raise DayRepairError("TASK_RESULT_ID_CONFLICT", fatal=True)
            merged[item.task_id] = item
        return tuple(merged.values())

    @staticmethod
    def _has_pending_route_legs(draft):
        return any(mode.status == "pending" for leg in draft.legs for mode in leg.mode_results)

    async def _await_stage(
        self,
        awaitable,
        *,
        timeout,
        stage,
        cancel_event,
        global_deadline: float | None = None,
        global_cancel_event: Event | None = None,
        timeout_code: str | None = None,
    ):
        try:
            return await asyncio.wait_for(awaitable, timeout=max(0.001, timeout))
        except asyncio.TimeoutError:
            cancel_event.set()
            is_global_deadline = (
                global_deadline is not None and self._remaining(global_deadline) <= 0
            )
            if is_global_deadline and global_cancel_event is not None:
                global_cancel_event.set()
            code = "PIPELINE_DEADLINE" if is_global_deadline else (
                timeout_code or f"{stage.upper()}_TIMEOUT"
            )
            raise PlanningV4PipelineError(
                code, stage, global_deadline=is_global_deadline,
            ) from None
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    async def _await_global_deadline(
        self,
        awaitable,
        *,
        deadline: float,
        stage: str,
        cancel_event: Event,
    ):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            cancel_event.set()
            raise PlanningV4PipelineError("PIPELINE_DEADLINE", stage, global_deadline=True)
        try:
            # The called service owns its shorter collection deadline and must return
            # its verified partial snapshot. This outer guard only protects the pipeline's
            # global deadline and gives the service a small return/serialization window.
            return await asyncio.wait_for(
                awaitable,
                timeout=remaining + GLOBAL_STAGE_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            is_global_deadline = self._remaining(deadline) <= 0
            if is_global_deadline:
                cancel_event.set()
            code = "PIPELINE_DEADLINE" if is_global_deadline else f"{stage.upper()}_GUARD_TIMEOUT"
            raise PlanningV4PipelineError(
                code, stage, global_deadline=is_global_deadline,
            ) from None
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    @staticmethod
    def _is_dispatch_data_corruption(code: str) -> bool:
        """仅将身份、事实闭包和候选/区域引用破坏视为不可恢复错误。"""
        exact = {
            "DISPATCH_RESULT_IDENTITY_MISMATCH",
            "TASK_EXECUTION_REQUEST_MISMATCH",
            "TASK_RESULT_IDENTITY_MISMATCH",
            "FACT_ID_CONFLICT",
            "FACT_SOURCE_ID_CONFLICT",
            "CANDIDATE_ID_CONFLICT",
            "AREA_HINT_ID_CONFLICT",
            "CANDIDATE_SOURCE_INVALID",
            "PLACE_FACT_SOURCE_INVALID",
            "WEATHER_FACT_SOURCE_INVALID",
            "ROUTE_FACT_SOURCE_INVALID",
        }
        return code in exact or "IDENTITY_MISMATCH" in code

    async def _progress(self, callback, stage, completed, total, message):
        value = callback(PreviewProgress(
            stage=stage,
            completed=completed,
            total=total,
            message=message,
        ))
        if inspect.isawaitable(value):
            await value

    def _check_deadline(self, deadline, stage):
        if self._remaining(deadline) <= 0:
            raise PlanningV4PipelineError("PIPELINE_DEADLINE", stage)

    def _remaining(self, deadline):
        return max(0.0, deadline - self.clock())

    @staticmethod
    def _safe_code(exc, fallback):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code and all(character.isalnum() or character in "_.-" for character in code):
            return code[:100]
        return fallback

    @staticmethod
    def _normalize_day_revision_execution(value: Any) -> DayRevision:
        """把F06执行统计包装解开；状态机只接收已绑定的单日修订契约。"""
        if isinstance(value, DailyDayRevisionExecution):
            value = value.revision
        # 注入式适配器和离线替身也允许直接返回契约对象。
        if isinstance(value, DayRevision):
            return value
        raise DayRepairError("DAY_REVISION_RESULT_INVALID")

    def _revision_reader(self, plan_id: str, fallback: _RevisionHolder) -> Callable[[], int]:
        def current_revision() -> int:
            if self.current_revision_lookup is None:
                return fallback.value
            revision = self.current_revision_lookup(plan_id)
            # A missing draft means the preview was removed or never persisted. Treat it
            # as superseded so a late background query cannot run against an orphan.
            return revision if isinstance(revision, int) and not isinstance(revision, bool) else 0

        return current_revision

    @classmethod
    def _stage_error(cls, exc, stage, fallback):
        return PlanningV4PipelineError(cls._safe_code(exc, fallback), stage)

    @staticmethod
    def _requirement_factory(timeout):
        from app.agents.requirement_interpreter import RequirementInterpreter

        return RequirementInterpreter(timeout_seconds=timeout)

    @staticmethod
    def _dispatch_factory(timeout):
        return PlanningDispatchService(total_timeout_seconds=timeout)

    @staticmethod
    def _daily_plan_factory(timeout):
        from app.agents.daily_plan_generator import DailyPlanGenerator

        return DailyPlanGeneratorAdapter(DailyPlanGenerator(timeout_seconds=timeout))
