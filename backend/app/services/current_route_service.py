"""按 PlanDraft 当前顺序查询相邻路线，并用 F07 逐段推进时间。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time
import math
import unicodedata
from threading import Event, Lock
from typing import Awaitable, Callable, Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field

from app.models.planning_v4 import (
    FactStore,
    LegEndpoint,
    PlanDraft,
    PlanningInput,
    PlaceFact,
    RouteFact,
    RouteLeg,
    RouteModeResult,
    ToolResult,
)
from app.models.planning_v4.base import ContractModel
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.endpoint_verification_service import EndpointVerificationExecution
from app.services.mcp_tool_executor import MCPToolExecutor, get_mcp_tool_executor
from app.services.planning_v4_timing import PlanTimingResult, calculate_plan_timing
from app.services.route_fact_cache import (
    LoadedRouteFact,
    RouteFactCache,
    RouteFactCacheError,
    RouteQueryKey,
)
from app.services.observation_service import span


RouteMode = Literal["transit", "bicycling", "walking"]
TimeBasis = Literal["computed", "reference", "unspecified"]
_MODE_ORDER: tuple[RouteMode, ...] = ("transit", "bicycling", "walking")
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "市")
_TOOL_CONTRACT: dict[RouteMode, tuple[str, str]] = {
    "transit": ("maps_direction_transit_integrated_by_coordinates", "v3"),
    "bicycling": ("maps_bicycling_by_coordinates", "v4"),
    "walking": ("maps_direction_walking_by_coordinates", "v3"),
}
_ADAPTER_VERSION = "planning-v4-route-adapter-1"
_DEFAULT_TRANSIT_STRATEGY = 0


class CurrentRouteError(ValueError):
    """当前路线查询的安全错误；message 不包含供应商或原始响应文本。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _UncacheableRoute(Exception):
    """保留可用响应，但不把参数不完整或未发送的响应写入路线缓存。"""

    def __init__(self, fact: RouteFact | None, *, error_category: str | None = None):
        self.fact = fact
        self.error_category = error_category
        super().__init__("route response is not cacheable")


class CurrentRouteModeOutcome(ContractModel):
    leg_id: str | None = None
    mode: RouteMode
    status: Literal["available", "empty", "error", "unknown", "not_queried"]
    route_fact_id: str | None = None
    duration_s: float | None = Field(default=None, ge=0)
    geometry_status: Literal["complete", "partial", "missing"] | None = None
    error_category: Literal[
        "invalid_request", "auth", "rate_limit", "timeout", "network",
        "provider_error", "invalid_response",
    ] | None = None
    reason: str | None = None


class CurrentRouteLegOutcome(ContractModel):
    leg_id: str | None = None
    day_id: str
    from_reference_id: str
    to_reference_id: str
    status: Literal["queried", "same_place", "unknown", "not_queried"]
    requested_departure_at: datetime | None = None
    selected_mode: RouteMode | None = None
    selected_option_id: str | None = None
    selection_source: Literal["backend_fastest_available", "user", "same_place"] | None = None
    reason: str | None = None
    modes: tuple[CurrentRouteModeOutcome, ...] = ()


class CurrentRouteExecution(ContractModel):
    plan_id: str
    request_id: str
    based_on_revision: int = Field(ge=1)
    status: Literal["complete", "partial"]
    draft: PlanDraft
    fact_store: FactStore
    timing: PlanTimingResult
    leg_results: tuple[CurrentRouteLegOutcome, ...] = ()


RouteContextLoader = Callable[[RouteQueryKey, PlaceFact, PlaceFact], Awaitable[LoadedRouteFact]]


def _route_place_key(place: PlaceFact) -> tuple[str, str, float, float] | None:
    if place.location is None:
        return None
    return (
        place.poi_id,
        place.location.coordinate_system,
        float(place.location.lng),
        float(place.location.lat),
    )


class PlanRouteContext:
    """跨生成与编辑复用的路线缓存及地点事实索引。"""

    def __init__(
        self,
        planning_input: PlanningInput,
        plan_id: str,
        loader: RouteContextLoader,
        *,
        cancel_event: Event | None = None,
    ):
        if not isinstance(planning_input, PlanningInput):
            raise CurrentRouteError("input_type_invalid")
        if not isinstance(plan_id, str) or not plan_id:
            raise CurrentRouteError("plan_id_invalid")
        self.plan_id = plan_id
        self._planning_input_snapshot = planning_input.model_dump(mode="json")
        self._loader = loader
        self.cancel_event = cancel_event or Event()
        self._lock = Lock()
        self._places: dict[tuple[str, str, float, float], PlaceFact] = {}
        self._alternative_sources: dict[tuple[str, str, float, float], set[str]] = {}
        self.backfill_lock = asyncio.Lock()
        self.route_cache = RouteFactCache(plan_id, self._load)

    @property
    def alternative_source_count(self) -> int:
        with self._lock:
            return len(self._alternative_sources)

    def assert_binding(self, planning_input: PlanningInput, draft: PlanDraft) -> None:
        if (
            draft.plan_id != self.plan_id
            or planning_input.model_dump(mode="json") != self._planning_input_snapshot
        ):
            raise CurrentRouteError("route_context_binding_mismatch")

    def register_facts(self, facts: FactStore) -> None:
        """保留首个已核实快照，并记录同一POI+坐标的替代来源。"""
        with self._lock:
            for place in facts.places:
                key = _route_place_key(place)
                if key is None:
                    continue
                previous = self._places.get(key)
                if previous is None:
                    self._places[key] = place.model_copy(deep=True)
                elif previous.model_dump(mode="json") != place.model_dump(mode="json"):
                    self._alternative_sources.setdefault(key, set()).update(
                        (previous.fact_id, place.fact_id),
                    )

    def _resolve(self, poi_id: str, coordinate: tuple[float, float]) -> PlaceFact | None:
        # RouteQueryKey stores (lng, lat); registry keys include coordinate system.
        key = (poi_id, "GCJ-02", coordinate[0], coordinate[1])
        with self._lock:
            place = self._places.get(key)
            return place.model_copy(deep=True) if place is not None else None

    async def _load(self, key: RouteQueryKey) -> LoadedRouteFact:
        origin = self._resolve(key.origin_poi_id, key.origin)
        destination = self._resolve(key.destination_poi_id, key.destination)
        if origin is None or destination is None:
            raise _UncacheableRoute(None, error_category="invalid_response")
        return await self._loader(key, origin, destination)

    async def aclose(self) -> None:
        self.cancel_event.set()
        await self.route_cache.aclose()


