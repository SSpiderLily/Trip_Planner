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


EndpointKind = Literal["arrival", "departure"]
EndpointStatus = Literal["missing", "verified", "ambiguous", "unverified", "not_found", "error"]
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "市")
_IDENTITY_FIELDS = ("name", "city", "adcode")


class EndpointVerificationContext(ContractModel):
    """请求、运行与基准版本随端点核实结果回显。"""

    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    base_revision: int | None = Field(default=None, ge=1)
    planning_input: PlanningInput

    @model_validator(mode="after")
    def validate_request_reference(self):
        if self.request_id != self.planning_input.request.request_id:
            raise ValueError("端点上下文 request_id 与 PlanningInput 不一致")
        return self

    @classmethod
    def from_planning_input(
        cls,
        planning_input: PlanningInput,
        *,
        run_id: str,
        context_revision: int,
        base_revision: int | None = None,
    ) -> "EndpointVerificationContext":
        return cls(
            request_id=planning_input.request.request_id,
            run_id=run_id,
            context_revision=context_revision,
            base_revision=base_revision,
            planning_input=planning_input,
        )


class EndpointVerification(ContractModel):
    kind: EndpointKind
    user_query: str | None
    status: EndpointStatus
    reason: str
    fact_id: str | None = None
    candidates: tuple[PlaceFact, ...] = ()
    search_result: ToolResult[PlaceSearchData] | None = None
    detail_results: tuple[ToolResult[PlaceFact], ...] = ()

    @model_validator(mode="after")
    def validate_verified_reference(self):
        if (self.status == "verified") != (self.fact_id is not None):
            raise ValueError("只有 verified 端点可以引用唯一 fact_id")
        candidate_ids = [item.poi_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("端点候选必须按真实 POI ID 去重")
        if self.status == "verified" and (
            len(self.candidates) != 1 or self.candidates[0].fact_id != self.fact_id
        ):
            raise ValueError("verified 端点必须对应唯一候选事实")
        if self.status == "missing" and self.user_query is not None and self.user_query.strip():
            raise ValueError("missing 端点必须来自空 query")
        if self.status != "missing" and (self.user_query is None or not self.user_query.strip()):
            raise ValueError("非 missing 端点必须保留用户填写的 query")
        return self


class EndpointVerificationExecution(ContractModel):
    request_id: str
    run_id: str
    context_revision: int
    base_revision: int | None
    city: str
    arrival: EndpointVerification
    departure: EndpointVerification
    fact_store: FactStore

    @model_validator(mode="after")
    def validate_fact_closure(self):
        facts = {fact.fact_id: fact for fact in self.fact_store.places}
        sources = {source.tool_call_id: source for source in self.fact_store.sources}
        poi_ids = [fact.poi_id for fact in self.fact_store.places]
        if len(poi_ids) != len(set(poi_ids)):
            raise ValueError("端点 FactStore 必须按真实 POI ID 去重")
        for endpoint in (self.arrival, self.departure):
            if endpoint.status == "verified":
                if endpoint.fact_id not in facts:
                    raise ValueError("verified 端点必须引用 FactStore 中的地点事实")
                fact = facts[endpoint.fact_id]
                if fact.location is None or not fact.city or _city_state(fact, self.city) != "city_match":
                    raise ValueError("verified 端点必须具有同城地点和有效坐标")
                if not {"poi_id", "name", "location", "city"} <= set(fact.field_sources):
                    raise ValueError("verified 端点的身份、位置和城市字段必须标记来源")
        for fact in facts.values():
            if not fact.source_refs or not set(fact.source_refs) <= set(sources):
                raise ValueError("端点地点事实来源未闭合")
            if not set(fact.field_sources.values()) <= set(sources):
                raise ValueError("端点地点字段来源未闭合")
        return self


class _EndpointQueryRecord:
    def __init__(self, query: str, search: ToolResult[PlaceSearchData]):
        self.query = query
        self.search = search
        self.details: dict[str, ToolResult[PlaceFact]] = {}


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
            "arrival": request.arrival_place_query,
            "departure": request.departure_place_query,
        }
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
                exact, duplicate_conflicts = _exact_candidates(query, search.data.places if search.data else ())
                # Detail is requested only when one exact identity still needs location or city evidence.
                if search.status == "ok" and len(exact) == 1 and not duplicate_conflicts:
                    candidate = exact[0]
                    if candidate.location is None or not candidate.city:
                        if not _city_conflicts(candidate.city, city):
                            records[query].details[candidate.poi_id] = adapter.search_detail(candidate.poi_id)

            endpoints = {
                kind: _verify_endpoint(kind, query_by_kind[kind], normalized_queries[kind], city, records)
                for kind in ("arrival", "departure")
            }
            endpoints, verified_facts = _reconcile_verified_endpoints(endpoints)
            fact_store = _fact_store(verified_facts, endpoints)
            execution = EndpointVerificationExecution(
                request_id=context.request_id,
                run_id=context.run_id,
                context_revision=context.context_revision,
                base_revision=context.base_revision,
                city=city,
                arrival=endpoints["arrival"],
                departure=endpoints["departure"],
                fact_store=fact_store,
            )
            record.output = {
                "arrival_status": execution.arrival.status,
                "arrival_reason": execution.arrival.reason,
                "departure_status": execution.departure.status,
                "departure_reason": execution.departure.reason,
                "search_calls": len(records),
                "detail_calls": sum(len(item.details) for item in records.values()),
                "verified_fact_count": len(fact_store.places),
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


def _validate_context(context: EndpointVerificationContext):
    try:
        if not isinstance(context, EndpointVerificationContext):
            raise ValueError("端点核实上下文类型无效")
        request = context.planning_input.request
        if context.request_id != request.request_id:
            raise ValueError("端点上下文 request_id 与原始请求不一致")
        if not context.run_id or type(context.context_revision) is not int or context.context_revision < 1:
            raise ValueError("端点上下文运行标识或版本无效")
        if context.base_revision is not None and (
            type(context.base_revision) is not int or context.base_revision < 1
        ):
            raise ValueError("端点上下文基准版本无效")
        city = request.city.strip()
        if not city:
            raise ValueError("旅行城市为空")
        if context.planning_input.request.request_id != context.request_id:
            raise ValueError("PlanningInput request_id 与上下文不一致")
        return request, city
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PlanningError(
            "ENDPOINT_CONTEXT_INVALID",
            "端点核实上下文无效",
            step="endpoint_preflight",
        ) from exc


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    result = []
    for char in unicodedata.normalize("NFKC", value).casefold():
        if char.isspace() or unicodedata.category(char).startswith("P"):
            continue
        result.append(char)
    return "".join(result)


def _normalize_city(value: str | None) -> str:
    normalized = _normalize(value)
    for suffix in _CITY_SUFFIXES:
        normalized_suffix = _normalize(suffix)
        if normalized.endswith(normalized_suffix):
            normalized = normalized[:-len(normalized_suffix)]
            break
    return normalized


def _city_state(place: PlaceFact, city: str) -> str:
    if not place.city:
        return "city_unverified"
    return "city_match" if _normalize_city(place.city) == _normalize_city(city) else "city_mismatch"


def _city_conflicts(place_city: str | None, requested_city: str) -> bool:
    return bool(place_city and _normalize_city(place_city) != _normalize_city(requested_city))


def _coordinates_equal(left: MapCoordinate, right: MapCoordinate) -> bool:
    return left == right


def _merge_place_facts(places: tuple[PlaceFact, ...]) -> tuple[PlaceFact | None, str | None]:
    """同 POI ID 只合并互不矛盾的字段；冲突时不覆盖任一方。"""
    if not places:
        return None, "identity_missing"
    first = places[0]
    identity_conflict = False
    for field in _IDENTITY_FIELDS:
        values = [
            getattr(place, field)
            for place in places
            if _has_place_value(getattr(place, field))
        ]
        normalize = _normalize_city if field == "city" else _normalize
        if len({normalize(value) for value in values}) > 1:
            identity_conflict = True
    coordinates = [place.location for place in places if place.location is not None]
    coordinate_conflict = any(
        not _coordinates_equal(coordinates[0], coordinate)
        for coordinate in coordinates[1:]
    ) if coordinates else False
    conflict_reason = (
        "poi_coordinate_conflict" if coordinate_conflict
        else "poi_identity_conflict" if identity_conflict
        else None
    )
    if conflict_reason:
        return first, conflict_reason

    updates = {}
    for field in (
        "location", "address", "city", "adcode", "type_name", "typecode", "business_area",
        "opening_hours_raw",
    ):
        if not _has_place_value(getattr(first, field)):
            value = next((getattr(place, field) for place in places[1:] if _has_place_value(getattr(place, field))), None)
            if value is not None:
                updates[field] = value
    if not first.photos:
        updates["photos"] = next((place.photos for place in places[1:] if place.photos), ())
    source_refs = tuple(dict.fromkeys(ref for place in places for ref in place.source_refs))
    updates["source_refs"] = source_refs
    field_sources = dict(first.field_sources)
    for place in places[1:]:
        for field, source_ref in place.field_sources.items():
            field_sources.setdefault(field, source_ref)
    updates["field_sources"] = field_sources
    missing_by_field = {(item.field, item.reason): item for place in places for item in place.missing_fields}
    present_fields = {
        "location": next((place.location for place in places if place.location is not None), None),
        "city": next((place.city for place in places if place.city), None),
    }
    updates["missing_fields"] = tuple(
        item for item in missing_by_field.values()
        if present_fields.get(item.field) is None
    )
    return first.model_copy(update=updates), None


def _has_place_value(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip()) and value != ()


def _exact_candidates(query: str, places: tuple[PlaceFact, ...]):
    grouped: dict[str, list[PlaceFact]] = {}
    for place in places:
        grouped.setdefault(place.poi_id, []).append(place)
    unique: list[PlaceFact] = []
    conflicts: list[str] = []
    for poi_id, group in grouped.items():
        merged, conflict = _merge_place_facts(tuple(group))
        if merged is None:
            continue
        if conflict:
            conflicts.append(poi_id)
        exact_rows = [place for place in group if _normalize(place.name) == _normalize(query)]
        if exact_rows:
            unique.append(merged if conflict is None else exact_rows[0])
    return tuple(unique), tuple(conflicts)


def _deduplicate_candidates(places: tuple[PlaceFact, ...]):
    grouped: dict[str, list[PlaceFact]] = {}
    for place in places:
        grouped.setdefault(place.poi_id, []).append(place)
    unique = []
    conflicts = []
    for poi_id, group in grouped.items():
        merged, conflict = _merge_place_facts(tuple(group))
        if merged is not None:
            unique.append(merged)
        if conflict:
            conflicts.append(poi_id)
    return tuple(unique), tuple(conflicts)


def _place_with_detail(search_place: PlaceFact, detail_result: ToolResult[PlaceFact]):
    detail_place = detail_result.data
    if detail_result.status != "ok" or detail_place is None:
        return None, "detail_not_found" if detail_result.status == "empty" else "detail_invalid"
    if detail_place.poi_id != search_place.poi_id:
        return None, "detail_identity_mismatch"
    if _normalize(detail_place.name) != _normalize(search_place.name):
        return None, "poi_identity_conflict"
    if (search_place.city and detail_place.city
            and _normalize_city(search_place.city) != _normalize_city(detail_place.city)):
        return None, "poi_identity_conflict"
    if (search_place.adcode and detail_place.adcode
            and search_place.adcode != detail_place.adcode):
        return None, "poi_identity_conflict"
    if (search_place.location and detail_place.location
            and not _coordinates_equal(search_place.location, detail_place.location)):
        return None, "poi_coordinate_conflict"

    sources = tuple(dict.fromkeys((*search_place.source_refs, *detail_place.source_refs)))
    field_sources = dict(detail_place.field_sources)
    for field, source_ref in search_place.field_sources.items():
        field_sources.setdefault(field, source_ref)
    updates = {
        "name": detail_place.name,
        "location": detail_place.location or search_place.location,
        "city": detail_place.city or search_place.city,
        "adcode": detail_place.adcode or search_place.adcode,
        "address": detail_place.address or search_place.address,
        "source_refs": sources,
        "field_sources": field_sources,
    }
    if updates["location"] is not None:
        field_sources.setdefault(
            "location",
            detail_result.source.tool_call_id if detail_place.location else search_place.field_sources.get("location", search_place.source_refs[0]),
        )
    if updates["city"] is not None:
        field_sources.setdefault("city", detail_result.source.tool_call_id if detail_place.city else search_place.source_refs[0])
    missing = tuple(
        item for item in (*search_place.missing_fields, *detail_place.missing_fields)
        if not (item.field == "location" and updates["location"] is not None)
        and not (item.field == "city" and updates["city"] is not None)
    )
    updates["missing_fields"] = tuple({(item.field, item.reason): item for item in missing}.values())
    return detail_place.model_copy(update=updates), None


def _verify_endpoint(kind, raw_query, normalized_query, city, records):
    if normalized_query is None:
        return EndpointVerification(kind=kind, user_query=raw_query, status="missing", reason="query_missing")
    record = records[normalized_query]
    search = record.search
    if search.status == "error":
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="error",
            reason=f"search_error:{search.error.category if search.error else 'invalid_response'}",
            search_result=search,
        )
    places = search.data.places if search.data is not None else ()
    if search.status == "empty" or not places:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="not_found", reason="search_empty",
            search_result=search,
        )

    exact, duplicate_conflicts = _exact_candidates(normalized_query, places)
    candidates, candidate_conflicts = _deduplicate_candidates(places)
    if not exact:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="ambiguous",
            reason="candidate_identity_conflict" if candidate_conflicts else "name_not_exact",
            candidates=candidates, search_result=search,
        )
    if len(exact) > 1:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="ambiguous", reason="multiple_exact_pois",
            candidates=exact, search_result=search,
        )
    candidate = exact[0]
    if candidate.poi_id in duplicate_conflicts:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="unverified", reason="duplicate_poi_conflict",
            candidates=exact, search_result=search,
        )

    state = _city_state(candidate, city)
    detail = record.details.get(candidate.poi_id)
    detail_results = (detail,) if detail is not None else ()
    if state == "city_mismatch":
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="unverified", reason="city_mismatch",
            candidates=exact, search_result=search, detail_results=detail_results,
        )
    if detail is not None:
        if detail.status == "error":
            return EndpointVerification(
                kind=kind, user_query=raw_query, status="error",
                reason=f"detail_error:{detail.error.category if detail.error else 'invalid_response'}",
                candidates=exact, search_result=search, detail_results=detail_results,
            )
        if detail.status == "empty":
            return EndpointVerification(
                kind=kind, user_query=raw_query, status="unverified", reason="detail_not_found",
                candidates=exact, search_result=search, detail_results=detail_results,
            )
        candidate, conflict = _place_with_detail(candidate, detail)
        if conflict:
            return EndpointVerification(
                kind=kind, user_query=raw_query, status="unverified", reason=conflict,
                candidates=exact, search_result=search, detail_results=detail_results,
            )
    if candidate.location is None:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="unverified", reason="coordinate_missing",
            candidates=exact, search_result=search, detail_results=detail_results,
        )
    state = _city_state(candidate, city)
    if state != "city_match":
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="unverified", reason=state,
            candidates=exact, search_result=search, detail_results=detail_results,
        )
    if not candidate.poi_id or not candidate.name or not candidate.source_refs:
        return EndpointVerification(
            kind=kind, user_query=raw_query, status="unverified", reason="identity_or_source_missing",
            candidates=exact, search_result=search, detail_results=detail_results,
        )
    candidate = _close_place_sources(candidate, search, detail)
    return EndpointVerification(
        kind=kind, user_query=raw_query, status="verified", reason="exact_name_city_and_coordinate",
        fact_id=candidate.fact_id, candidates=(candidate,), search_result=search,
        detail_results=detail_results,
    )


