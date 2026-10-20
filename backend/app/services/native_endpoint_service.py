"""原生规划到离地点事实；保留原文、歧义和失败，不补造交通枢纽。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

from app.agents.execution import PlanningError
from app.models.planning_v4.facts import FactStore, FactSource, ToolResult, ToolError, PlaceSearchData
from app.models.planning_v4.input import PlanningInput
from app.services.planning_lodging_policy import city_for_days
from app.services.endpoint_verification_service import (
    EndpointVerification, EndpointVerificationContext, EndpointVerificationExecution, EndpointVerificationService,
    _EndpointQueryRecord, _project_execution, _validate_context, _exact_candidates, _city_conflicts,
    _reconcile_verified_endpoints, _fact_store,
)


def _scopes(planning):
    groups = {}
    for kind, day in (("arrival", planning.days[0]), ("departure", planning.days[-1])):
        groups.setdefault(city_for_days(planning, (day.day_id,)), []).append(kind)
    return tuple((city, tuple(kinds)) for city, kinds in groups.items())


def _context(planning, run_id, kinds):
    return EndpointVerificationContext.from_planning_input(planning, run_id=run_id,
        context_revision=1, endpoint_kinds=kinds)


def _checked_execution(context, execution):
    """从地图诊断结果重建核实状态，拒绝把歧义候选冒充唯一地点。"""
    if not isinstance(execution, EndpointVerificationExecution):
        raise ValueError("到离地点结果类型无效")
    records = {}
    for kind in context.endpoint_kinds:
        query = getattr(context.planning_input.request, f"{kind}_place_query")
        if not query or not query.strip():
            continue
        endpoint = getattr(execution, kind)
        search = endpoint.search_result
        if search is None or search.source.tool_name != "maps_text_search" or search.tool_call_id != search.source.tool_call_id:
            raise ValueError("到离地点搜索来源无效")
        key = query.strip()
        record = _EndpointQueryRecord(key, search)
        exact, conflicts = _exact_candidates(key, search.data.places if search.data is not None else ())
        if len(endpoint.detail_results) > 1:
            raise ValueError("到离地点详情范围无效")
        if endpoint.detail_results:
            if (search.status != "ok" or len(exact) != 1 or conflicts
                    or (exact[0].location is not None and exact[0].city)
                    or _city_conflicts(exact[0].city, execution.city)):
                raise ValueError("到离地点不能查询范围外的详情")
            detail = endpoint.detail_results[0]
            if detail.source.tool_name != "maps_search_detail" or detail.tool_call_id != detail.source.tool_call_id:
                raise ValueError("到离地点详情来源无效")
            record.details[exact[0].poi_id] = detail
        if key in records and (records[key].search != search or records[key].details != record.details):
            raise ValueError("同一到离查询结果冲突")
        records[key] = record
    expected = _project_execution(context, records)
    if expected != execution:
        raise ValueError("到离地点核实状态与地图结果不一致")


def _combine(executions, scopes):
    endpoints = {kind: getattr(execution, kind)
                 for (_, kinds), execution in zip(scopes, executions) for kind in kinds}
    endpoints, facts = _reconcile_verified_endpoints(endpoints)
    return endpoints, _fact_store(facts, endpoints)


@dataclass(frozen=True)
class NativeEndpointResult:
    run_id: str
    planning_input: PlanningInput = field(repr=False)
    executions: tuple[EndpointVerificationExecution, ...] = field(repr=False)
    arrival: EndpointVerification = field(repr=False)
    departure: EndpointVerification = field(repr=False)
    fact_store: FactStore = field(repr=False)

    def tool_payload(self):
        payload = {}
        for kind, day in (("arrival", self.planning_input.days[0]), ("departure", self.planning_input.days[-1])):
            endpoint = getattr(self, kind)
            place = next((p for p in self.fact_store.places if p.fact_id == endpoint.fact_id), None)
            payload[kind] = {"kind": kind, "user_query": endpoint.user_query,
                "city": city_for_days(self.planning_input, (day.day_id,)), "day_id": day.day_id,
                "status": endpoint.status, "availability": "known" if endpoint.status == "verified" else "unknown",
                "reason": endpoint.reason, "fact_id": endpoint.fact_id,
                "place": place.model_dump(mode="json", exclude={"photos"}) if place is not None else None,
                "candidates": [{"poi_id": p.poi_id, "name": p.name, "city": p.city, "address": p.address}
                               for p in endpoint.candidates]}
        return {**payload, "sources": [s.model_dump(mode="json") for s in self.fact_store.sources],
            "policy": "仅verified到离地点可采用；歧义和缺省保持未知。地点不是活动候选，不代表交通、就绪时间或返程可行性已核实。"}


def validate_native_endpoint_result(planning, run_id, result):
    try:
        if not isinstance(result, NativeEndpointResult) or result.run_id != run_id or result.planning_input != planning:
            raise ValueError("到离地点运行身份不一致")
        scopes = _scopes(planning)
        if len(result.executions) != len(scopes):
            raise ValueError("到离地点范围不完整")
        for (_, kinds), execution in zip(scopes, result.executions):
            _checked_execution(_context(planning, run_id, kinds), execution)
        endpoints, facts = _combine(result.executions, scopes)
        if result.arrival != endpoints["arrival"] or result.departure != endpoints["departure"] or result.fact_store != facts:
            raise ValueError("到离地点合并结果不一致")
    except (AttributeError, TypeError, ValueError, KeyError, IndexError):
        raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection") from None


class NativeEndpointService:
    def __init__(self, *, adapter=None, tool_executor=None, endpoint_service=None, emit=None):
        self.endpoint_service = endpoint_service or EndpointVerificationService(adapter=adapter, tool_executor=tool_executor)
        self.emit = emit or (lambda event, data: None)

    def run(self, planning_input, *, run_id, cancel_event: Event | None = None):
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise PlanningError("CANCELLED", "到离地点查询已取消", step="endpoint_verify")

        if not isinstance(planning_input, PlanningInput) or not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
            raise PlanningError("ENDPOINT_CONTEXT_INVALID", "到离地点上下文无效", step="endpoint_preflight")
        cancelled()
        try:
            scopes = _scopes(planning_input)
            contexts = tuple(_context(planning_input, run_id, kinds) for _, kinds in scopes)
            for context in contexts:
                _validate_context(context)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            raise PlanningError("ENDPOINT_CONTEXT_INVALID", "到离地点上下文无效", step="endpoint_preflight") from None
        executions = []
        for (city, kinds), context in zip(scopes, contexts):
            cancelled()
            self.emit("endpoints.start", {"run_id": run_id, "city": city, "kinds": list(kinds)})
            try:
                execution = self.endpoint_service.run(context, cancel_event=cancel_event)
            except PlanningError:
                raise
            except Exception as exc:
                cancelled()
                category = "timeout" if isinstance(exc, TimeoutError) else "network" if isinstance(exc, ConnectionError) else "provider_error"
                records = {}
                for kind in kinds:
                    query = getattr(planning_input.request, f"{kind}_place_query")
                    if query and query.strip() and query.strip() not in records:
                        # 本地失败尝试；不伪造供应商收到的HTTP参数或地点事实。
                        source = FactSource(tool_call_id=f"endpoint-attempt-{uuid4().hex}", tool_name="maps_text_search",
                            api_version="v3", queried_at=datetime.now(timezone.utc), effective_params={})
                        search = ToolResult[PlaceSearchData](tool_call_id=source.tool_call_id, source=source, status="error",
                            error=ToolError(category=category, retryable=category in {"timeout", "network"}))
                        records[query.strip()] = _EndpointQueryRecord(query.strip(), search)
                execution = _project_execution(context, records)
            cancelled()
            try:
                _checked_execution(context, execution)
            except (AttributeError, TypeError, ValueError, KeyError, IndexError):
                raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection") from None
            executions.append(execution)
        try:
            endpoints, facts = _combine(executions, scopes)
        except (AttributeError, TypeError, ValueError, KeyError):
            raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection") from None
        result = NativeEndpointResult(run_id, planning_input, tuple(executions), endpoints["arrival"], endpoints["departure"], facts)
        validate_native_endpoint_result(planning_input, run_id, result)
        self.emit("endpoints.result", {"run_id": run_id, **result.tool_payload()})
        return result
