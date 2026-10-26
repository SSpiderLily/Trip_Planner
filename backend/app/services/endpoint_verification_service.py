"""核实用户明确填写的到达与离开地点；不推断缺省交通枢纽。"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
from threading import Event
import math
import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from app.agents.execution import PlanningError
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.facts import (
    FactSource,
    FactStore,
    MapCoordinate,
    PlaceFact,
    PlaceSearchData,
    ToolResult,
)
from app.models.planning_v4.input import PlanningInput
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import MCPToolExecutor, get_mcp_tool_executor
from app.services.observation_service import span
from app.services.planning_lodging_policy import city_for_days


from app.planning.endpoint_projection import (
    EndpointKind, EndpointStatus, _CITY_SUFFIXES, _IDENTITY_FIELDS, EndpointVerificationContext, EndpointVerification, EndpointVerificationExecution, _EndpointQueryRecord, _validate_context, _project_execution, _normalize, _normalize_city, _city_state, _city_conflicts, _coordinates_equal, _merge_place_facts, _has_place_value, _exact_candidates, _deduplicate_candidates, _place_with_detail, _verify_endpoint, _close_place_sources, _reconcile_verified_endpoints, _fact_store, _add_source_without_overwrite
)

class EndpointVerificationService:
    """只核实用户填写的端点；查询失败与未核实数据分别表达。"""

    def __init__(
        self,
        *,
        adapter: AmapContractAdapter | None = None,
        tool_executor: MCPToolExecutor | None = None,
        tool_timeout_seconds: float | None = None,
    ):
        self.adapter = adapter
        self.tool_executor = tool_executor
        if tool_timeout_seconds is None:
            from app.config import settings

            tool_timeout_seconds = settings.planner_tool_timeout_seconds
        if (isinstance(tool_timeout_seconds, bool) or not isinstance(tool_timeout_seconds, (int, float))
                or not math.isfinite(tool_timeout_seconds) or tool_timeout_seconds <= 0):
            raise ValueError("tool_timeout_seconds 必须是有限正数")
        self.tool_timeout_seconds = float(tool_timeout_seconds)

    async def run_async(self, context: EndpointVerificationContext) -> EndpointVerificationExecution:
        """取消立即返回；执行器会阻止仍在排队的 MCP 请求启动。"""
        loop = asyncio.get_running_loop()
        copied_context = copy_context()
        cancel_event = Event()
        future = loop.run_in_executor(
            None,
            copied_context.run,
            lambda: self.run(context, cancel_event=cancel_event),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    def run(
        self,
        context: EndpointVerificationContext,
        *,
        cancel_event: Event | None = None,
    ) -> EndpointVerificationExecution:
        request, city = _validate_context(context)
        query_by_kind = {
            "arrival": request.arrival_place_query if not context.endpoint_kinds or "arrival" in context.endpoint_kinds else None,
            "departure": request.departure_place_query if not context.endpoint_kinds or "departure" in context.endpoint_kinds else None,
        }
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise PlanningError("CANCELLED", "到离地点查询已取消", step="endpoint_verify")

        cancelled()
        safe_context = {
            "request_id": context.request_id,
            "run_id": context.run_id,
            "context_revision": context.context_revision,
            "base_revision": context.base_revision,
            "city": city,
            "requested_endpoints": [kind for kind, query in query_by_kind.items() if query and query.strip()],
        }
        with span("endpoint.verify", "validation", safe_context) as record:
            normalized_queries = {
                kind: query.strip() if isinstance(query, str) and query.strip() else None
                for kind, query in query_by_kind.items()
            }
            records: dict[str, _EndpointQueryRecord] = {}
            adapter = self.adapter
            for query in dict.fromkeys(query for query in normalized_queries.values() if query is not None):
                cancelled()
                if adapter is None:
                    adapter = self._adapter_for(cancel_event)
                search = adapter.search_text(
                    query,
                    city,
                    citylimit=True,
                    page=1,
                    offset=20,
                    extensions="base",
                )
                records[query] = _EndpointQueryRecord(query, search)
                cancelled()
                exact, duplicate_conflicts = _exact_candidates(query, search.data.places if search.data else ())
                # Detail is requested only when one exact identity still needs location or city evidence.
                if search.status == "ok" and len(exact) == 1 and not duplicate_conflicts:
                    candidate = exact[0]
                    if candidate.location is None or not candidate.city:
                        if not _city_conflicts(candidate.city, city):
                            records[query].details[candidate.poi_id] = adapter.search_detail(candidate.poi_id)
            cancelled()
            execution = _project_execution(context, records)
            record.output = {
                "arrival_status": execution.arrival.status,
                "arrival_reason": execution.arrival.reason,
                "departure_status": execution.departure.status,
                "departure_reason": execution.departure.reason,
                "search_calls": len(records),
                "detail_calls": sum(len(item.details) for item in records.values()),
                "verified_fact_count": len(execution.fact_store.places),
            }
            return execution

    def _adapter_for(self, cancel_event: Event | None) -> AmapContractAdapter:
        executor = self.tool_executor or get_mcp_tool_executor()

        def tool_runner(tool_name: str, arguments: dict) -> object:
            return executor.run(
                tool_name,
                arguments,
                timeout_seconds=self.tool_timeout_seconds,
                cancel_event=cancel_event,
            )

        return AmapContractAdapter(tool_runner=tool_runner)