def _close_place_sources(
    place: PlaceFact,
    search_result: ToolResult[PlaceSearchData],
    detail_result: ToolResult[PlaceFact] | None,
) -> PlaceFact:
    """把核实身份、位置和同城判断所用的来源明确挂到对应字段。"""
    search_id = search_result.source.tool_call_id
    detail_id = detail_result.source.tool_call_id if detail_result is not None else None
    refs = tuple(dict.fromkeys((*place.source_refs, search_id, *((detail_id,) if detail_id else ()))))
    field_sources = dict(place.field_sources)
    field_sources["poi_id"] = search_id
    field_sources["name"] = search_id
    if place.location is not None:
        detail_has_location = detail_result is not None and detail_result.data is not None and detail_result.data.location is not None
        field_sources["location"] = detail_id if detail_has_location else search_id
    if place.city is not None:
        detail_has_city = detail_result is not None and detail_result.data is not None and detail_result.data.city is not None
        field_sources["city"] = detail_id if detail_has_city else search_id
    if place.adcode is not None:
        detail_has_adcode = detail_result is not None and detail_result.data is not None and detail_result.data.adcode is not None
        field_sources["adcode"] = detail_id if detail_has_adcode else search_id
    return place.model_copy(update={"source_refs": refs, "field_sources": field_sources})


