"""到离地点单模块及原生链路模拟验证；不依赖其他测试文件或真实服务。"""
from dataclasses import replace
from datetime import datetime
import json
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

from app.agents.execution import PlanningError
from app.agents.native_multiagent_collection import NativeMultiagentCollection
from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_v4 import TripRequest, RequirementSummary, FactStore
from app.models.planning_v4.input import LodgingChange
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.endpoint_verification_service import EndpointVerificationContext, EndpointVerificationService
from app.services.llm_service import NativeLLMCompletion
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_endpoint_service import NativeEndpointService, validate_native_endpoint_result
from app.services.native_tool_loop import NativeToolLoopError
from app.services.native_weather_service import NativeWeatherService
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.specified_lodging_service import SpecifiedLodgingResult


def request(**updates):
    return TripRequest(**{**dict(request_id="endpoint-fixture", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-22T15:00:00+08:00"),
        lodging_query="测试酒店", lodging_poi_id="hotel", arrival_place_query="上海虹桥站", departure_place_query="上海站"), **updates})


def planning(req=None, changes=()):
    req = req or request()
    return build_planning_input(PlanningInputBuildRequest(request=req, requirements=RequirementSummary(
        requirements_id="endpoint-summary", request_id=req.request_id, lodging_changes=changes),
        rules=default_planning_v4_rules()))


def poi(identifier="station", name="上海虹桥站", **updates):
    return {"id": identifier, "name": name, "cityname": "上海市", "location": "121.327,31.20", **updates}


def raw(rows=(), **updates):
    return {"result_status": "ok", "pois": list(rows), **updates}