@dataclass(frozen=True, slots=True)
class _Edge:
    day_id: str
    from_reference_id: str
    to_reference_id: str
    leg: RouteLeg | None
    origin: PlaceFact | None
    destination: PlaceFact | None
    reason: str | None = None
    same_place: bool = False


@dataclass(frozen=True, slots=True)
class _ModeResult:
    outcome: CurrentRouteModeOutcome
    route_fact: RouteFact | None = None
    mode_result: RouteModeResult | None = None


def current_leg_id(
    plan_id: str,
    revision: int,
    day_id: str,
    from_endpoint: LegEndpoint,
    to_endpoint: LegEndpoint,
) -> str:
    """为一条计划内有向路段生成稳定标识，保留两个端点的活动身份。"""
    material = "|".join((
        plan_id, str(revision), day_id,
        from_endpoint.kind, from_endpoint.reference_id, from_endpoint.poi_id,
        to_endpoint.kind, to_endpoint.reference_id, to_endpoint.poi_id,
    ))
    return f"leg:{uuid5(NAMESPACE_URL, material)}"


def _normalized_user_query(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _normalized_city(value: str | None) -> str:
    normalized = _normalized_user_query(value)
    for suffix in _CITY_SUFFIXES:
        normalized_suffix = _normalized_user_query(suffix)
        if normalized.endswith(normalized_suffix):
            return normalized[:-len(normalized_suffix)]
    return normalized


def _same_place_state(origin: PlaceFact, destination: PlaceFact) -> Literal["same", "different", "conflict"]:
    if origin.poi_id != destination.poi_id:
        return "different"
    if (
        origin.location is None
        or destination.location is None
        or origin.location != destination.location
        or _normalized_user_query(origin.name) != _normalized_user_query(destination.name)
        or (
            origin.city is not None
            and destination.city is not None
            and _normalized_city(origin.city) != _normalized_city(destination.city)
        )
    ):
        return "conflict"
    return "same"


def _model_payload(value) -> dict:
    return value.model_dump(mode="json")


def _merge_fact_stores(*stores: FactStore) -> FactStore:
    """按ID闭合合并；相同ID出现不同payload时拒绝整个合并。"""
    groups: dict[str, dict[str, object]] = {
        "sources": {}, "places": {}, "weather": {}, "routes": {},
    }
    id_fields = {"sources": "tool_call_id", "places": "fact_id", "weather": "fact_id", "routes": "fact_id"}
    for store in stores:
        for name in groups:
            for item in getattr(store, name):
                key = getattr(item, id_fields[name])
                current = groups[name].get(key)
                if current is not None and _model_payload(current) != _model_payload(item):
                    raise CurrentRouteError("fact_id_payload_conflict")
                groups[name][key] = item

    merged = FactStore(
        sources=tuple(groups["sources"].values()),
        places=tuple(groups["places"].values()),
        weather=tuple(groups["weather"].values()),
        routes=tuple(groups["routes"].values()),
    )
    source_ids = {source.tool_call_id for source in merged.sources}
    for place in merged.places:
        if not place.source_refs or not set(place.source_refs) <= source_ids:
            raise CurrentRouteError("place_source_not_closed")
        if not set(place.field_sources.values()) <= source_ids:
            raise CurrentRouteError("place_field_source_not_closed")
    for fact in merged.weather:
        if fact.source.tool_call_id not in source_ids:
            raise CurrentRouteError("weather_source_not_closed")
    for fact in merged.routes:
        if fact.source.tool_call_id not in source_ids:
            raise CurrentRouteError("route_source_not_closed")
    return merged


def _route_store(routes: list[RouteFact] | tuple[RouteFact, ...]) -> FactStore:
    """构造来源闭合的路线事实集；相同ID的payload冲突时拒绝合并。"""
    by_route: dict[str, RouteFact] = {}
    by_source: dict[str, object] = {}
    for route in routes:
        previous = by_route.get(route.fact_id)
        if previous is not None and _model_payload(previous) != _model_payload(route):
            raise CurrentRouteError("fact_id_payload_conflict")
        by_route[route.fact_id] = route
        source = route.source
        previous_source = by_source.get(source.tool_call_id)
        if previous_source is not None and _model_payload(previous_source) != _model_payload(source):
            raise CurrentRouteError("source_id_payload_conflict")
        by_source[source.tool_call_id] = source
    return FactStore(sources=tuple(by_source.values()), routes=tuple(by_route.values()))


def _resolve_departure_fact(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    endpoint_execution: EndpointVerificationExecution | None,
    verified_departure_fact_id: str | None,
    current_context_revision: int | None,
) -> tuple[FactStore, str | None]:
    stores = [facts]
    endpoint_id: str | None = None
    if endpoint_execution is not None:
        request = planning_input.request
        if (
            endpoint_execution.request_id != request.request_id
            or _normalized_city(endpoint_execution.city) != _normalized_city(request.city)
            or endpoint_execution.run_id != draft.source_run_id
        ):
            raise CurrentRouteError("endpoint_execution_context_mismatch")
        if (
            type(current_context_revision) is not int
            or current_context_revision < 1
            or endpoint_execution.context_revision != current_context_revision
        ):
            raise CurrentRouteError("endpoint_execution_stale_context")
        if endpoint_execution.base_revision is not None and endpoint_execution.base_revision != draft.revision:
            raise CurrentRouteError("endpoint_execution_stale_base_revision")
        if _normalized_user_query(endpoint_execution.departure.user_query) != _normalized_user_query(
            request.departure_place_query,
        ):
            raise CurrentRouteError("endpoint_execution_departure_query_mismatch")
        stores.append(endpoint_execution.fact_store)
        if endpoint_execution.departure.status == "verified":
            endpoint_id = endpoint_execution.departure.fact_id
        if verified_departure_fact_id is not None and verified_departure_fact_id != endpoint_id:
            raise CurrentRouteError("departure_fact_id_conflict")
    else:
        endpoint_id = verified_departure_fact_id

    merged = _merge_fact_stores(*stores)
    if endpoint_id is None:
        return merged, None
    request = planning_input.request
    if not request.departure_place_query or not request.departure_place_query.strip():
        raise CurrentRouteError("departure_fact_without_user_query")
    fact = next((item for item in merged.places if item.fact_id == endpoint_id), None)
    if fact is None or fact.location is None or not fact.city:
        raise CurrentRouteError("verified_departure_fact_missing_or_incomplete")
    if _normalized_city(fact.city) != _normalized_city(request.city):
        raise CurrentRouteError("verified_departure_city_mismatch")
    return merged, endpoint_id


def _place_by_fact_id(facts: FactStore) -> dict[str, PlaceFact]:
    return {place.fact_id: place for place in facts.places}


def _place_for_endpoint(endpoint: LegEndpoint, day, facts_by_id: dict[str, PlaceFact], departure_fact_id: str | None) -> PlaceFact | None:
    if endpoint.kind == "activity":
        activity = next((item for item in day.activities if item.activity_id == endpoint.reference_id), None)
        if activity is None or activity.kind == "free_time" or activity.place_fact_id is None:
            return None
        fact_id = activity.place_fact_id
    elif endpoint.kind == "lodging":
        lodging = day.lodging
        if lodging is None or lodging.place_fact_id is None:
            return None
        if endpoint.reference_id != f"lodging:{day.day_id}":
            return None
        fact_id = lodging.place_fact_id
    elif endpoint.kind == "departure":
        fact_id = departure_fact_id or ""
        if endpoint.reference_id != fact_id:
            return None
    else:
        return None
    place = facts_by_id.get(fact_id)
    if place is None or place.poi_id != endpoint.poi_id:
        return None
    return place


def _pending_leg(
    leg_id: str,
    revision: int,
    day_id: str,
    origin: LegEndpoint,
    destination: LegEndpoint,
) -> RouteLeg:
    return RouteLeg(
        leg_id=leg_id,
        plan_revision=revision,
        day_id=day_id,
        from_endpoint=origin,
        to_endpoint=destination,
        mode_results=tuple(RouteModeResult(mode=mode, status="pending") for mode in _MODE_ORDER),
    )


def _existing_legs_by_signature(draft: PlanDraft) -> dict[tuple[str, str, str, str, str], RouteLeg]:
    result: dict[tuple[str, str, str, str, str], RouteLeg] = {}
    for leg in draft.legs:
        signature = (
            leg.day_id,
            leg.from_endpoint.kind, leg.from_endpoint.reference_id,
            leg.to_endpoint.kind, leg.to_endpoint.reference_id,
        )
        if signature in result:
            raise CurrentRouteError("duplicate_existing_leg_signature")
        result[signature] = leg
    return result


def _make_edge(
    *,
    draft: PlanDraft,
    day,
    from_reference_id: str,
    origin: PlaceFact | None,
    origin_endpoint: LegEndpoint | None,
    destination_endpoint: LegEndpoint,
    destination: PlaceFact | None,
    existing: dict[tuple[str, str, str, str, str], RouteLeg],
) -> _Edge:
    if origin_endpoint is None:
        return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, None,
                     origin, destination, "origin_endpoint_missing")
    signature = (
        day.day_id,
        origin_endpoint.kind, origin_endpoint.reference_id,
        destination_endpoint.kind, destination_endpoint.reference_id,
    )
    previous = existing.get(signature)
    leg_id = previous.leg_id if previous else current_leg_id(
        draft.plan_id, draft.revision, day.day_id, origin_endpoint, destination_endpoint,
    )
    if origin is None or destination is None:
        reason = "origin_fact_missing" if origin is None else "destination_fact_missing"
        leg = _pending_leg(leg_id, draft.revision, day.day_id, origin_endpoint, destination_endpoint)
        return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, leg,
                     origin, destination, reason)
    if origin.location is None or destination.location is None:
        reason = "origin_coordinates_missing" if origin.location is None else "destination_coordinates_missing"
        leg = _pending_leg(leg_id, draft.revision, day.day_id, origin_endpoint, destination_endpoint)
        return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, leg,
                     origin, destination, reason)

    same_place_state = _same_place_state(origin, destination)
    if same_place_state == "same":
        leg = RouteLeg(
            leg_id=leg_id,
            plan_revision=draft.revision,
            day_id=day.day_id,
            from_endpoint=origin_endpoint,
            to_endpoint=destination_endpoint,
            selection_source="same_place",
        )
        return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, leg,
                     origin, destination, same_place=True)
    if same_place_state == "conflict":
        leg = _pending_leg(leg_id, draft.revision, day.day_id, origin_endpoint, destination_endpoint)
        return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, leg,
                     origin, destination, "same_poi_fact_conflict")

    leg = _pending_leg(leg_id, draft.revision, day.day_id, origin_endpoint, destination_endpoint)
    if previous is not None and previous.selection_source == "user":
        # 保留用户明确选择的方式；只有供应商返回该方式的有效方案后才绑定新 option_id。
        leg = leg.model_copy(update={"selection_source": "user"})
    return _Edge(day.day_id, from_reference_id, destination_endpoint.reference_id, leg,
                 origin, destination)


