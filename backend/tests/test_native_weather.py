"""天气事实单模块及原生链路模拟验证；不依赖其他测试文件或真实服务。"""
from dataclasses import replace
from datetime import datetime
import json
from threading import Barrier, Event
import unittest
from unittest.mock import Mock

from app.agents.execution import PlanningError
from app.agents.native_multiagent_collection import NativeMultiagentCollection
from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_v4 import TripRequest, RequirementSummary, FactStore
from app.models.planning_v4.input import LodgingChange
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.llm_service import NativeLLMCompletion
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_tool_loop import NativeToolLoopError
from app.services.native_weather_service import NativeWeatherService, validate_native_weather_result
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.specified_lodging_service import SpecifiedLodgingResult
from app.services.weather_query_service import WeatherQueryContext, WeatherQueryService


def request(**updates):
    return TripRequest(**{**dict(request_id="weather-fixture", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-22T15:00:00+08:00"),
        lodging_query="测试酒店", lodging_poi_id="hotel"), **updates})


def planning(req=None, changes=()):
    req = req or request()
    return build_planning_input(PlanningInputBuildRequest(request=req, requirements=RequirementSummary(
        requirements_id="weather-summary", request_id=req.request_id, lodging_changes=changes),
        rules=default_planning_v4_rules()))


def raw_weather(rows=(), city="上海", **updates):
    return {"result_status": "ok", "city": city, "forecasts": list(rows),
            "reporttime": "2026-10-19 10:00:00", **updates}


def forecast(day, **updates):
    return {"date": day, "dayweather": "小雨", "nightweather": "多云", **updates}


