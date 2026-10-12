"""指定酒店的地图搜索与ID核实；不调用模型，不信任浏览器提供的坐标。"""
from threading import Event
from uuid import uuid4

from app.agents.execution import PlanningError
from app.agents.lodging_subtask import _lodging_category, _matches_original_query, _is_region_or_business_area
from app.services.endpoint_verification_service import _city_state
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import get_mcp_tool_executor
from app.services.observation_service import span
from app.models.planning_v4.facts import FactStore
from app.models.planning_v4.tasks import CandidateRef


class HotelSelectionService:
    def __init__(self, adapter=None):
        self.adapter = adapter

    def _adapter(self, cancel_event=None):
        if self.adapter is not None:
            return self.adapter
        executor = get_mcp_tool_executor()
        return AmapContractAdapter(tool_runner=lambda name, args: executor.run(
            name, args, timeout_seconds=25, cancel_event=cancel_event or Event(), priority="interactive",
        ))

    @staticmethod
    def eligible(place, city):
        return (place is not None and place.location is not None
                and _city_state(place, city) == "city_match"
                and _lodging_category(place) == "lodging")

    def search(self, keywords, city):
        result = self._adapter().search_text(keywords, city, citylimit=True, types="100000", offset=10, extensions="all")
        if result.status == "error":
            raise PlanningError("HOTEL_SEARCH_FAILED", "酒店查询暂时不可用，请重试", step="hotel_search")
        return [{"poi_id": p.poi_id, "name": p.name, "address": p.address,
                 "city": p.city, "location": p.location.model_dump(mode="json")}
                for p in (result.data.places if result.data else ()) if self.eligible(p, city)]

    def verify_stays(self, planning_input, cancel_event=None):
        from app.services.planning_lodging_policy import lodging_stays
        facts, candidates = FactStore(), ()
        verified = {}
        for stay in lodging_stays(planning_input):
            if not stay.specified:
                continue
            key = (stay.city, stay.lodging_poi_id, stay.lodging_query)
            if key not in verified:
                verified[key] = self.verify(planning_input.request.model_copy(update={
                    "city": stay.city, "lodging_poi_id": stay.lodging_poi_id,
                    "lodging_query": stay.lodging_query,
                }), cancel_event)
                new_facts, _ = verified[key]
                facts = FactStore(places=(*facts.places, *new_facts.places), sources=(*facts.sources, *new_facts.sources))
            _, selected = verified[key]
            candidates += tuple(item.model_copy(update={
                "candidate_id": f"candidate_{uuid4().hex}", "lodging_day_ids": stay.day_ids,
            }) for item in selected)
        return facts, candidates

    def verify(self, request, cancel_event=None):
        if not request.lodging_poi_id:
            return self._verify_text(request, cancel_event)

        with span("hotel.verify_selected", "tool", {"poi_id": request.lodging_poi_id}) as record:
            result = self._adapter(cancel_event).search_detail(request.lodging_poi_id)
            place = result.data
            if (result.status != "ok" or not self.eligible(place, request.city)
                    or place.poi_id != request.lodging_poi_id):
                record.output = {"status": "rejected", "error_code": "HOTEL_SELECTION_INVALID"}
                raise PlanningError("HOTEL_SELECTION_INVALID", "所选酒店无法核实，请重新搜索选择", step="hotel_verification")
            facts = FactStore(places=(place,), sources=(result.source,))
            candidate = CandidateRef(candidate_id=f"candidate_{uuid4().hex}", category="lodging",
                                     poi_id=place.poi_id, place_fact_id=place.fact_id,
                                     recommendation_reason="用户已选定的酒店，高德地点详情已核实")
            record.output = {"status": "verified", "place_fact_id": place.fact_id}
            return facts, (candidate,)

    def _verify_text(self, request, cancel_event=None):
        query = (request.lodging_query or "").strip()
        if not query:
            return FactStore(), ()
        with span("hotel.verify_text", "tool", {"specified": True}) as record:
            adapter = self._adapter(cancel_event)
            result = adapter.search_text(query, request.city, citylimit=True, offset=10, extensions="all")
            places = result.data.places if result.data else ()
            sources = (result.source,)
            matches = {place.poi_id: place for place in places
                       if _city_state(place, request.city) == "city_match"
                       and _matches_original_query(place, query)
                       and not _is_region_or_business_area(place)}
            place = next(iter(matches.values())) if result.status == "ok" and len(matches) == 1 else None
            if place is not None and place.location is None:
                detail = adapter.search_detail(place.poi_id)
                sources += (detail.source,)
                place = detail.data if (detail.status == "ok" and detail.data
                    and detail.data.poi_id == place.poi_id
                    and _city_state(detail.data, request.city) == "city_match"
                    and _matches_original_query(detail.data, query)
                    and not _is_region_or_business_area(detail.data)) else None
            if place is None or place.location is None:
                record.output = {"status": "unverified", "candidate_count": 0}
                # 指定原文仍在请求中；无法定位不允许替换成推荐酒店。
                return FactStore(places=tuple(places), sources=sources), ()
            facts = FactStore(places=(place,), sources=sources)
            candidate = CandidateRef(candidate_id=f"candidate_{uuid4().hex}", category="lodging",
                poi_id=place.poi_id, place_fact_id=place.fact_id,
                recommendation_reason="用户指定住处，原文及地图位置已核实")
            record.output = {"status": "verified", "candidate_count": 1}
            return facts, (candidate,)