def _reconcile_verified_endpoints(endpoints):
    verified_rows = [
        (kind, endpoint.candidates[0])
        for kind, endpoint in endpoints.items()
        if endpoint.status == "verified" and endpoint.fact_id and endpoint.candidates
    ]
    reasons: dict[str, str] = {}

    source_groups: dict[str, list[FactSource]] = {}
    for endpoint in endpoints.values():
        if endpoint.search_result is not None:
            source = endpoint.search_result.source
            source_groups.setdefault(source.tool_call_id, []).append(source)
        for result in endpoint.detail_results:
            source_groups.setdefault(result.source.tool_call_id, []).append(result.source)
    conflicting_source_ids = {
        source_id for source_id, sources in source_groups.items()
        if any(source != sources[0] for source in sources[1:])
    }
    for kind, fact in verified_rows:
        fact_source_ids = set(fact.source_refs) | set(fact.field_sources.values())
        if fact_source_ids & conflicting_source_ids:
            reasons[kind] = "source_identity_conflict"

    # Preserve every endpoint snapshot until both fact-ID and POI-ID conflicts are checked.
    fact_id_groups: dict[str, list[tuple[str, PlaceFact]]] = {}
    poi_id_groups: dict[str, list[tuple[str, PlaceFact]]] = {}
    for kind, fact in verified_rows:
        fact_id_groups.setdefault(fact.fact_id, []).append((kind, fact))
        poi_id_groups.setdefault(fact.poi_id, []).append((kind, fact))

    for fact_id, rows in fact_id_groups.items():
        facts = tuple(fact for _, fact in rows)
        if len({fact.poi_id for fact in facts}) > 1:
            conflict = "fact_id_identity_conflict"
        else:
            _, conflict = _merge_place_facts(facts)
        if conflict:
            for kind, _ in rows:
                reasons.setdefault(kind, conflict)

    merged_by_poi: dict[str, PlaceFact] = {}
    for poi_id, rows in poi_id_groups.items():
        merged, conflict = _merge_place_facts(tuple(fact for _, fact in rows))
        if conflict:
            for kind, _ in rows:
                reasons.setdefault(kind, conflict)
        elif merged is not None:
            merged_by_poi[poi_id] = merged

    updated = dict(endpoints)
    for kind, endpoint in endpoints.items():
        if endpoint.status == "verified" and kind in reasons:
            updated[kind] = endpoint.model_copy(update={
                "status": "unverified", "reason": reasons[kind], "fact_id": None,
            })
    for kind, endpoint in endpoints.items():
        if endpoint.status != "verified" or kind in reasons:
            continue
        merged = merged_by_poi[endpoint.candidates[0].poi_id]
        updated[kind] = endpoint.model_copy(update={"fact_id": merged.fact_id, "candidates": (merged,)})

    verified_poi_ids = tuple(dict.fromkeys(
        endpoint.candidates[0].poi_id
        for kind, endpoint in updated.items()
        if endpoint.status == "verified" and endpoint.candidates
    ))
    return updated, tuple(merged_by_poi[poi_id] for poi_id in verified_poi_ids)


def _fact_store(facts: tuple[PlaceFact, ...], endpoints) -> FactStore:
    available_sources = {}
    for endpoint in endpoints.values():
        if endpoint.search_result is not None:
            source = endpoint.search_result.source
            available_sources.setdefault(source.tool_call_id, source)
        for result in endpoint.detail_results:
            available_sources.setdefault(result.source.tool_call_id, result.source)
    sources: dict[str, FactSource] = {}
    for fact in facts:
        needed = set(fact.source_refs) | set(fact.field_sources.values())
        for source_id in needed:
            source = available_sources.get(source_id)
            if source is None:
                raise ValueError("核实地点引用了诊断结果中不存在的来源")
            _add_source_without_overwrite(sources, source)
    return FactStore(sources=tuple(sources.values()), places=facts)


def _add_source_without_overwrite(sources: dict[str, FactSource], source: FactSource) -> None:
    existing = sources.get(source.tool_call_id)
    if existing is not None and existing != source:
        raise ValueError("相同来源 ID 对应了冲突的来源记录")
    sources[source.tool_call_id] = source