def _build_edges(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    departure_fact_id: str | None,
) -> tuple[dict[str, list[_Edge]], list[RouteLeg]]:
    facts_by_id = _place_by_fact_id(facts)
    existing = _existing_legs_by_signature(draft)
    by_day: dict[str, list[_Edge]] = {}
    for day in draft.days:
        day_edges: list[_Edge] = []
        lodging = day.lodging
        previous_endpoint: LegEndpoint | None = None
        previous_place: PlaceFact | None = None
        previous_reference = f"lodging:{day.day_id}"
        if lodging is not None and lodging.poi_id is not None:
            previous_endpoint = LegEndpoint(
                kind="lodging", reference_id=f"lodging:{day.day_id}", poi_id=lodging.poi_id,
            )
            previous_place = _place_for_endpoint(previous_endpoint, day, facts_by_id, departure_fact_id)

        for activity in day.activities:
            if activity.kind == "free_time":
                continue
            destination_endpoint = LegEndpoint(
                kind="activity", reference_id=activity.activity_id, poi_id=activity.poi_id or "",
            )
            destination = _place_for_endpoint(destination_endpoint, day, facts_by_id, departure_fact_id)
            if previous_endpoint is None:
                day_edges.append(_Edge(
                    day.day_id, previous_reference, activity.activity_id, None,
                    previous_place, destination, "lodging_endpoint_missing",
                ))
            else:
                day_edges.append(_make_edge(
                    draft=draft, day=day, from_reference_id=previous_reference,
                    origin=previous_place, origin_endpoint=previous_endpoint,
                    destination_endpoint=destination_endpoint, destination=destination,
                    existing=existing,
                ))
            previous_endpoint = destination_endpoint
            previous_place = destination
            previous_reference = activity.activity_id

        local_departure_date = planning_input.request.departure_at.astimezone(
            ZoneInfo(planning_input.request.timezone),
        ).date()
        if day.day_date == local_departure_date and departure_fact_id is not None:
            terminal = facts_by_id.get(departure_fact_id)
            terminal_endpoint = (
                LegEndpoint(kind="departure", reference_id=departure_fact_id, poi_id=terminal.poi_id)
                if terminal is not None else None
            )
            if terminal_endpoint is not None:
                if previous_endpoint is None:
                    # 离开日没有 POI 活动时，F07 允许以酒店作为返程起点。
                    day_edges.append(_Edge(
                        day.day_id, previous_reference, departure_fact_id, None,
                        previous_place, terminal, "return_origin_endpoint_missing",
                    ))
                else:
                    day_edges.append(_make_edge(
                        draft=draft, day=day, from_reference_id=previous_reference,
                        origin=previous_place, origin_endpoint=previous_endpoint,
                        destination_endpoint=terminal_endpoint, destination=terminal,
                        existing=existing,
                    ))
        by_day[day.day_id] = day_edges

    legs = [edge.leg for edges in by_day.values() for edge in edges if edge.leg is not None]
    untouched = [leg for leg in draft.legs if leg.day_id not in by_day]
    return by_day, untouched + legs