def tool_call(name, args, identifier):
    return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [{
        "id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, None, "tool_calls")


def final():
    return NativeLLMCompletion({"role": "assistant", "content": "查询完成；未知保持未知"}, None, "stop")


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


class NativeWeatherTests(unittest.TestCase):
    def service(self, payload):
        mapper = Mock(side_effect=payload if callable(payload) else lambda *_: payload)
        return NativeWeatherService(adapter=AmapContractAdapter(mapper)), mapper

    def test_exact_date_coverage_and_partial_no_forecast_copy(self):
        service, mapper = self.service(raw_weather([forecast("2026-10-19"), forecast("2026-10-20"),
            forecast("2026-10-21", nightweather=None, daytemp="0")]))
        source = planning(); before = source.model_dump(mode="json")
        result = service.run(source, run_id="task-weather")
        coverage = result.tool_payload()["coverage"]
        self.assertEqual([c["forecast_date"] for c in coverage], ["2026-10-20", "2026-10-21", "2026-10-22"])
        self.assertEqual([c["status"] for c in coverage], ["available", "partial", "unknown"])
        self.assertEqual(coverage[-1]["reason"], "out_of_coverage")
        self.assertEqual(result.fact_store.weather[0].forecasts[-1].day_temp_c, 0)
        self.assertEqual(len(result.fact_store.weather[0].forecasts), 2)
        self.assertEqual(result.fact_store.weather[0].source, result.fact_store.sources[0])
        self.assertEqual(source.model_dump(mode="json"), before)
        mapper.assert_called_once_with("maps_weather", {"city": "上海"})

    def test_duplicate_blank_and_missing_dates_remain_unknown(self):
        service, _ = self.service(raw_weather([forecast("2026-10-20"), forecast("2026-10-20", dayweather="晴"),
            forecast("2026-10-21", dayweather=" ", nightweather=None, daytemp="NaN")]))
        result = service.run(planning(), run_id="task")
        self.assertEqual([c["reason"] for c in result.tool_payload()["coverage"]],
                         ["duplicate_provider_date", "no_valid_fields", "out_of_coverage"])
        self.assertEqual(result.fact_store, FactStore())

    def test_empty_result_has_no_forecast_not_query_error(self):
        service, _ = self.service(raw_weather(result_status="empty"))
        result = service.run(planning(), run_id="task")
        self.assertEqual(result.executions[0].tool_status, "empty")
        self.assertTrue(all(c.reason == "no_forecast" for c in result.executions[0].coverage))

    def test_all_future_dates_outside_provider_range_stay_unknown(self):
        service, _ = self.service(raw_weather([forecast("2026-10-19")]))
        result = service.run(planning(), run_id="task")
        self.assertEqual(result.executions[0].tool_status, "ok")
        self.assertEqual(result.fact_store, FactStore())
        self.assertTrue(all(c.reason == "out_of_coverage" for c in result.executions[0].coverage))

    def test_actual_weather_cannot_replace_date_forecasts(self):
        service, _ = self.service({"result_status": "ok", "city": "上海", "weather": "晴", "temperature": "20"})
        result = service.run(planning(), run_id="task")
        self.assertEqual(result.fact_store, FactStore())
        self.assertTrue(all(c.status == "unknown" for c in result.executions[0].coverage))

    def test_city_mismatch_or_unverified_is_not_consumable(self):
        for payload, reason in [(raw_weather([forecast("2026-10-20")], city="北京"), "city_mismatch"),
                                (raw_weather([forecast("2026-10-20")], city=[]), "city_unverified")]:
            with self.subTest(reason=reason):
                service, _ = self.service(payload)
                result = service.run(planning(), run_id="task")
                self.assertEqual(result.fact_store, FactStore())
                self.assertTrue(all(c.reason == reason for c in result.executions[0].coverage))

    def test_query_failure_unknown_and_safe_payload_and_trace(self):
        for category in ("timeout", "network", "auth", "rate_limit", "provider_error", "invalid_response"):
            with self.subTest(category=category):
                service, mapper = self.service({"result_status": "error", "result_error": {
                    "category": category, "message": "secret-provider-message"}})
                trace = NativeChainTrace(); service.emit = trace.emit
                result = service.run(planning(), run_id="task")
                self.assertEqual(result.executions[0].tool_status, "error")
                self.assertEqual(result.executions[0].tool_error_category, category)
                self.assertTrue(all(c.reason == "query_error" for c in result.executions[0].coverage))
                self.assertEqual(result.fact_store, FactStore())
                self.assertEqual(mapper.call_count, 1)
                self.assertNotIn("secret-provider-message", json.dumps([result.tool_payload(), trace.events]))

    def test_mapper_exception_is_sanitized_as_unknown(self):
        service, _ = self.service(lambda *_: (_ for _ in ()).throw(RuntimeError("secret-exception")))
        result = service.run(planning(), run_id="task")
        self.assertEqual(result.executions[0].tool_status, "error")
        self.assertNotIn("secret-exception", json.dumps(result.tool_payload()))

    def test_client_initialization_failure_also_yields_unknown(self):
        query = Mock(); query.run.side_effect = ConnectionError("secret-initialization")
        result = NativeWeatherService(query_service=query).run(planning(), run_id="task")
        self.assertEqual(result.executions[0].tool_error_category, "network")
        self.assertTrue(all(c.reason == "query_error" for c in result.executions[0].coverage))
        self.assertEqual(result.fact_store, FactStore())
        self.assertEqual(result.executions[0].diagnostic_tool_result.source.effective_params, {})
        self.assertNotIn("secret-initialization", json.dumps(result.tool_payload()))

    def test_city_changes_and_return_visit_query_once_per_city(self):
        req = request(remarks="21日杭州，22日上海")
        changes = tuple(LodgingChange(start_date=date, city=city, source={"field": "remarks", "quote": quote})
            for date, city, quote in [("2026-10-21", "杭州", "21日杭州"), ("2026-10-22", "上海", "22日上海")])
        service, mapper = self.service(lambda _, args: raw_weather(
            [forecast("2026-10-20"), forecast("2026-10-21"), forecast("2026-10-22")], city=args["city"]))
        result = service.run(planning(req, changes), run_id="task")
        self.assertEqual([c["city"] for c in result.tool_payload()["coverage"]], ["上海", "杭州", "上海"])
        self.assertEqual(mapper.call_count, 2)
        self.assertEqual(len(result.fact_store.weather[0].forecasts), 2)
        self.assertEqual(len(result.fact_store.weather[1].forecasts), 1)

    def test_one_city_fails_other_city_is_retained(self):
        req = request(remarks="21日杭州")
        change = LodgingChange(start_date="2026-10-21", city="杭州", source={"field": "remarks", "quote": req.remarks})
        service, _ = self.service(lambda _, args: raw_weather([forecast("2026-10-20")]) if args["city"] == "上海"
            else {"result_status": "error", "result_error": {"category": "timeout"}})
        result = service.run(planning(req, (change,)), run_id="task")
        self.assertEqual([c["status"] for c in result.tool_payload()["coverage"]], ["available", "unknown", "unknown"])
        self.assertEqual(len(result.fact_store.weather), 1)

    def test_scoped_query_rejects_unknown_duplicate_or_mixed_city_days_before_map(self):
        source = planning(request(remarks="21日杭州"), (LodgingChange(start_date="2026-10-21", city="杭州",
            source={"field": "remarks", "quote": "21日杭州"}),))
        mapper = Mock()
        for ids in [("foreign",), (source.days[0].day_id,)*2, tuple(d.day_id for d in source.days)]:
            with self.subTest(ids=ids), self.assertRaises(PlanningError):
                WeatherQueryService(adapter=AmapContractAdapter(mapper)).run(WeatherQueryContext.from_planning_input(
                    source, run_id="task", context_revision=1, day_ids=ids))
        mapper.assert_not_called()

    def test_native_service_rejects_incomplete_dates_before_injected_query(self):
        source = planning(); query = Mock()
        with self.assertRaises(PlanningError) as caught:
            NativeWeatherService(query_service=query).run(source.model_copy(update={"days": source.days[:1]}), run_id="task")
        self.assertEqual(caught.exception.code, "WEATHER_CONTEXT_INVALID")
        query.run.assert_not_called()

    def test_malformed_injected_execution_is_classified_and_safe(self):
        query = Mock(); query.run.return_value = None
        with self.assertRaises(PlanningError) as caught:
            NativeWeatherService(query_service=query).run(planning(), run_id="task")
        self.assertEqual(caught.exception.code, "WEATHER_RESULT_INVALID")

    def test_foreign_identity_or_forged_projection_is_rejected(self):
        source = planning(); service, _ = self.service(raw_weather([forecast("2026-10-20")]))
        result = service.run(source, run_id="task")
        execution = result.executions[0]
        for invalid in [replace(result, run_id="foreign"), replace(result, fact_store=FactStore()),
                replace(result, executions=(execution.model_copy(update={"run_id": "foreign"}),)),
                replace(result, executions=(execution.model_copy(update={"city": "杭州"}),)),
                replace(result, executions=(execution.model_copy(update={"context_revision": 2}),)),
                replace(result, planning_input=planning(request(request_id="other")))]:
            with self.subTest(invalid=invalid), self.assertRaises(PlanningError) as caught:
                validate_native_weather_result(source, "task", invalid)
            self.assertEqual(caught.exception.code, "WEATHER_RESULT_INVALID")

    def test_cancellation_before_and_after_query_does_not_accept_late_result(self):
        event = Event(); event.set()
        service, mapper = self.service(raw_weather())
        with self.assertRaises(PlanningError): service.run(planning(), run_id="task", cancel_event=event)
        mapper.assert_not_called()
        event.clear()
        service, mapper = self.service(lambda *_: (event.set(), raw_weather([forecast("2026-10-20")]))[1])
        with self.assertRaises(PlanningError) as caught: service.run(planning(), run_id="task", cancel_event=event)
        self.assertEqual(caught.exception.code, "CANCELLED")

    def test_separate_runs_query_again_without_cross_run_cache(self):
        service, mapper = self.service(raw_weather([forecast("2026-10-20")]))
        first = service.run(planning(), run_id="one"); second = service.run(planning(), run_id="two")
        self.assertEqual(mapper.call_count, 2)
        self.assertNotEqual(first.fact_store.weather[0].fact_id, second.fact_store.weather[0].fact_id)


class NativeWeatherIntegrationTests(unittest.TestCase):
    def collector(self, master, service):
        lodging = Mock()
        lodging.run.side_effect = lambda planning, **_: SpecifiedLodgingResult(planning, FactStore(), (), ())
        return NativeMultiagentCollection(master_llm=master, specified_lodging_service=lodging, weather_service=service)

    def test_master_tool_pairing_duplicate_reuse_and_repair_reuse(self):
        mapper = Mock(return_value=raw_weather([forecast("2026-10-20")]))
        master = Model(batch(tool_call("query_weather", {}, "weather-a"), tool_call("query_weather", {}, "weather-b")), final(),
            tool_call("query_weather", {}, "repair-weather"), final())
        collector = self.collector(master, NativeWeatherService(adapter=AmapContractAdapter(mapper)))
        state = collector.run(request(), run_id="formal-task")
        messages = [m for m in master.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in messages], ["weather-a", "weather-b"])
        self.assertEqual(json.loads(messages[0]["content"])["data"]["coverage"][0]["status"], "available")
        repaired = collector.run(request(), initial_state=state, run_id="formal-task")
        self.assertEqual(mapper.call_count, 1)
        self.assertEqual(state.fact_store.weather, repaired.fact_store.weather)
        self.assertEqual(user_payload(master.requests[2]["messages"])["known_weather"]["coverage"][0]["status"], "available")

    def test_weather_city_and_date_arguments_rejected_before_query_then_corrected(self):
        mapper = Mock(return_value=raw_weather())
        master = Model(tool_call("query_weather", {"city": "北京", "dates": ["1900-01-01"]}, "invalid"),
            tool_call("query_weather", {}, "valid"), final())
        result = self.collector(master, NativeWeatherService(adapter=AmapContractAdapter(mapper))).run(request())
        self.assertFalse(json.loads(master.requests[1]["messages"][-1]["content"])["ok"])
        self.assertEqual(mapper.call_count, 1)
        self.assertEqual(result.weather.executions[0].city, "上海")

    def test_omitted_tool_still_provides_required_fact_update_before_planning(self):
        mapper = Mock(return_value=raw_weather([forecast("2026-10-20")]))
        collector = self.collector(Model(final()), NativeWeatherService(adapter=AmapContractAdapter(mapper)))
        result = collector.run(request())
        self.assertEqual(mapper.call_count, 1)
        self.assertTrue(any("weather_facts_update" in json.loads(m["content"])
                            for m in result.protocol.messages if m["role"] == "user"))

    def test_injected_foreign_weather_result_is_rejected(self):
        service = NativeWeatherService(adapter=AmapContractAdapter(lambda *_: raw_weather()))
        result = service.run(planning(), run_id="other")
        injected = Mock(); injected.run.return_value = result
        with self.assertRaises(NativeToolLoopError) as caught:
            self.collector(Model(final()), injected).run(request(), run_id="task")
        self.assertEqual(caught.exception.code, "WEATHER_RESULT_INVALID")

    def test_weather_and_attraction_queries_can_overlap_and_merge(self):
        barrier = Barrier(2)
        def mapper(name, args):
            barrier.wait(3)
            return raw_weather([forecast("2026-10-20")]) if name == "maps_weather" else {"result_status": "ok", "pois": [
                {"id": "park", "name": "测试公园", "cityname": "上海", "typecode": "110000", "location": "121.49,31.24"}]}
        maps = Mock(side_effect=mapper)
        master = Model(batch(tool_call("query_weather", {}, "weather"), tool_call("find_attractions", {
            "city": "上海", "dates": ["2026-10-20"], "objective": "采集公园", "candidate_target": 1}, "sight")), final())
        child = Model(tool_call("maps_text_search", {"city": "上海", "keywords": "公园"}, "map"),
            tool_call("submit_attractions", {"selections": [{"poi_id": "park", "recommendation_reason": "测试"}]}, "select"), final())
        collector = self.collector(master, NativeWeatherService(adapter=AmapContractAdapter(maps)))
        collector.adapter = AmapContractAdapter(maps); collector.models["attractions"] = child
        result = collector.run(request())
        self.assertEqual(maps.call_count, 2)
        self.assertEqual(len(result.children), 1)
        self.assertEqual(len(result.fact_store.weather), 1)
        self.assertEqual(len(result.fact_store.places), 1)

    def test_native_daily_draft_binds_only_covered_dates_and_failure_does_not_repair(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                def dining_task(messages):
                    data = user_payload(messages)
                    return tool_call("find_restaurants", {"city": "上海", "objective": "各餐次测试", "candidate_target": 1,
                        "dates": [d["day_date"] for d in data["day_slots"]], "meals": [
                            {"date": d["day_date"], "period": s["period"], "anchor_poi_id": "park"}
                            for d in data["day_slots"] for s in d["slots"] if s["eligibility"] == "required"
                            and s["period"] in ("breakfast", "lunch", "dinner")]}, "dining")
                def dining_query(messages):
                    return tool_call("maps_around_search", {"scope_id": user_payload(messages)["meal_scopes"][0]["scope_id"],
                        "keywords": "早点餐馆"}, "food-map")
                def dining_submit(messages):
                    return tool_call("submit_restaurants", {"selections": [{"poi_id": "food", "recommendation_reason": "测试",
                        "slot_ids": [s["scope_id"] for s in user_payload(messages)["meal_scopes"]]}]}, "food-select")
                def daily(messages):
                    data = user_payload(messages)
                    self.assertEqual(len(data["weather_facts"]), 0 if failure else 1)
                    hotel = next(c for c in data["candidates"] if c["category"] == "lodging")
                    output = []
                    for day in data["planning_input"]["days"]:
                        options = next(o for o in data["day_slot_options"] if o["day_id"] == day["day_id"])
                        activities = []
                        for slot in (*options["available_meal_slot_ids"], *options["available_sightseeing_slot_ids"]):
                            candidate = next(c for c in data["candidates"] if slot in c["applicable_slot_ids"])
                            activities.append({"proposal_ref": slot, "slot_id": slot, "candidate_ref": candidate["candidate_id"],
                                "kind": candidate["category"], "recommendation_reason": "模拟候选"})
                        output.append({"day_id": day["day_id"], "day_date": day["day_date"], "theme": "测试",
                            "activities": activities, "lodging": {"candidate_ref": hotel["candidate_id"]},
                            "weather_advice": "有雨，请带雨具"})
                    return tool_call("submit_daily_plan", {"days": output}, "daily")
                empty = {key: [] for key in ("core_goals", "preferences", "hard_requirements", "lodging_changes", "unknowns", "assumptions")}
                master = Model(tool_call("submit_requirements", empty, "requirements"), batch(
                    tool_call("query_weather", {}, "weather"), tool_call("find_attractions", {
                        "city": "上海", "dates": ["2026-10-20", "2026-10-21", "2026-10-22"],
                        "objective": "测试公园", "candidate_target": 1}, "sight")), dining_task, final(), daily, final())
                sight = Model(tool_call("maps_text_search", {"city": "上海", "keywords": "公园"}, "sight-map"),
                    tool_call("submit_attractions", {"selections": [{"poi_id": "park", "recommendation_reason": "测试"}]}, "select"), final())
                dining = Model(dining_query, dining_submit, final())
                def mapper(name, args):
                    if name == "maps_weather":
                        return {"result_status": "error", "result_error": {"category": "timeout"}} if failure else raw_weather([forecast("2026-10-20")])
                    identifier = {"maps_search_detail": "hotel", "maps_text_search": "park", "maps_around_search": "food"}[name]
                    return {"result_status": "ok", "pois": [{"id": identifier, "name": {"hotel": "测试酒店", "park": "公园", "food": "早点餐馆"}[identifier],
                        "cityname": "上海", "location": "121.49,31.24", "typecode": {"hotel": "100100", "park": "110000", "food": "050100"}[identifier]}]}
                maps = Mock(side_effect=mapper)
                trace = NativeChainTrace()
                result = NativeTripPlanner(master_llm=master, attraction_llm=sight, dining_llm=dining,
                    adapter=AmapContractAdapter(maps), emit=trace.emit).run(request(), task_id="task", plan_id="plan")
                self.assertEqual(result.draft.plan_id, "plan")
                self.assertEqual(result.draft.source_run_id, "task")
                self.assertEqual(len(result.repair.attempts), 0)
                self.assertEqual(sum(c.args[0] == "maps_weather" for c in maps.call_args_list), 1)
                self.assertEqual(result.draft.days[0].weather_fact_id is None, failure)
                if not failure: self.assertEqual(result.draft.days[0].weather_advice, "有雨，请带雨具")
                for day in result.draft.days[1:]:
                    self.assertIsNone(day.weather_fact_id)
                    self.assertIn("未知", day.weather_advice)
                self.assertTrue(any(e["event"] == "weather.result" for e in trace.events))


if __name__ == "__main__":
    unittest.main()
