"""正式原生规划工厂与纯预览适配，复用相邻交通、编辑和保存链。"""
from __future__ import annotations

from app.services.native_observation import native_event_emitter

import asyncio
import math
from functools import wraps
from app.services.native_observation import observe_native_run
from app.services.planning_diagnostics import observe_pipeline_run
from uuid import uuid4

from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_outcome import PlanningOutcome
from app.planning.validation import validate_snapshot, validate_selected_draft
from app.models.planning_runtime import PlanningRunIdentity
from app.models.planning_v4 import ItineraryPreview, PreviewCalculation, TripRequest, ValidationIssue
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.current_route_service import CurrentRouteService, build_pending_route_legs_for_day
from app.services.native_planning_runtime import NativePlanningRuntime
from app.services.native_tool_loop import NativeToolLoopError
from app.services.planning_timeout_policy import planning_timeout_scope
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan, is_required_place_gap
from app.services.preview_task_service import PreviewPipelineResult
from app.services.preview_notice_policy import preserve_preview_notices
from app.services.reference_cost import aggregate_reference_costs


class NativePreviewError(RuntimeError):
    def __init__(self, code, stage):
        self.code, self.stage = code, stage
        super().__init__(code)


def native_preview_deadline(function):
    """总预算覆盖交付；首次采集预算从dispatch开始，取消后禁止后台线程交付。"""
    @wraps(function)
    async def run(self, request, *, task_id, plan_id, progress=None):
        if not self.enforce_timeouts:
            return await function(self, request, task_id=task_id, plan_id=plan_id, progress=progress)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.total_timeout_seconds
        collecting = False
        collection_started = False
        try:
            async with asyncio.timeout_at(deadline) as timeout:
                async def report(update):
                    nonlocal collecting, collection_started
                    if update.stage == "dispatch" and update.completed == 0 and not collection_started:
                        collecting = collection_started = True
                        timeout.reschedule(min(deadline, loop.time() + self.initial_collection_timeout_seconds))
                    elif collecting and ((update.stage == "dispatch" and update.total > 0 and update.completed >= update.total)
                                          or update.stage == "proposal"):
                        collecting = False
                        timeout.reschedule(deadline)
                    if progress is not None:
                        await progress(update)
                return await function(self, request, task_id=task_id, plan_id=plan_id, progress=report)
        except TimeoutError:
            code = "INITIAL_COLLECTION_TIMEOUT" if collecting and loop.time() < deadline else "PIPELINE_DEADLINE"
            raise NativePreviewError(code, "dispatch" if code == "INITIAL_COLLECTION_TIMEOUT" else "planning") from None
    return run


def _check_result(result, request, identity):
    if not isinstance(result, PlanningOutcome):
        raise NativePreviewError("NATIVE_RESULT_INVALID", "preview_input")
    planning, draft, snapshot = result.planning_input, result.draft, result.snapshot
    if (planning.request != request or snapshot.identity != identity
            or result.initial_result.planning_input != planning or result.initial_result.identity != identity
            or any((item.plan_id, item.source_run_id, item.request_id, item.rule_version) !=
                   (identity.plan_id, identity.task_id, request.request_id, planning.rules.rule_version)
                   for item in (result.initial_draft, draft))
            or draft.revision < result.initial_draft.revision):
        raise NativePreviewError("NATIVE_RESULT_IDENTITY_MISMATCH", "preview_input")
    if ((result.report.plan_id, result.report.based_on_revision) != (draft.plan_id, draft.revision)
            or (result.initial_result.report.plan_id, result.initial_result.report.based_on_revision)
            != (result.initial_draft.plan_id, result.initial_draft.revision)):
        raise NativePreviewError("NATIVE_REPORT_VERSION_MISMATCH", "preview_input")
    if result.status == "technical_error" or result.repair.status == "technical_error":
        raise NativePreviewError("NATIVE_PLANNING_TECHNICAL_ERROR", "repair")
    if result.status != result.repair.status:
        raise NativePreviewError("NATIVE_RESULT_INVALID", "preview_input")
    if (snapshot.facts != result.fact_store or snapshot.candidates != result.repair.candidates
            or snapshot.area_hints != result.repair.area_hints or snapshot.task_results != result.repair.task_results):
        raise NativePreviewError("NATIVE_FACTS_MISSING", "preview_input")
    for evidence, kinds in ((snapshot.weather_evidence, ('sources', 'weather')),
                            (snapshot.endpoint_evidence, ('sources', 'places'))):
        if evidence is None:
            continue
        for kind in kinds:
            key = 'tool_call_id' if kind == 'sources' else 'fact_id'
            rows = {getattr(row, key): row for row in getattr(snapshot.facts, kind)}
            if any(rows.get(getattr(row, key)) != row for row in getattr(evidence.fact_store, kind)):
                raise NativePreviewError("NATIVE_FACTS_MISSING", "preview_input")
    # 重验公开快照及已选地点，不能信任生产者传来的有效标志。
    validate_snapshot(snapshot)
    validate_selected_draft(snapshot, draft)
    expected = validate_daily_plan(planning, draft, snapshot.facts, snapshot.candidates,
                                  repair_states=result.repair.repair_states)
    if expected != result.report:
        raise NativePreviewError("NATIVE_REPORT_MISMATCH", "preview_input")
    if result.status == 'complete' and any(is_required_place_gap(planning, i) for i in expected.issues):
        raise NativePreviewError("NATIVE_RESULT_INVALID", "preview_input")