def _effective_http_params(
    mode: RouteMode,
    origin: PlaceFact,
    destination: PlaceFact,
    *,
    city: str,
    transit_departure_at: datetime | None,
    timezone_name: str,
) -> dict[str, str]:
    assert origin.location is not None and destination.location is not None
    params = {
        "origin": f"{origin.location.lng},{origin.location.lat}",
        "destination": f"{destination.location.lng},{destination.location.lat}",
    }
    if mode == "transit":
        if transit_departure_at is None:
            raise CurrentRouteError("transit_departure_missing")
        local = transit_departure_at.astimezone(ZoneInfo(timezone_name))
        params.update({
            "city": city,
            "cityd": city,
            "extensions": "base",
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M"),
            "strategy": str(_DEFAULT_TRANSIT_STRATEGY),
        })
    return params


def _route_query_key(
    planning_input: PlanningInput,
    draft: PlanDraft,
    mode: RouteMode,
    origin: PlaceFact,
    destination: PlaceFact,
    *,
    transit_departure_at: datetime | None,
) -> RouteQueryKey:
    tool_name, api_version = _TOOL_CONTRACT[mode]
    assert origin.location is not None and destination.location is not None
    city = planning_input.request.city
    params = _effective_http_params(
        mode, origin, destination, city=city,
        transit_departure_at=transit_departure_at if mode == "transit" else None,
        timezone_name=planning_input.request.timezone,
    )
    return RouteQueryKey.from_effective_params(
        plan_id=draft.plan_id,
        provider="amap",
        api_version=api_version,
        adapter_version=_ADAPTER_VERSION,
        tool_name=tool_name,
        coordinate_system=origin.location.coordinate_system,
        origin_poi_id=origin.poi_id,
        origin=origin.location,
        destination_poi_id=destination.poi_id,
        destination=destination.location,
        city=city if mode == "transit" else None,
        destination_city=city if mode == "transit" else None,
        mode=mode,
        effective_params=params,
        requested_departure_at=transit_departure_at if mode == "transit" else None,
        timezone_name=planning_input.request.timezone,
    )


def _fact_matches_request(
    fact: RouteFact,
    mode: RouteMode,
    origin: PlaceFact,
    destination: PlaceFact,
) -> bool:
    tool_name, api_version = _TOOL_CONTRACT[mode]
    return bool(
        origin.location is not None and destination.location is not None
        and fact.mode == mode
        and fact.origin_poi_id == origin.poi_id
        and fact.destination_poi_id == destination.poi_id
        and fact.origin == origin.location
        and fact.destination == destination.location
        and fact.source.provider == "amap"
        and fact.source.tool_name == tool_name
        and fact.source.api_version == api_version
    )


