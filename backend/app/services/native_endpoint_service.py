"""原生规划到离地点事实；保留原文、歧义和失败，不补造交通枢纽。"""
from app.models.planning_evidence import EndpointEvidence
from app.planning.collection_evidence import endpoint_scopes as _scopes, _context, _checked_execution, _combine, validate_endpoint_evidence
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


@dataclass(frozen=True)
class NativeEndpointResult:
    run_id: str
    planning_input: PlanningInput = field(repr=False)
    executions: tuple[EndpointVerificationExecution, ...] = field(repr=False)
    arrival: EndpointVerification = field(repr=False)
    departure: EndpointVerification = field(repr=False)
    fact_store: FactStore = field(repr=False)

    def tool_payload(self):
        from app.planning.endpoint_projection import endpoint_tool_payload
        return endpoint_tool_payload(self)



def validate_native_endpoint_result(planning, run_id, result):
    if not isinstance(result, NativeEndpointResult):
        raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection")
    try:
        evidence = EndpointEvidence.model_validate({key: getattr(result, key) for key in ('run_id', 'planning_input', 'executions', 'arrival', 'departure', 'fact_store')})
    except (AttributeError, TypeError, ValueError):
        raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection") from None
    validate_endpoint_evidence(planning, run_id, evidence)

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
