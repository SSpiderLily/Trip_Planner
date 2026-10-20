"""按已确认住宿范围直接核实指定住处；不调用模型、不推荐替代酒店。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from threading import Event
from typing import Literal
from uuid import uuid4

from app.agents.execution import PlanningError
from app.agents.lodging_subtask import _matches_original_query, _is_region_or_business_area
from app.models.planning_v4 import PlanningInput, CandidateRef, FactStore
from app.services.endpoint_verification_service import _city_state
from app.services.hotel_selection_service import HotelSelectionService
from app.services.planning_lodging_policy import lodging_stays
from app.services.amap_contract_adapter import AmapContractAdapter


@dataclass(frozen=True)
class SpecifiedLodgingStay:
    day_ids: tuple[str, ...]
    city: str
    lodging_query: str | None
    lodging_poi_id: str | None
    status: Literal["verified", "ambiguous", "unverified", "not_specified"]
    candidate_id: str | None = None


@dataclass(frozen=True)
class SpecifiedLodgingResult:
    planning_input: PlanningInput = field(repr=False)
    fact_store: FactStore = field(repr=False)
    candidates: tuple[CandidateRef, ...]
    stays: tuple[SpecifiedLodgingStay, ...]


class SpecifiedLodgingService:
    """可注入地图适配器或核实服务；查询复用仅限当前run，结果按连续范围绑定。"""
    def __init__(self, *, adapter=None, tool_executor=None, hotel_service=None, emit=None):
        self.adapter, self.tool_executor, self.hotel_service = adapter, tool_executor, hotel_service
        self.emit = emit or (lambda event, data: None)

    @staticmethod
    def _cancelled(event):
        if event is not None and event.is_set():
            raise PlanningError("SPECIFIED_LODGING_CANCELLED", "指定住宿核实已取消", step="specified_lodging")

    def _service(self, cancel_event):
        if self.hotel_service is not None:
            return self.hotel_service
        adapter = self.adapter
        if adapter is None and self.tool_executor is not None:
            from app.config import get_settings
            adapter = AmapContractAdapter(tool_runner=lambda name, args: self.tool_executor.run(
                name, args, timeout_seconds=get_settings().planner_tool_timeout_seconds,
                cancel_event=cancel_event, priority="interactive"))
        return HotelSelectionService(adapter)

    @staticmethod
    def _validate_selected(stay, facts, selected):
        sources = {source.tool_call_id for source in facts.sources}
        places = {place.fact_id: place for place in facts.places}
        if len(selected) > 1:
            raise ValueError("指定住宿结果不能选择多个候选")
        if selected and not stay.lodging_poi_id:
            matches = {place.poi_id for place in facts.places if _city_state(place, stay.city) == "city_match"
                and not _is_region_or_business_area(place) and _matches_original_query(place, stay.lodging_query)}
            if matches != {selected[0].poi_id}:
                raise ValueError("原文住处尚未唯一匹配")
        for candidate in selected:
            place = places.get(candidate.place_fact_id)
            if (candidate.category != "lodging" or candidate.area_hint_id is not None or place is None
                    or candidate.poi_id != place.poi_id or place.location is None
                    or _city_state(place, stay.city) != "city_match" or _is_region_or_business_area(place)
                    or not set((*place.source_refs, *place.field_sources.values())).issubset(sources)
                    or (stay.lodging_poi_id and candidate.poi_id != stay.lodging_poi_id)
                    or (not stay.lodging_poi_id and not _matches_original_query(place, stay.lodging_query))):
                raise ValueError("指定住宿结果与用户选择或事实来源不一致")

    def run(self, planning_input: PlanningInput, *, cancel_event: Event | None = None):
        self._cancelled(cancel_event)
        if not isinstance(planning_input, PlanningInput):
            raise PlanningError("SPECIFIED_LODGING_INPUT_INVALID", "指定住宿输入无效", step="specified_lodging")
        try:
            stays = lodging_stays(planning_input)
        except ValueError:
            raise PlanningError("SPECIFIED_LODGING_INPUT_INVALID", "指定住宿范围或原文无效", step="specified_lodging") from None
        # 没有指定住处时不初始化地图执行器；未指定范围交给住宿子智能体。
        service = self._service(cancel_event) if any(stay.specified for stay in stays) else None
        cache, sources, places, candidates, outcomes = {}, {}, {}, [], []
        for stay in stays:
            self._cancelled(cancel_event)
            selected = ()
            if not stay.specified:
                status = "not_specified"
            else:
                key = (stay.city, "poi", stay.lodging_poi_id) if stay.lodging_poi_id else (stay.city, "text", stay.lodging_query)
                reused = key in cache
                if not reused:
                    self.emit("lodging.specified_start", {"request_id": planning_input.request.request_id,
                        "day_ids": list(stay.day_ids), "city": stay.city,
                        "lodging_poi_id": stay.lodging_poi_id, "lodging_query": stay.lodging_query})
                    request = planning_input.request.model_copy(update={"city": stay.city,
                        "lodging_poi_id": stay.lodging_poi_id, "lodging_query": stay.lodging_query})
                    try:
                        value = service.verify(request, cancel_event)
                    except PlanningError:
                        raise
                    except Exception:
                        raise PlanningError("SPECIFIED_LODGING_QUERY_FAILED", "指定住宿查询失败", step="specified_lodging") from None
                    self._cancelled(cancel_event)
                    cache[key] = value
                facts, selected = cache[key]
                try:
                    self._validate_selected(stay, facts, selected)
                    for source in facts.sources:
                        if source.tool_call_id in sources and sources[source.tool_call_id] != source:
                            raise ValueError("住宿来源冲突")
                        sources[source.tool_call_id] = source
                    for place in facts.places:
                        if place.fact_id in places and places[place.fact_id] != place:
                            raise ValueError("住宿事实冲突")
                        places[place.fact_id] = place
                except (ValueError, AttributeError, TypeError):
                    raise PlanningError("SPECIFIED_LODGING_RESULT_INVALID", "指定住宿结果无法验证", step="specified_lodging") from None
                matches = {place.poi_id for place in facts.places if _city_state(place, stay.city) == "city_match"
                    and not _is_region_or_business_area(place) and _matches_original_query(place, stay.lodging_query or "")}
                status = "verified" if selected else "ambiguous" if len(matches) > 1 else "unverified"
                self.emit("lodging.specified_checked", {"day_ids": list(stay.day_ids), "status": status,
                    "reused_query": reused, "sources": [s.model_dump(mode="json") for s in facts.sources],
                    "places": [p.model_dump(mode="json", exclude={"photos"}) for p in facts.places]})
            bound = tuple(candidate.model_copy(update={"candidate_id": f"candidate_{uuid4().hex}",
                "lodging_day_ids": stay.day_ids}) for candidate in selected)
            candidates.extend(bound)
            outcomes.append(SpecifiedLodgingStay(stay.day_ids, stay.city, stay.lodging_query, stay.lodging_poi_id,
                status, bound[0].candidate_id if bound else None))
        self._cancelled(cancel_event)
        result = SpecifiedLodgingResult(planning_input, FactStore(sources=tuple(sources.values()), places=tuple(places.values())),
            tuple(candidates), tuple(outcomes))
        self.emit("lodging.specified_result", {"request_id": planning_input.request.request_id,
            "stays": [asdict(stay) for stay in outcomes], "candidates": [c.model_dump(mode="json") for c in candidates]})
        return result