def _endpoint_notices(report, result):
    endpoints = result.snapshot.endpoint_evidence
    if endpoints is None:
        return report
    added = []
    for kind, label in (("arrival", "到达"), ("departure", "离开")):
        endpoint = getattr(endpoints, kind)
        if not (endpoint.user_query or "").strip() or endpoint.status == "verified":
            continue
        if endpoint.status == "not_found":
            message = f"未查到你填写的{label}地点，请核对地点名称。"
        elif endpoint.status == "ambiguous":
            message = f"你填写的{label}地点无法唯一确定，请补充具体名称或地址。"
        elif endpoint.status == "error":
            message = f"{label}地点查询失败，暂时无法核实。"
        else:
            message = f"你填写的{label}地点信息不完整，暂时无法核实。"
        added.append(ValidationIssue(issue_id=f"endpoint-{kind}-unknown", code=f"ENDPOINT_{kind.upper()}_UNKNOWN",
            category="unknown_info", severity="warning", scope="plan", target_id=kind,
            message=message,
            triggers_repair=False, based_on_revision=report.based_on_revision))
    return report.model_copy(update={"issues": (*report.issues, *added)})


class NativePreviewAdapter:
    """同步纯转换：不查询模型/地图，不落盘，不发ready或交通完成。"""

    def convert(self, result, *, request, task_id, plan_id) -> PreviewPipelineResult:
        try:
            if not isinstance(request, TripRequest):
                raise NativePreviewError("TRIP_REQUEST_INVALID", "preview_input")
            identity = PlanningRunIdentity(task_id=task_id, plan_id=plan_id)
            _check_result(result, request, identity)
            result = PlanningOutcome.model_validate(result.model_dump(mode="python"))
            planning = result.planning_input.model_copy(deep=True)
            facts = result.fact_store.model_copy(deep=True)
            candidates = tuple(candidate.model_copy(deep=True) for candidate in result.repair.candidates)
            draft = result.draft.model_copy(update={"area_hints": tuple(
                hint.model_copy(deep=True) for hint in result.repair.area_hints)}, deep=True)
            # 只按最终每日顺序构造酒店→首地点及当前相邻地点的骨架，不查询交通。
            draft = draft.model_copy(update={"legs": tuple(leg for day in draft.days
                for leg in build_pending_route_legs_for_day(planning, draft, facts, day.day_id))})
            report = validate_daily_plan(planning, draft, facts, candidates, repair_states=result.repair.repair_states)
            report = preserve_preview_notices(report, result.report)
            report = _endpoint_notices(report, result)
            cost = aggregate_reference_costs(planning, draft, facts)
            costs = {day.day_id: day for day in cost.days}
            summaries = tuple(summary.model_copy(update={
                "cost_status": "available" if costs[summary.day_id].status in {"all_known", "known_partial"} else "missing",
                "known_cost_subtotal": str(costs[summary.day_id].known_sightseeing_meal_per_person_subtotal),
            }) for summary in build_day_summaries(planning, draft, facts))
            pending_ids = tuple(leg.leg_id for leg in draft.legs if any(mode.status == "pending" for mode in leg.mode_results))
            route_days = tuple(day.day_id for day in draft.days if any(
                leg.day_id == day.day_id and leg.leg_id in pending_ids for leg in draft.legs))
            preview = ItineraryPreview(preview_id=f"preview_{uuid4().hex}", planning_input=planning, draft=draft,
                facts=facts, candidates=candidates, validation=report, day_summaries=summaries,
                calculation=PreviewCalculation(based_on_revision=draft.revision,
                    calculation_id=f"generation:{draft.plan_id}:{uuid4().hex}",
                    current_routes="pending" if pending_ids else "completed", alternative_backfill="not_required",
                    pending_leg_ids=pending_ids))
            return PreviewPipelineResult(snapshot=PreviewSnapshot(preview=preview, cost=cost), initial_route_day_ids=route_days)
        except NativePreviewError:
            raise
        except Exception:
            raise NativePreviewError("NATIVE_PREVIEW_INVALID", "preview_validation") from None