def _params_match_or_time_was_omitted(
    fact: RouteFact,
    expected: dict[str, str],
    mode: RouteMode,
) -> tuple[bool, bool]:
    actual = fact.source.effective_params
    if actual == expected:
        return True, True
    if (
        mode == "transit"
        and fact.time_verification == "not_sent"
        and actual == {key: value for key, value in expected.items() if key not in {"date", "time"}}
    ):
        return True, False
    if fact.status == "error" and not actual:
        # MCP 调用在供应商确认实际 HTTP 参数前失败。
        return True, False
    return False, False


def _safe_error_category(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "network"
    if isinstance(exc, RouteFactCacheError):
        return "invalid_response"
    return "provider_error"


def _duration_candidates(mode_facts: dict[RouteMode, RouteFact]) -> list[tuple[float, int, int, RouteMode, str]]:
    candidates: list[tuple[float, int, int, RouteMode, str]] = []
    for mode_index, mode in enumerate(_MODE_ORDER):
        fact = mode_facts.get(mode)
        if fact is None or fact.status != "available":
            continue
        for option_index, option in enumerate(fact.options):
            if option.duration_s is None or not math.isfinite(option.duration_s) or option.duration_s < 0:
                continue
            candidates.append((option.duration_s, mode_index, option_index, mode, option.option_id))
    return candidates


def _select_leg(
    original_leg: RouteLeg,
    current_leg: RouteLeg,
    mode_facts: dict[RouteMode, RouteFact],
) -> RouteLeg:
    candidates = _duration_candidates(mode_facts)
    if not candidates:
        return current_leg.model_copy(update={
            "selected_mode": None,
            "selected_option_id": None,
            "selection_source": None,
            "time_basis": "unspecified",
        })
    chosen = None
    if original_leg.selection_source == "user" and original_leg.selected_mode is not None:
        user_candidates = [item for item in candidates if item[3] == original_leg.selected_mode]
        if user_candidates:
            chosen = min(user_candidates, key=lambda item: (item[0], item[2]))
            selection_source = "user"
    if chosen is None:
        chosen = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        selection_source = "backend_fastest_available"
    _, _, _, mode, option_id = chosen
    fact = mode_facts[mode]
    time_basis: TimeBasis = "unspecified"
    if mode == "transit":
        if fact.time_verification in {"sent_not_echoed", "provider_echoed"}:
            requested = current_leg.requested_departure_at
            time_basis = "computed" if requested is not None and current_leg.time_basis == "computed" else "reference"
    return current_leg.model_copy(update={
        "selected_mode": mode,
        "selected_option_id": option_id,
        "selection_source": selection_source,
        "time_basis": time_basis,
    })


def _route_fact_outcome(leg_id: str | None, mode: RouteMode, fact: RouteFact) -> _ModeResult:
    if fact.status == "available":
        known_durations = [option.duration_s for option in fact.options if option.duration_s is not None]
        shortest = min(known_durations) if known_durations else None
        shortest_option = next((option for option in fact.options if option.duration_s == shortest), None) if shortest is not None else None
        outcome = CurrentRouteModeOutcome(
            leg_id=leg_id,
            mode=mode,
            status="available",
            route_fact_id=fact.fact_id,
            duration_s=shortest,
            geometry_status=shortest_option.geometry_status if shortest_option else None,
            reason="duration_unknown" if shortest is None else None,
        )
        return _ModeResult(outcome, fact, RouteModeResult(
            mode=mode, status="available", route_fact_id=fact.fact_id,
            option_ids=tuple(option.option_id for option in fact.options),
        ))
    if fact.status == "empty":
        return _ModeResult(
            CurrentRouteModeOutcome(leg_id=leg_id, mode=mode, status="empty", route_fact_id=fact.fact_id,
                                    reason="no_route"),
            fact,
            RouteModeResult(mode=mode, status="empty", route_fact_id=fact.fact_id),
        )
    return _ModeResult(
        CurrentRouteModeOutcome(leg_id=leg_id, mode=mode, status="error", route_fact_id=fact.fact_id,
                                error_category=fact.error_category or "provider_error",
                                reason="route_query_error"),
        fact,
        RouteModeResult(mode=mode, status="error", route_fact_id=fact.fact_id,
                        error_category=fact.error_category or "provider_error"),
    )


class CurrentRouteService:
    """单次当前路段查询；路线查询实际并发由共享四worker MCP执行器限制。"""

    def __init__(
        self,
        *,
        adapter: AmapContractAdapter | None = None,
        tool_executor: MCPToolExecutor | None = None,
        route_cache: RouteFactCache | None = None,
        tool_timeout_seconds: float | None = None,
    ):
        if tool_timeout_seconds is None:
            from app.config import settings

            tool_timeout_seconds = settings.planner_tool_timeout_seconds
        if (
            isinstance(tool_timeout_seconds, bool)
            or not isinstance(tool_timeout_seconds, (int, float))
            or not math.isfinite(tool_timeout_seconds)
            or tool_timeout_seconds <= 0
        ):
            raise ValueError("tool_timeout_seconds 必须是有限正数")
        self.adapter = adapter
        self.tool_executor = tool_executor
        self.route_cache = route_cache
        self.tool_timeout_seconds = float(tool_timeout_seconds)

    def create_route_context(
        self, planning_input: PlanningInput, plan_id: str,
    ) -> PlanRouteContext:
        """创建可由F08当前查询与F11b后台查询共同持有的plan上下文。"""
        cancel_event = Event()

        async def load(key: RouteQueryKey, origin: PlaceFact, destination: PlaceFact) -> LoadedRouteFact:
            raw_strategy = key.params_dict().get("strategy")
            strategy = int(raw_strategy) if raw_strategy is not None else None
            fact, cacheable, valid = await self._load_route_fact(
                planning_input,
                key.mode,
                origin,
                destination,
                transit_departure_at=key.requested_departure_at,
                strategy=strategy,
                cancel_event=cancel_event,
            )
            if fact is None or not valid:
                raise _UncacheableRoute(fact, error_category="invalid_response")
            if not cacheable:
                raise _UncacheableRoute(fact)
            return LoadedRouteFact(adapter_version=key.adapter_version, fact=fact)

        return PlanRouteContext(
            planning_input, plan_id, load, cancel_event=cancel_event,
        )

    async def run_async(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        facts: FactStore,
        *,
        endpoint_execution: EndpointVerificationExecution | None = None,
        verified_departure_fact_id: str | None = None,
        current_context_revision: int | None = None,
        only_edge: tuple[str, str, str] | None = None,
        route_context: PlanRouteContext | None = None,
    ) -> CurrentRouteExecution:
        _validate_plan_binding(planning_input, draft)
        merged_facts, departure_fact_id = _resolve_departure_fact(
            planning_input, draft, facts, endpoint_execution,
            verified_departure_fact_id, current_context_revision,
        )
        try:
            calculate_plan_timing(
                planning_input, draft, merged_facts,
                verified_departure_fact_id=departure_fact_id,
            )
        except (TypeError, ValueError) as exc:
            raise CurrentRouteError("input_contract_invalid") from exc

        edges_by_day, generated_legs = _build_edges(
            planning_input, draft, merged_facts, departure_fact_id,
        )
        if only_edge is not None:
            matches = [edge for edges in edges_by_day.values() for edge in edges if (
                edge.day_id == only_edge[0]
                and edge.from_reference_id == only_edge[1]
                and edge.to_reference_id == only_edge[2]
            )]
            if len(matches) != 1:
                raise CurrentRouteError("requested_live_edge_not_unique")

        if route_context is not None and (self.route_cache is not None):
            raise CurrentRouteError("route_context_and_route_cache_conflict")
        owns_route_context = route_context is None and self.route_cache is None
        if route_context is None and self.route_cache is None:
            route_context = self.create_route_context(planning_input, draft.plan_id)
        if route_context is not None:
            route_context.assert_binding(planning_input, draft)
            route_context.register_facts(merged_facts)
            cache = route_context.route_cache
        else:
            cache = self.route_cache
        if cache.plan_id != draft.plan_id:
            raise CurrentRouteError("route_cache_plan_mismatch")

        # 每日worker只更新自己的路段，避免影响其他日期的临时时间；共享执行器仍把MCP并发限制为4。
        skeleton = draft.model_copy(update={"legs": tuple(generated_legs)}, deep=True)
        original_legs = {leg.leg_id: leg for leg in draft.legs}
        day_results: list[tuple[list[RouteLeg], list[RouteFact], list[CurrentRouteLegOutcome]]] = []

        async def run_day(day_id: str, edges: list[_Edge]):
            local_legs = [edge.leg for edge in edges if edge.leg is not None]
            local_facts: list[RouteFact] = []
            local_results: list[CurrentRouteLegOutcome] = []
            for edge in edges:
                if only_edge is not None and (
                    edge.day_id, edge.from_reference_id, edge.to_reference_id
                ) != only_edge:
                    local_results.append(_not_queried_outcome(edge, "outside_selected_live_edge"))
                    continue
                if edge.leg is None:
                    local_results.append(_unknown_outcome(edge, edge.reason or "route_endpoint_missing"))
                    continue
                if edge.same_place:
                    local_results.append(CurrentRouteLegOutcome(
                        leg_id=edge.leg.leg_id, day_id=edge.day_id,
                        from_reference_id=edge.from_reference_id, to_reference_id=edge.to_reference_id,
                        status="same_place", selected_mode=None,
                        selection_source="same_place", reason=None,
                    ))
                    continue
                if edge.reason is not None:
                    pending_modes = tuple(CurrentRouteModeOutcome(
                        leg_id=edge.leg.leg_id, mode=mode, status="unknown", reason=edge.reason,
                    ) for mode in _MODE_ORDER)
                    local_results.append(CurrentRouteLegOutcome(
                        leg_id=edge.leg.leg_id, day_id=edge.day_id,
                        from_reference_id=edge.from_reference_id, to_reference_id=edge.to_reference_id,
                        status="unknown", reason=edge.reason,
                        modes=pending_modes,
                    ))
                    continue
                assert edge.origin is not None and edge.destination is not None
                original_leg = original_legs.get(edge.leg.leg_id, edge.leg)
                timing = self._timing_for_day(
                    planning_input, skeleton, local_legs,
                    merged_facts, local_facts, departure_fact_id,
                )
                requested_departure = _departure_for_edge(timing, edge)
                query_basis: TimeBasis = "computed" if requested_departure is not None else "reference"
                if requested_departure is None:
                    local_day = edge_day_date(skeleton, edge.day_id)
                    local_zone = ZoneInfo(planning_input.request.timezone)
                    requested_departure = datetime.combine(local_day, time(9, 0), tzinfo=local_zone)
                provisional_leg = edge.leg.model_copy(update={
                    "requested_departure_at": requested_departure,
                    "time_basis": query_basis,
                })
                mode_results = await asyncio.gather(*(
                    self._query_mode(
                        planning_input, draft, cache, edge, mode,
                        transit_departure_at=requested_departure,
                        query_basis=query_basis,
                    )
                    for mode in _MODE_ORDER
                ))
                mode_facts = {
                    result.outcome.mode: result.route_fact
                    for result in mode_results if result.route_fact is not None
                }
                local_facts.extend(mode_facts.values())
                provider_modes = tuple(
                    result.mode_result for result in mode_results
                    if result.mode_result is not None
                )
                queried_leg = provisional_leg.model_copy(update={"mode_results": provider_modes})
                selected_leg = _select_leg(original_leg, queried_leg, mode_facts)
                _replace_leg(local_legs, selected_leg)
                local_results.append(CurrentRouteLegOutcome(
                    leg_id=edge.leg.leg_id, day_id=edge.day_id,
                    from_reference_id=edge.from_reference_id, to_reference_id=edge.to_reference_id,
                    status="queried", requested_departure_at=requested_departure,
                    selected_mode=selected_leg.selected_mode,
                    selected_option_id=selected_leg.selected_option_id,
                    selection_source=selected_leg.selection_source,
                    reason=("user_selected_mode_unavailable"
                            if original_leg.selection_source == "user"
                            and original_leg.selected_mode is not None
                            and selected_leg.selected_mode != original_leg.selected_mode
                            else None),
                    modes=tuple(result.outcome for result in mode_results),
                ))
            return local_legs, local_facts, local_results

        day_tasks = [
            asyncio.create_task(run_day(day_id, edges))
            for day_id, edges in edges_by_day.items()
        ]
        try:
            day_results = list(await asyncio.gather(*day_tasks))
        except BaseException:
            if owns_route_context and route_context is not None:
                route_context.cancel_event.set()
            for task in day_tasks:
                task.cancel()
            await asyncio.gather(*day_tasks, return_exceptions=True)
            raise
        finally:
            if owns_route_context and route_context is not None:
                await route_context.aclose()

        updated_by_id: dict[str, RouteLeg] = {}
        added_routes: list[RouteFact] = []
        leg_outcomes: list[CurrentRouteLegOutcome] = []
        for local_legs, local_facts, local_results in day_results:
            updated_by_id.update((leg.leg_id, leg) for leg in local_legs)
            added_routes.extend(local_facts)
            leg_outcomes.extend(local_results)
        final_legs = tuple(
            updated_by_id.get(leg.leg_id, leg)
            for leg in generated_legs
        )
        final_draft = draft.model_copy(update={"legs": final_legs}, deep=True)
        final_facts = _merge_fact_stores(
            merged_facts,
            _route_store(added_routes),
        )
        try:
            timing = calculate_plan_timing(
                planning_input, final_draft, final_facts,
                verified_departure_fact_id=departure_fact_id,
            )
        except (TypeError, ValueError) as exc:
            raise CurrentRouteError("route_result_contract_invalid") from exc
        partial = any(item.status in {"unknown", "not_queried"} for item in leg_outcomes) or any(
            mode.status in {"empty", "error", "unknown", "not_queried"}
            for item in leg_outcomes for mode in item.modes
        )
        return CurrentRouteExecution(
            plan_id=draft.plan_id,
            request_id=draft.request_id,
            based_on_revision=draft.revision,
            status="partial" if partial else "complete",
            draft=final_draft,
            fact_store=final_facts,
            timing=timing,
            leg_results=tuple(leg_outcomes),
        )

    def _timing_for_day(
        self,
        planning_input: PlanningInput,
        skeleton: PlanDraft,
        local_legs: list[RouteLeg],
        base_facts: FactStore,
        local_facts: list[RouteFact],
        departure_fact_id: str | None,
    ) -> PlanTimingResult:
        current_ids = {leg.leg_id for leg in local_legs}
        legs = [leg for leg in skeleton.legs if leg.leg_id not in current_ids] + list(local_legs)
        temp_draft = skeleton.model_copy(update={"legs": tuple(legs)}, deep=True)
        temp_facts = _merge_fact_stores(base_facts, _route_store(local_facts))
        return calculate_plan_timing(
            planning_input, temp_draft, temp_facts,
            verified_departure_fact_id=departure_fact_id,
        )

    async def _query_mode(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        cache: RouteFactCache,
        edge: _Edge,
        mode: RouteMode,
        *,
        transit_departure_at: datetime,
        query_basis: TimeBasis,
    ) -> _ModeResult:
        assert edge.leg is not None and edge.origin is not None and edge.destination is not None
        leg_id = edge.leg.leg_id
        key = _route_query_key(
            planning_input, draft, mode, edge.origin, edge.destination,
            transit_departure_at=transit_departure_at if mode == "transit" else None,
        )
        with span("route.current_mode", "tool", {
            "plan_id": draft.plan_id,
            "based_on_revision": draft.revision,
            "leg_id": leg_id,
            "day_id": edge.day_id,
            "mode": mode,
            "time_basis": query_basis if mode == "transit" else "unspecified",
        }) as record:
            try:
                fact = await cache.get_or_load(key) if cache is not None else None
                if fact is None:
                    raise RouteFactCacheError("cache returned no route fact")
                record.output = {"status": fact.status, "cache": "hit_or_loaded"}
                return _route_fact_outcome(leg_id, mode, fact)
            except _UncacheableRoute as exc:
                fact = exc.fact
                category = exc.error_category
                if fact is not None and _fact_matches_request(fact, mode, edge.origin, edge.destination):
                    if category is None and fact.status == "error":
                        category = fact.error_category or "provider_error"
                    if category is None:
                        record.output = {"status": fact.status, "cache": "bypassed"}
                        return _route_fact_outcome(leg_id, mode, fact)
                record.output = {"status": "error", "cache": "rejected"}
                return _query_error(leg_id, mode, category or "invalid_response")
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                category = _safe_error_category(exc)
                record.output = {"status": "error", "error_category": category}
                return _query_error(leg_id, mode, category)

    async def _load_route_fact(
        self,
        planning_input: PlanningInput,
        mode: RouteMode,
        origin: PlaceFact,
        destination: PlaceFact,
        *,
        transit_departure_at: datetime | None,
        strategy: int | None = None,
        cancel_event: Event | None = None,
    ) -> tuple[RouteFact | None, bool, bool]:
        tool_executor = self.tool_executor or get_mcp_tool_executor()
        if self.adapter is None:
            def tool_runner(tool_name: str, arguments: dict) -> object:
                return tool_executor.run(
                    tool_name,
                    arguments,
                    timeout_seconds=self.tool_timeout_seconds,
                    cancel_event=cancel_event,
                )
            adapter = AmapContractAdapter(tool_runner=tool_runner)
        else:
            adapter = self.adapter
        departure = transit_departure_at if mode == "transit" else None
        result: ToolResult[RouteFact] = await asyncio.to_thread(
            adapter.plan_route,
            origin,
            destination,
            mode,
            city=planning_input.request.city,
            destination_city=planning_input.request.city,
            departure_at=departure,
            strategy=(strategy if strategy is not None else _DEFAULT_TRANSIT_STRATEGY) if mode == "transit" else None,
            timezone_name=planning_input.request.timezone,
        )
        fact = result.data
        if fact is None or not _fact_matches_request(fact, mode, origin, destination):
            return None, False, False
        expected = _effective_http_params(
            mode,
            origin,
            destination,
            city=planning_input.request.city,
            transit_departure_at=departure,
            timezone_name=planning_input.request.timezone,
        )
        params_ok, exact = _params_match_or_time_was_omitted(fact, expected, mode)
        if not params_ok:
            return fact, False, False
        return fact, exact, True


def _validate_plan_binding(planning_input: PlanningInput, draft: PlanDraft) -> None:
    if not isinstance(planning_input, PlanningInput) or not isinstance(draft, PlanDraft):
        raise CurrentRouteError("input_type_invalid")
    if draft.request_id != planning_input.request.request_id:
        raise CurrentRouteError("request_id_mismatch")
    if draft.rule_version != planning_input.rules.rule_version:
        raise CurrentRouteError("rule_version_mismatch")
    if draft.revision < 1:
        raise CurrentRouteError("revision_invalid")


def _unknown_outcome(edge: _Edge, reason: str) -> CurrentRouteLegOutcome:
    return CurrentRouteLegOutcome(
        leg_id=edge.leg.leg_id if edge.leg else None,
        day_id=edge.day_id,
        from_reference_id=edge.from_reference_id,
        to_reference_id=edge.to_reference_id,
        status="unknown",
        reason=reason,
        modes=tuple(CurrentRouteModeOutcome(
            leg_id=edge.leg.leg_id if edge.leg else None,
            mode=mode,
            status="unknown",
            reason=reason,
        ) for mode in _MODE_ORDER),
    )


def _not_queried_outcome(edge: _Edge, reason: str) -> CurrentRouteLegOutcome:
    return CurrentRouteLegOutcome(
        leg_id=edge.leg.leg_id if edge.leg else None,
        day_id=edge.day_id,
        from_reference_id=edge.from_reference_id,
        to_reference_id=edge.to_reference_id,
        status="not_queried",
        reason=reason,
        modes=tuple(CurrentRouteModeOutcome(
            leg_id=edge.leg.leg_id if edge.leg else None,
            mode=mode,
            status="not_queried",
            reason=reason,
        ) for mode in _MODE_ORDER),
    )


def _query_error(
    leg_id: str | None,
    mode: RouteMode,
    category: str,
) -> _ModeResult:
    allowed = {"invalid_request", "auth", "rate_limit", "timeout", "network", "provider_error", "invalid_response"}
    safe_category = category if category in allowed else "provider_error"
    outcome = CurrentRouteModeOutcome(
        leg_id=leg_id,
        mode=mode,
        status="error",
        error_category=safe_category,
        reason="route_query_error",
    )
    return _ModeResult(outcome, None, RouteModeResult(
        mode=mode, status="error", error_category=safe_category,
    ))


def _replace_leg(legs: list[RouteLeg], updated: RouteLeg) -> None:
    for index, leg in enumerate(legs):
        if leg.leg_id == updated.leg_id:
            legs[index] = updated
            return
    legs.append(updated)


def edge_day_date(draft: PlanDraft, day_id: str):
    return next(day.day_date for day in draft.days if day.day_id == day_id)


def _departure_for_edge(
    timing: PlanTimingResult,
    edge: _Edge,
) -> datetime | None:
    day_timing = next((item for item in timing.days if item.day_id == edge.day_id), None)
    if day_timing is None:
        return None
    if edge.leg is not None and edge.leg.to_endpoint.kind == "activity":
        activity = next((item for item in day_timing.activities if item.activity_id == edge.to_reference_id), None)
        return activity.depart_at if activity else None
    if edge.leg is not None and edge.leg.to_endpoint.kind == "departure":
        if day_timing.activities:
            # 在当日最后一项活动结束时查询，包括末尾 free_time。
            # latest_activity_end_at 是返程截止边界，不是实际出发时刻。
            return day_timing.activities[-1].end_at
        return day_timing.day_start_at
    return None


async def query_current_routes(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    *,
    endpoint_execution: EndpointVerificationExecution | None = None,
    verified_departure_fact_id: str | None = None,
    current_context_revision: int | None = None,
    adapter: AmapContractAdapter | None = None,
    tool_executor: MCPToolExecutor | None = None,
    route_cache: RouteFactCache | None = None,
    tool_timeout_seconds: float | None = None,
    only_edge: tuple[str, str, str] | None = None,
    route_context: PlanRouteContext | None = None,
) -> CurrentRouteExecution:
    """查询酒店起点、相邻地点及已核实返程终点；不会重排活动或改写输入。"""
    service = CurrentRouteService(
        adapter=adapter,
        tool_executor=tool_executor,
        route_cache=route_cache,
        tool_timeout_seconds=tool_timeout_seconds,
    )
    return await service.run_async(
        planning_input,
        draft,
        facts,
        endpoint_execution=endpoint_execution,
        verified_departure_fact_id=verified_departure_fact_id,
        current_context_revision=current_context_revision,
        only_edge=only_edge,
        route_context=route_context,
    )
