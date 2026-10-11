"""用户发起的来源化目的地搜索；不调用模型或修改预览快照。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from threading import Event
from typing import Literal
from uuid import uuid4

from app.agents.attraction_subtask import _category_status
from app.agents.dining_subtask import _dining_category
from app.models.planning_v4.destination_search import (
    DestinationBlockReason,
    DestinationCategory,
    DestinationSearchCandidate,
    DestinationSearchRequest,
    DestinationSearchResponse,
    DestinationSourceSummary,
)
from app.models.planning_v4.facts import FactSource, PlaceFact, ToolError
from app.models.planning_v4.tasks import CandidateRef
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.destination_candidate_repository import (
    DestinationCandidateRepository,
    StoredDestinationCandidate,
)
from app.services.mcp_tool_executor import MCPToolExecutor, ToolPriority, get_mcp_tool_executor
from app.services.observation_service import span


_SPACE = re.compile(r"\s+")
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "市")


@dataclass(frozen=True, slots=True)
class DestinationCandidateBundle:
    """由服务端 token 解析出的候选闭包，不能由客户端构造。"""

    task_id: str
    plan_id: str
    search_id: str
    candidate: CandidateRef
    place: PlaceFact
    sources: tuple[FactSource, ...]


class DestinationSearchService:
    """查询当前行程城市，保存短期事实闭包并提供受计划绑定的选择解析。"""

    def __init__(
        self,
        repository: DestinationCandidateRepository,
        *,
        adapter: AmapContractAdapter | None = None,
        tool_executor: MCPToolExecutor | None = None,
        observation_service=None,
        tool_timeout_seconds: float | None = None,
    ):
        self.repository = repository
        self.adapter = adapter
        self.tool_executor = tool_executor
        self.observation_service = observation_service
        if tool_timeout_seconds is None:
            from app.config import settings

            tool_timeout_seconds = settings.planner_tool_timeout_seconds
        if isinstance(tool_timeout_seconds, bool) or not isinstance(tool_timeout_seconds, (int, float)) or tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds 必须大于0")
        self.tool_timeout_seconds = float(tool_timeout_seconds)

    async def search_async(
        self,
        *,
        task_id: str,
        plan_id: str,
        access_token: str,
        city: str,
        request: DestinationSearchRequest,
    ) -> DestinationSearchResponse:
        """搜索城市内候选；取消时阻止还未开始的 MCP 调用。"""
        self.repository.validate_owner_binding(
            task_id=task_id, plan_id=plan_id, access_token=access_token,
        )
        with self._task_observation(task_id):
            return await self._search_async(
                task_id=task_id,
                plan_id=plan_id,
                access_token=access_token,
                city=city,
                request=request,
            )

    async def _search_async(
        self,
        *,
        task_id: str,
        plan_id: str,
        access_token: str,
        city: str,
        request: DestinationSearchRequest,
    ) -> DestinationSearchResponse:
        if not city.strip():
            raise ValueError("city 不能为空")
        keywords = DestinationSearchRequest.normalized_keywords(request.keywords)
        if not keywords:
            raise ValueError("keywords 不能为空")
        search_id = uuid4().hex
        cancel_event = Event()
        safe_input = {
            "task_id": task_id,
            "plan_id": plan_id,
            "city": city,
            "category": request.category,
            "page": request.page,
            "keyword_length": len(keywords),
        }
        with span("destination.search", "planning", safe_input) as record:
            adapter = self.adapter or self._make_adapter(cancel_event)
            try:
                result = await asyncio.to_thread(
                    adapter.search_text,
                    keywords,
                    city,
                    citylimit=True,
                    page=request.page,
                    offset=20,
                    extensions="all",
                )
            except asyncio.CancelledError:
                cancel_event.set()
                raise
            except Exception:
                record.output = {"status": "error", "code": "DESTINATION_SEARCH_FAILED"}
                return DestinationSearchResponse(
                    search_id=search_id,
                    city=city,
                    keywords=keywords,
                    category=request.category,
                    page=request.page,
                    status="error",
                    error=ToolError(category="provider_error", message="地图搜索失败", retryable=True),
                )

            data = result.data
            rows = tuple(data.places) if data is not None and result.status == "ok" else ()
            if result.status != "ok" and data is not None and data.places:
                record.output = {"status": "error", "code": "DESTINATION_SEARCH_RESPONSE_INVALID"}
                return DestinationSearchResponse(
                    search_id=search_id,
                    city=city,
                    keywords=keywords,
                    category=request.category,
                    page=request.page,
                    status="error",
                    error=ToolError(category="invalid_response", message="地图搜索返回结构无效"),
                )
            screened, omitted = _screen_places(rows, category=request.category, city=city, source=result.source)
            tokens: tuple[str, ...] = ()
            selectable = tuple((place, reason) for place, reason in screened if reason is None)
            if selectable:
                stored_rows = tuple(place for place, _ in selectable)
                tokens = self.repository.store_search(
                    task_id=task_id,
                    plan_id=plan_id,
                    access_token=access_token,
                    search_id=search_id,
                    category=request.category,
                    places=stored_rows,
                    source=result.source,
                )
            tokens_by_poi = {place.poi_id: token for (place, _), token in zip(selectable, tokens, strict=True)}
            candidates = tuple(
                _public_candidate(
                    place,
                    result.source,
                    token=tokens_by_poi.get(place.poi_id),
                    block_reason=reason,
                )
                for place, reason in screened
            )
            record.output = {
                "status": result.status,
                "candidate_count": len(candidates),
                "omitted_count": omitted,
                "tool_name": result.source.tool_name,
            }
            status = result.status
            if result.status == "ok" and not candidates:
                status = "empty"
            return DestinationSearchResponse(
                search_id=search_id,
                city=city,
                keywords=keywords,
                category=request.category,
                page=request.page,
                status=status,
                provider_count=data.provider_count if data is not None else None,
                candidates=candidates,
                omitted_count=omitted,
                error=result.error,
            )

    def resolve_candidate(
        self,
        candidate_token: str,
        *,
        task_id: str,
        plan_id: str,
        access_token: str,
    ) -> DestinationCandidateBundle:
        """解析当前计划有效的候选；返回闭包供选择接口进行一次原子编辑。"""
        self.repository.validate_owner_binding(
            task_id=task_id, plan_id=plan_id, access_token=access_token,
        )
        with self._task_observation(task_id):
            with span("destination.resolve_candidate", "validation", {
                "task_id": task_id,
                "plan_id": plan_id,
            }) as record:
                stored: StoredDestinationCandidate = self.repository.get_candidate(
                    candidate_token,
                    task_id=task_id,
                    plan_id=plan_id,
                    access_token=access_token,
                )
                if stored.place.location is None or not stored.place.source_refs:
                    raise ValueError("目的地候选缺少可用坐标或来源")
                source_by_id = {source.tool_call_id: source for source in stored.sources}
                source_refs = set(stored.place.source_refs) | set(stored.place.field_sources.values())
                if not source_refs or not source_refs.issubset(source_by_id):
                    raise ValueError("目的地候选事实来源未闭合")
                fact_payload = json.dumps(
                    stored.place.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                candidate_digest = hashlib.sha256(
                    f"{plan_id}|{stored.category}|{fact_payload}".encode("utf-8")
                ).hexdigest()[:32]
                candidate = CandidateRef(
                    candidate_id=f"candidate:user:{candidate_digest}",
                    category=stored.category,
                    poi_id=stored.place.poi_id,
                    place_fact_id=stored.place.fact_id,
                    # 用户可把同一来源候选加入多个合法时段；slot 仍由 AddActivity 单独校验。
                    slot_ids=(),
                    requirement_ids=(),
                    recommendation_reason="用户从目的地搜索结果中选择。",
                )
                record.output = {"status": "resolved", "category": stored.category}
                return DestinationCandidateBundle(
                    task_id=stored.task_id,
                    plan_id=stored.plan_id,
                    search_id=stored.search_id,
                    candidate=candidate,
                    place=stored.place,
                    sources=stored.sources,
                )

    def _task_observation(self, task_id: str):
        return _best_effort_task_observation(self.observation_service, task_id)

    def _make_adapter(self, cancel_event: Event) -> AmapContractAdapter:
        executor = self.tool_executor or get_mcp_tool_executor()

        def run_tool(tool_name: str, arguments: dict):
            return executor.run(
                tool_name,
                arguments,
                timeout_seconds=self.tool_timeout_seconds,
                cancel_event=cancel_event,
                priority="interactive",
            )

        return AmapContractAdapter(tool_runner=run_tool)


def _screen_places(
    places: tuple[PlaceFact, ...],
    *,
    category: DestinationCategory,
    city: str,
    source: FactSource,
) -> tuple[tuple[tuple[PlaceFact, DestinationBlockReason | None], ...], int]:
    """保留可展示的来源化结果；不适用的候选没有选择 token。"""
    source_id = source.tool_call_id
    grouped: dict[str, PlaceFact] = {}
    conflicted: set[str] = set()
    omitted = 0
    for place in places:
        previous = grouped.get(place.poi_id)
        if previous is not None:
            if previous != place:
                conflicted.add(place.poi_id)
            continue
        grouped[place.poi_id] = place
    output: list[tuple[PlaceFact, DestinationBlockReason | None]] = []
    for poi_id, place in grouped.items():
        if poi_id in conflicted:
            omitted += 1
            continue
        refs = set(place.source_refs) | set(place.field_sources.values())
        if not refs or not refs.issubset({source_id}):
            output.append((place, "source_unclosed"))
            continue
        category_state = _category_state(place, category)
        if category_state == "excluded":
            omitted += 1
            continue
        if category_state == "unknown":
            output.append((place, "category_unverified"))
            continue
        if not place.city or not _city_matches(city, place.city):
            output.append((place, "city_mismatch"))
            continue
        if place.location is None:
            output.append((place, "location_unavailable"))
            continue
        output.append((place, None))
    return tuple(output), omitted


def _category_state(place: PlaceFact, category: DestinationCategory) -> Literal["match", "excluded", "unknown"]:
    if category == "meal":
        state = _dining_category(place)
        return {"dining": "match", "excluded": "excluded", "unknown": "unknown"}[state]
    state = _category_status(place)
    return {"sightseeing": "match", "excluded": "excluded", "unknown": "unknown"}[state]


def _city_matches(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    left, right = _normalize_city(expected), _normalize_city(actual)
    return left == right


def _normalize_city(value: str) -> str:
    normalized = _SPACE.sub("", unicodedata.normalize("NFKC", value)).casefold()
    for suffix in _CITY_SUFFIXES:
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
    return normalized


def _source_summary(source: FactSource) -> DestinationSourceSummary:
    return DestinationSourceSummary(
        provider=source.provider,
        tool_name=source.tool_name,
        api_version=source.api_version,
        queried_at=source.queried_at,
        effective_params=source.effective_params,
    )


def _public_candidate(
    place: PlaceFact,
    source: FactSource,
    *,
    token: str | None,
    block_reason: DestinationBlockReason | None,
) -> DestinationSearchCandidate:
    return DestinationSearchCandidate(
        candidate_token=token,
        name=place.name,
        address=place.address,
        city=place.city,
        location=place.location,
        type_name=place.type_name,
        cost=place.cost,
        opening_hours_raw=place.opening_hours_raw,
        photos=place.photos,
        missing_fields=place.missing_fields,
        source=_source_summary(source),
        selectable=block_reason is None,
        selection_block_reason=block_reason,
    )


@contextmanager
def _best_effort_task_observation(observation_service, task_id: str):
    """观测上下文失败时继续搜索或候选解析，不改变业务结果。"""
    if observation_service is None:
        yield
        return
    try:
        scope = observation_service.task(task_id)
        scope.__enter__()
    except Exception:
        yield
        return
    try:
        yield
    except BaseException as exc:
        try:
            scope.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            pass