def tool_call(name, args, identifier):
    return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [{"id": identifier,
        "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, None, "tool_calls")


def final():
    return NativeLLMCompletion({"role": "assistant", "content": "已处理；未知保持未知"}, None, "stop")


def batch(*responses):
    return NativeLLMCompletion({"role": "assistant", "content": None,
        "tool_calls": [c for response in responses for c in response.message["tool_calls"]]}, None, "tool_calls")


class Model:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def invoke_native(self, messages, *, tools, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        response = self.responses.pop(0)
        return response(messages) if callable(response) else response


def user_payload(messages):
    return json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))


class NativeEndpointTests(unittest.TestCase):
    def service(self, payload):
        mapper = Mock(side_effect=payload if callable(payload) else lambda *_: payload)
        return NativeEndpointService(adapter=AmapContractAdapter(mapper)), mapper

    def test_missing_original_queries_do_not_initialize_map_or_infer_station_from_remarks(self):
        source = planning(request(arrival_place_query=None, departure_place_query="   ", remarks="我坐火车来"))
        before = source.model_dump(mode="json")
        with patch("app.services.endpoint_verification_service.get_mcp_tool_executor") as executor:
            result = NativeEndpointService().run(source, run_id="task")
        executor.assert_not_called()
        self.assertEqual(result.arrival.status, "missing")
        self.assertEqual(result.departure.status, "missing")
        self.assertEqual(result.tool_payload()["arrival"]["availability"], "unknown")
        self.assertEqual(result.fact_store, FactStore())
        self.assertEqual(source.model_dump(mode="json"), before)

    def test_unique_exact_endpoints_keep_original_queries_and_close_fact_sources(self):
        service, maps = self.service(lambda _, args: raw([poi(args["keywords"], args["keywords"])]))
        source = planning(request(arrival_place_query=" 上海虹桥站 "))
        result = service.run(source, run_id="formal-task")
        self.assertEqual([result.arrival.status, result.departure.status], ["verified", "verified"])
        self.assertEqual(result.arrival.user_query, " 上海虹桥站 ")
        self.assertEqual(len(result.fact_store.places), 2)
        self.assertEqual(maps.call_count, 2)
        sources = {s.tool_call_id for s in result.fact_store.sources}
        for place in result.fact_store.places:
            self.assertTrue(set(place.source_refs) <= sources)
            self.assertTrue(set(place.field_sources.values()) <= sources)
            self.assertTrue({"poi_id", "name", "city", "location"} <= set(place.field_sources))
        self.assertEqual(result.tool_payload()["departure"]["day_id"], source.days[-1].day_id)
        self.assertIsNone(source.boundary_facts.day_ready_at)

    def test_identical_queries_reuse_one_search_and_one_fact_for_two_kinds(self):
        service, maps = self.service(raw([poi()]))
        result = service.run(planning(request(departure_place_query=" 上海虹桥站 ")), run_id="task")
        self.assertEqual(maps.call_count, 1)
        self.assertEqual(result.arrival.fact_id, result.departure.fact_id)
        self.assertEqual(len(result.fact_store.places), 1)
        self.assertEqual(len(result.fact_store.sources), 1)

    def test_same_name_multiple_pois_preserves_ambiguity_without_place_reference(self):
        service, maps = self.service(raw([poi("one"), poi("two", location="121.33,31.21")]))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.status, "ambiguous")
        self.assertEqual(result.arrival.reason, "multiple_exact_pois")
        self.assertEqual({c["poi_id"] for c in result.tool_payload()["arrival"]["candidates"]}, {"one", "two"})
        self.assertIsNone(result.tool_payload()["arrival"]["place"])
        self.assertIsNone(result.arrival.fact_id)
        self.assertEqual(result.fact_store, FactStore())
        self.assertEqual(maps.call_count, 1)

    def test_abbreviation_stays_ambiguous_but_punctuation_can_normalize(self):
        service, maps = self.service(raw([poi()]))
        exact = service.run(planning(request(arrival_place_query="上海，虹桥站", departure_place_query=None)), run_id="one")
        fuzzy = service.run(planning(request(arrival_place_query="虹桥站", departure_place_query=None)), run_id="two")
        self.assertEqual(exact.arrival.status, "verified")
        self.assertEqual(fuzzy.arrival.reason, "name_not_exact")
        self.assertEqual(fuzzy.fact_store, FactStore())
        self.assertEqual(maps.call_count, 2)

    def test_unique_missing_coordinate_queries_only_known_poi_detail(self):
        service, maps = self.service(lambda name, args: raw([poi(location=None)]) if name == "maps_text_search" else raw([poi()]))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual([c.args[0] for c in maps.call_args_list], ["maps_text_search", "maps_search_detail"])
        self.assertEqual(maps.call_args_list[1].args[1], {"id": "station", "extensions": "all"})
        self.assertEqual(len(result.fact_store.sources), 2)
        self.assertEqual(result.fact_store.places[0].field_sources["location"], result.arrival.detail_results[0].source.tool_call_id)

    def test_wrong_detail_identity_or_city_cannot_replace_original_place(self):
        for update in ({"id": "other"}, {"name": "别的站"}, {"cityname": "北京市"}):
            with self.subTest(update=update):
                service, maps = self.service(lambda name, args: raw([poi(location=None)]) if name == "maps_text_search" else raw([poi(**update)]))
                result = service.run(planning(request(departure_place_query=None)), run_id="task")
                self.assertEqual(result.arrival.status, "unverified")
                self.assertEqual(result.fact_store, FactStore())
                self.assertEqual(maps.call_count, 2)

    def test_wrong_search_city_does_not_query_detail_or_verify(self):
        service, maps = self.service(raw([poi(cityname="北京市", location=None)]))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.reason, "city_mismatch")
        self.assertEqual(maps.call_count, 1)
        self.assertEqual(result.fact_store, FactStore())

    def test_missing_or_invalid_coordinates_remain_unknown_after_detail(self):
        for location in (None, "invalid", "999,999"):
            with self.subTest(location=location):
                service, maps = self.service(raw([poi(location=location)]))
                result = service.run(planning(request(departure_place_query=None)), run_id="task")
                self.assertEqual(result.arrival.reason, "coordinate_missing")
                self.assertEqual(result.fact_store, FactStore())
                self.assertEqual(maps.call_count, 2)

    def test_missing_city_stays_unverified_after_detail(self):
        service, maps = self.service(raw([poi(cityname=None)]))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.reason, "city_unverified")
        self.assertEqual(result.fact_store, FactStore())
        self.assertEqual(maps.call_count, 2)

    def test_duplicate_poi_conflict_is_not_last_write_wins(self):
        service, _ = self.service(raw([poi(), poi(location="121.90,31.80")]))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.reason, "duplicate_poi_conflict")
        self.assertEqual(result.fact_store, FactStore())

    def test_empty_search_is_not_found_not_query_error(self):
        service, maps = self.service(raw(result_status="empty"))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.status, "not_found")
        self.assertEqual(result.arrival.reason, "search_empty")
        self.assertEqual(result.tool_payload()["arrival"]["availability"], "unknown")
        self.assertEqual(maps.call_count, 1)

    def test_search_failure_categories_are_safe_and_do_not_produce_facts(self):
        for category in ("timeout", "network", "rate_limit", "auth", "provider_error", "invalid_response"):
            with self.subTest(category=category):
                service, _ = self.service(raw(result_status="error", result_error={"category": category, "message": "secret-response"}))
                trace = NativeChainTrace(); service.emit = trace.emit
                result = service.run(planning(request(departure_place_query=None)), run_id="task")
                self.assertEqual(result.arrival.status, "error")
                self.assertEqual(result.arrival.reason, "search_error:"+category)
                self.assertEqual(result.fact_store, FactStore())
                self.assertNotIn("secret-response", json.dumps([result.tool_payload(), trace.events]))
                self.assertNotIn("search_result", json.dumps(result.tool_payload()))

    def test_detail_failure_is_error_and_keeps_candidate_unselected(self):
        service, _ = self.service(lambda name, args: raw([poi(location=None)]) if name == "maps_text_search"
            else raw(result_status="error", result_error={"category": "timeout"}))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.reason, "detail_error:timeout")
        self.assertIsNone(result.arrival.fact_id)
        self.assertEqual(result.fact_store, FactStore())

    def test_one_query_fails_other_verified_endpoint_is_retained(self):
        service, _ = self.service(lambda _, args: raw([poi()]) if args["keywords"] == "上海虹桥站"
            else raw(result_status="error", result_error={"category": "network"}))
        result = service.run(planning(), run_id="task")
        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual(result.departure.status, "error")
        self.assertEqual(len(result.fact_store.places), 1)

    def test_arrival_and_departure_use_first_and_last_confirmed_cities(self):
        req = request(arrival_place_query="中心站", departure_place_query="中心站", remarks="21日杭州")
        change = LodgingChange(start_date="2026-10-21", city="杭州", source={"field": "remarks", "quote": req.remarks})
        service, maps = self.service(lambda _, args: raw([poi(args["city"]+"-station", "中心站", cityname=args["city"])]))
        result = service.run(planning(req, (change,)), run_id="task")
        self.assertEqual([c.args[1]["city"] for c in maps.call_args_list], ["上海", "杭州"])
        self.assertEqual([result.tool_payload()[kind]["city"] for kind in ("arrival", "departure")], ["上海", "杭州"])
        self.assertEqual(len(result.fact_store.places), 2)

    def test_changed_city_on_first_date_can_group_both_endpoints_without_using_original_city(self):
        req = request(arrival_place_query="中心站", departure_place_query="中心站", remarks="20日杭州")
        change = LodgingChange(start_date="2026-10-20", city="杭州", source={"field": "remarks", "quote": req.remarks})
        service, maps = self.service(raw([poi(name="中心站", cityname="杭州")]))
        result = service.run(planning(req, (change,)), run_id="task")
        self.assertEqual(maps.call_count, 1)
        self.assertEqual(maps.call_args.args[1]["city"], "杭州")
        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual(result.departure.status, "verified")

    def test_same_poi_conflicting_cities_downgrade_both_endpoints(self):
        req = request(arrival_place_query="中心站", departure_place_query="中心站", remarks="21日杭州")
        change = LodgingChange(start_date="2026-10-21", city="杭州", source={"field": "remarks", "quote": req.remarks})
        service, _ = self.service(lambda _, args: raw([poi("same-id", "中心站", cityname=args["city"])]))
        result = service.run(planning(req, (change,)), run_id="task")
        self.assertEqual([result.arrival.status, result.departure.status], ["unverified", "unverified"])
        self.assertEqual(result.fact_store, FactStore())

    def test_client_initialization_failure_keeps_missing_and_error_separate(self):
        client = Mock(); client.run.side_effect = ConnectionError("secret-initialization")
        result = NativeEndpointService(endpoint_service=client).run(planning(request(arrival_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.status, "missing")
        self.assertEqual(result.departure.reason, "search_error:network")
        self.assertNotIn("secret-initialization", json.dumps(result.tool_payload()))

    def test_mapper_exception_is_sanitized_as_query_failure(self):
        service, _ = self.service(lambda *_: (_ for _ in ()).throw(RuntimeError("secret-exception")))
        result = service.run(planning(request(departure_place_query=None)), run_id="task")
        self.assertEqual(result.arrival.status, "error")
        self.assertNotIn("secret-exception", json.dumps(result.tool_payload()))

    def test_cancel_before_and_during_search_blocks_later_queries_and_result(self):
        event = Event(); event.set()
        service, maps = self.service(raw([poi()]))
        with self.assertRaises(PlanningError): service.run(planning(), run_id="task", cancel_event=event)
        maps.assert_not_called(); event.clear()
        service, maps = self.service(lambda *_: (event.set(), raw([poi(location=None)]))[1])
        with self.assertRaises(PlanningError) as caught: service.run(planning(), run_id="task", cancel_event=event)
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.assertEqual(maps.call_count, 1)

    def test_new_run_queries_again_without_cross_run_cache(self):
        service, maps = self.service(raw([poi()]))
        source = planning(request(departure_place_query=None))
        first = service.run(source, run_id="one"); second = service.run(source, run_id="two")
        self.assertEqual(maps.call_count, 2)
        self.assertNotEqual(first.arrival.fact_id, second.arrival.fact_id)

    def test_illegal_context_and_malformed_execution_are_safe_failures(self):
        query = Mock()
        source = planning()
        with self.assertRaises(PlanningError) as caught:
            NativeEndpointService(endpoint_service=query).run(source.model_copy(update={"days": source.days[:1]}), run_id="task")
        self.assertEqual(caught.exception.code, "ENDPOINT_CONTEXT_INVALID")
        query.run.assert_not_called()
        query.run.return_value = None
        with self.assertRaises(PlanningError) as caught: NativeEndpointService(endpoint_service=query).run(source, run_id="task")
        self.assertEqual(caught.exception.code, "ENDPOINT_RESULT_INVALID")

    def test_foreign_identity_version_query_and_source_are_rejected(self):
        source = planning(request(departure_place_query=None)); service, _ = self.service(raw([poi()]))
        result = service.run(source, run_id="task"); execution = result.executions[0]
        bad_source = execution.arrival.search_result.source.model_copy(update={"tool_name": "maps_weather"})
        bad_search = execution.arrival.search_result.model_copy(update={"source": bad_source})
        bad_endpoint = execution.arrival.model_copy(update={"search_result": bad_search})
        for invalid in [replace(result, run_id="other"), replace(result, fact_store=FactStore()),
            replace(result, planning_input=planning(request(request_id="other"))),
            replace(result, executions=(execution.model_copy(update={"run_id": "other"}),)),
            replace(result, executions=(execution.model_copy(update={"context_revision": 2}),)),
            replace(result, executions=(execution.model_copy(update={"arrival": execution.arrival.model_copy(update={"user_query": "别的站"})}),)),
            replace(result, executions=(execution.model_copy(update={"arrival": bad_endpoint}),))]:
            with self.subTest(invalid=invalid), self.assertRaises(PlanningError) as caught:
                validate_native_endpoint_result(source, "task", invalid)
            self.assertEqual(caught.exception.code, "ENDPOINT_RESULT_INVALID")

    def test_injected_ambiguous_candidate_cannot_be_promoted_to_verified(self):
        source = planning(request(departure_place_query=None)); service, _ = self.service(raw([poi("one"), poi("two")]))
        result = service.run(source, run_id="task"); place = result.arrival.candidates[0]
        fake = result.arrival.model_copy(update={"status": "verified", "fact_id": place.fact_id, "candidates": (place,)})
        for invalid in [replace(result, arrival=fake), replace(result, executions=(result.executions[0].model_copy(update={"arrival": fake}),))]:
            with self.assertRaises(PlanningError): validate_native_endpoint_result(source, "task", invalid)

    def test_mixed_city_scoped_context_rejected_before_map(self):
        req = request(remarks="21日杭州")
        source = planning(req, (LodgingChange(start_date="2026-10-21", city="杭州", source={"field": "remarks", "quote": req.remarks}),))
        maps = Mock()
        with self.assertRaises(PlanningError):
            EndpointVerificationService(adapter=AmapContractAdapter(maps)).run(EndpointVerificationContext.from_planning_input(
                source, run_id="task", context_revision=1, endpoint_kinds=("arrival", "departure")))
        maps.assert_not_called()


class NativeEndpointIntegrationTests(unittest.TestCase):
    def collector(self, master, service):
        lodging = Mock()
        lodging.run.side_effect = lambda planning, **_: SpecifiedLodgingResult(planning, FactStore(), (), ())
        return NativeMultiagentCollection(master_llm=master, specified_lodging_service=lodging, endpoint_service=service,
            weather_service=NativeWeatherService(adapter=AmapContractAdapter(lambda *_: {"result_status": "empty", "city": "上海", "forecasts": []})))

    def test_native_tool_pairing_duplicate_reuse_and_repair_reuse(self):
        maps = Mock(return_value=raw([poi()]))
        master = Model(batch(tool_call("verify_endpoints", {}, "arrival-a"), tool_call("verify_endpoints", {}, "arrival-b")), final(),
            tool_call("verify_endpoints", {}, "repair-endpoints"), final())
        collector = self.collector(master, NativeEndpointService(adapter=AmapContractAdapter(maps)))
        req = request(departure_place_query="上海虹桥站")
        state = collector.run(req, run_id="formal-task")
        tools = [m for m in master.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["arrival-a", "arrival-b"])
        self.assertEqual(json.loads(tools[0]["content"])["data"]["arrival"]["status"], "verified")
        continued = collector.run(req, initial_state=state, run_id="formal-task")
        self.assertEqual(maps.call_count, 1)
        self.assertEqual(state.endpoints, continued.endpoints)
        self.assertEqual(state.fact_store, continued.fact_store)
        self.assertEqual(continued.candidates, ())
        self.assertEqual(continued.area_hints, ())
        self.assertEqual(user_payload(master.requests[2]["messages"])["known_endpoints"]["arrival"]["status"], "verified")

    def test_query_and_city_overrides_rejected_before_map_then_corrected(self):
        maps = Mock(return_value=raw([poi()]))
        master = Model(tool_call("verify_endpoints", {"city": "北京", "arrival_place_query": "北京站"}, "invalid"),
            tool_call("verify_endpoints", {}, "valid"), final())
        state = self.collector(master, NativeEndpointService(adapter=AmapContractAdapter(maps))).run(request(departure_place_query=None))
        self.assertFalse(json.loads(master.requests[1]["messages"][-1]["content"])["ok"])
        self.assertEqual(maps.call_count, 1)
        self.assertEqual(state.endpoints.arrival.user_query, "上海虹桥站")

    def test_omitted_tool_still_updates_facts_before_daily_phase(self):
        maps = Mock(return_value=raw([poi()]))
        result = self.collector(Model(final()), NativeEndpointService(adapter=AmapContractAdapter(maps))).run(request(departure_place_query=None))
        updates = [json.loads(m["content"]) for m in result.protocol.messages if m["role"] == "user"]
        self.assertTrue(any("endpoints_facts_update" in u for u in updates))
        self.assertEqual(len(result.fact_store.places), 1)
        self.assertEqual(result.candidates, ())

    def test_injected_foreign_state_rejected_by_collector_before_planning(self):
        req = request(departure_place_query=None)
        service = NativeEndpointService(adapter=AmapContractAdapter(lambda *_: raw([poi()])))
        injected = Mock(); injected.run.return_value = service.run(planning(req), run_id="foreign")
        with self.assertRaises(NativeToolLoopError) as caught:
            self.collector(Model(final()), injected).run(req, run_id="task")
        self.assertEqual(caught.exception.code, "ENDPOINT_RESULT_INVALID")

    def test_endpoint_weather_and_attraction_queries_can_overlap_and_merge(self):
        barrier = Barrier(3)
        def mapper(name, args):
            barrier.wait(3)
            if name == "maps_weather": return {"result_status": "empty", "city": "上海", "forecasts": []}
            return raw([poi()] if args["keywords"] == "上海虹桥站" else [poi("park", "公园", typecode="110000")])
        maps = Mock(side_effect=mapper); adapter = AmapContractAdapter(maps)
        master = Model(batch(tool_call("verify_endpoints", {}, "endpoints"), tool_call("query_weather", {}, "weather"),
            tool_call("find_attractions", {"city": "上海", "dates": ["2026-10-20"], "objective": "采集公园", "candidate_target": 1}, "sights")), final())
        child = Model(tool_call("maps_text_search", {"city": "上海", "keywords": "公园"}, "map"),
            tool_call("submit_attractions", {"selections": [{"poi_id": "park", "recommendation_reason": "测试"}]}, "select"), final())
        collector = self.collector(master, NativeEndpointService(adapter=adapter))
        collector.weather_service = NativeWeatherService(adapter=adapter)
        collector.adapter = adapter; collector.models["attractions"] = child
        state = collector.run(request(departure_place_query="上海虹桥站"))
        self.assertEqual(maps.call_count, 3)
        self.assertEqual(len(state.fact_store.places), 2)
        self.assertEqual([c.poi_id for c in state.candidates], ["park"])
        self.assertEqual([a.anchor_poi_ids for a in state.area_hints], [("park",)])

    def test_native_daily_planner_receives_endpoint_states_without_phantom_activities_or_repairs(self):
        for known in (True, False):
            with self.subTest(known=known):
                def dining_task(messages):
                    data = user_payload(messages)
                    return tool_call("find_restaurants", {"city": "上海", "objective": "各餐次测试", "candidate_target": 1,
                        "dates": [d["day_date"] for d in data["day_slots"]], "meals": [
                            {"date": d["day_date"], "period": s["period"], "anchor_poi_id": "park"}
                            for d in data["day_slots"] for s in d["slots"] if s["eligibility"] == "required"
                            and s["period"] in ("breakfast", "lunch", "dinner")]}, "dining")
                def dining_query(messages):
                    return tool_call("maps_around_search", {"scope_id": user_payload(messages)["meal_scopes"][0]["scope_id"], "keywords": "早点餐馆"}, "food-map")
                def dining_submit(messages):
                    return tool_call("submit_restaurants", {"selections": [{"poi_id": "food", "recommendation_reason": "测试",
                        "slot_ids": [s["scope_id"] for s in user_payload(messages)["meal_scopes"]]}]}, "food-select")
                def daily(messages):
                    data = user_payload(messages)
                    self.assertEqual([data["endpoints"][k]["status"] for k in ("arrival", "departure")],
                                     ["verified", "verified"] if known else ["ambiguous", "error"])
                    if not known:
                        self.assertTrue(all(data["endpoints"][k]["place"] is None for k in ("arrival", "departure")))
                    hotel = next(c for c in data["candidates"] if c["category"] == "lodging")
                    self.assertFalse({"station", "station2", "departure"} & {c["poi_id"] for c in data["candidates"]})
                    output = []
                    for day in data["planning_input"]["days"]:
                        options = next(o for o in data["day_slot_options"] if o["day_id"] == day["day_id"])
                        activities = []
                        for slot in (*options["available_meal_slot_ids"], *options["available_sightseeing_slot_ids"]):
                            candidate = next(c for c in data["candidates"] if slot in c["applicable_slot_ids"])
                            activities.append({"proposal_ref": slot, "slot_id": slot, "candidate_ref": candidate["candidate_id"],
                                "kind": candidate["category"], "recommendation_reason": "模拟候选"})
                        output.append({"day_id": day["day_id"], "day_date": day["day_date"], "theme": "测试",
                            "activities": activities, "lodging": {"candidate_ref": hotel["candidate_id"]}})
                    return tool_call("submit_daily_plan", {"days": output}, "daily")
                empty = {key: [] for key in ("core_goals", "preferences", "hard_requirements", "lodging_changes", "unknowns", "assumptions")}
                master = Model(tool_call("submit_requirements", empty, "requirements"), final(), batch(
                    tool_call("verify_endpoints", {}, "endpoints"), tool_call("query_weather", {}, "weather"),
                    tool_call("find_attractions", {"city": "上海", "dates": ["2026-10-20", "2026-10-21", "2026-10-22"],
                        "objective": "测试公园", "candidate_target": 1}, "sights")), dining_task, final(), daily, final())
                sight = Model(tool_call("maps_text_search", {"city": "上海", "keywords": "公园"}, "sight-map"),
                    tool_call("submit_attractions", {"selections": [{"poi_id": "park", "recommendation_reason": "测试"}]}, "select"), final())
                dining = Model(dining_query, dining_submit, final())
                def mapper(name, args):
                    if name == "maps_weather": return {"result_status": "empty", "city": "上海", "forecasts": []}
                    if name == "maps_text_search" and args["keywords"] in ("上海虹桥站", "上海站"):
                        if args["keywords"] == "上海虹桥站": return raw([poi()] if known else [poi(), poi("station2")])
                        return raw([poi("departure", "上海站")]) if known else raw(result_status="error", result_error={"category": "timeout"})
                    identifier = {"maps_search_detail": "hotel", "maps_text_search": "park", "maps_around_search": "food"}[name]
                    return raw([poi(identifier, {"hotel": "测试酒店", "park": "公园", "food": "早点餐馆"}[identifier],
                        typecode={"hotel": "100100", "park": "110000", "food": "050100"}[identifier])])
                maps = Mock(side_effect=mapper); trace = NativeChainTrace()
                result = NativeTripPlanner(master_llm=master, attraction_llm=sight, dining_llm=dining,
                    adapter=AmapContractAdapter(maps), emit=trace.emit).run(request(), task_id="task", plan_id="plan")
                self.assertEqual(result.draft.plan_id, "plan")
                self.assertEqual(result.draft.source_run_id, "task")
                self.assertEqual(result.collection.endpoints.run_id, "task")
                self.assertEqual(len(result.repair.attempts), 0)
                self.assertFalse({"station", "station2", "departure"} & {a.poi_id for d in result.draft.days for a in d.activities})
                self.assertTrue(any(e["event"] == "endpoints.result" for e in trace.events))
                self.assertFalse(any("direction" in c.args[0] for c in maps.call_args_list))


if __name__ == "__main__":
    unittest.main()