async def _close_context(context):
    if context is None:
        return
    try:
        cancel_event = getattr(context, "cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        close = getattr(context, "aclose", None)
        if close is not None:
            await close()
    except Exception:
        pass


class NativePreviewPipeline:
    """正式API生成工厂：原生地点规划交给既有预览与相邻交通服务。"""

    def __init__(self, *, planner_factory=None, adapter=None, current_route_service_factory=None,
                 enforce_timeouts=None, emit=None, total_timeout_seconds=None, initial_collection_timeout_seconds=None):
        self.planner_factory = planner_factory or NativeTripPlanner
        self.adapter = adapter or NativePreviewAdapter()
        self.current_route_service_factory = current_route_service_factory or CurrentRouteService
        if enforce_timeouts is None:
            from app.config import settings
            enforce_timeouts = settings.planner_enforce_timeouts
        if type(enforce_timeouts) is not bool:
            raise ValueError("enforce_timeouts必须是bool")
        self.enforce_timeouts = enforce_timeouts
        from app.config import settings
        self.total_timeout_seconds = settings.planner_v4_total_timeout_seconds if total_timeout_seconds is None else total_timeout_seconds
        self.initial_collection_timeout_seconds = settings.planner_v4_initial_collection_timeout_seconds if initial_collection_timeout_seconds is None else initial_collection_timeout_seconds
        for value in (self.total_timeout_seconds, self.initial_collection_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("原生规划超时预算必须为有限正数")
        self.emit = native_event_emitter(emit)

    @observe_pipeline_run
    @observe_native_run
    @planning_timeout_scope
    @native_preview_deadline
    async def run(self, request, *, task_id, plan_id, progress=None):
        runtime = NativePlanningRuntime(task_id=task_id, plan_id=plan_id, progress=progress)
        context = None
        retained = False
        try:
            runtime.check_cancelled()
            if not isinstance(request, TripRequest):
                raise NativePreviewError("TRIP_REQUEST_INVALID", "request_validation")
            planner = self.planner_factory()
            result = await planner.run_async(request, task_id=task_id, plan_id=plan_id,
                progress=progress, cancel_event=runtime.cancel_event)
            runtime.check_cancelled()
            await runtime.report("cost", message="汇总原生地点规划的消费参考与预览")
            converted = self.adapter.convert(result, request=request, task_id=task_id, plan_id=plan_id)
            runtime.check_cancelled()
            try:
                converted.route_service = self.current_route_service_factory()
                context = converted.route_service.create_route_context(converted.snapshot.preview.planning_input, plan_id)
                context.assert_binding(converted.snapshot.preview.planning_input, converted.snapshot.preview.draft)
                context.register_facts(converted.snapshot.preview.facts)
                converted.route_context = context
            except Exception as exc:
                await _close_context(context)
                context = None
                self.emit("preview.route_context_unavailable", {"plan_id": plan_id, "exception_type": type(exc).__name__})
            await runtime.report("cost", 1, message="预览已适配，等待持久化后查询相邻交通")
            self.emit("preview.converted", {"task_id": task_id, "plan_id": plan_id,
                "revision": converted.snapshot.preview.draft.revision, "route_day_ids": list(converted.initial_route_day_ids)})
            retained = context is not None
            return converted
        except BaseException as exc:
            runtime.cancel_event.set()
            if isinstance(exc, (NativePreviewError, NativeToolLoopError, asyncio.CancelledError)) or not isinstance(exc, Exception):
                raise
            raise NativePreviewError("NATIVE_PREVIEW_PIPELINE_FAILED", "planning") from None
        finally:
            if not retained:
                await _close_context(context)
