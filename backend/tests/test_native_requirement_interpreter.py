"""第二点单模块验证：固定原始需求和原生响应；不导入其他测试文件、不启动服务。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import Event
import unittest
from app.services.native_weather_service import NativeWeatherService
from app.services.amap_contract_adapter import AmapContractAdapter
from unittest.mock import Mock, patch

from app.agents.native_requirement_interpreter import NativeRequirementInterpreter, validate_native_requirement_summary
from app.models.planning_v4.input import TripRequest, TripBudget
from app.services.llm_service import NativeLLMCompletion
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_tool_loop import NativeToolLoopError


def weather_fixture():
    return NativeWeatherService(adapter=AmapContractAdapter(lambda *_: {"result_status": "empty", "city": "上海", "forecasts": []}))


def request():
    return TripRequest(request_id="requirements-fixture", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-22T15:00:00+08:00"),
        preferences=("历史建筑", "本帮菜"), transport_preferences=("transit", "walking"),
        lodging_query="指定住处", lodging_poi_id="hotel-fixed", explicit_return_buffer_s=0,
        budget=TripBudget(amount="4000", amount_basis="per_person"),
        remarks="城市漫步；必须去外滩，不要去动物园；必须吃大壶春，必须素食；第二天换到新住处。")


def grounded(text, quote, field="remarks"):
    return {"text": text, "source": {"field": field, "quote": quote}}


def submission(req=None):
    req = req or request()
    return {"core_goals": [grounded("城市漫步", "城市漫步")],
        "hard_requirements": [
            {"kind": "must_visit", **grounded("外滩", "必须去外滩")},
            {"kind": "must_avoid", **grounded("动物园", "不要去动物园")},
            {"kind": "must_include", **grounded("大壶春", "必须吃大壶春")},
            {"kind": "must_include", **grounded("素食", "必须素食")}],
        "lodging_changes": [{"start_date": "2026-10-21", "lodging_query": "新住处",
            "source": {"field": "remarks", "quote": "第二天换到新住处"}}],
        "preferences": [*[grounded(p, p, "preferences") for p in req.preferences],
                        *[grounded(p, p, "transport_preferences") for p in req.transport_preferences]],
        "unknowns": ["arrival_place_query", "departure_place_query", "budget.party_size"], "assumptions": []}


def call(args, identifier="submit", raw=False):
    return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [{"id": identifier,
        "type": "function", "function": {"name": "submit_requirements", "arguments": args if raw else json.dumps(args)}}]},
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, "tool_calls")


def final():
    return NativeLLMCompletion({"role": "assistant", "content": "已整理"}, None, "stop")


class Model:
    def __init__(self, *responses): self.responses, self.requests = list(responses), []
    def invoke_native(self, messages, *, tools, **kwargs):
        self.requests.append({"messages": deepcopy(messages), "tools": tools})
        value = self.responses.pop(0)
        if isinstance(value, Exception): raise value
        return value(messages) if callable(value) else value


class NativeRequirementTests(unittest.TestCase):
    def run_submission(self, args=None, req=None):
        req = req or request()
        model = Model(call(args if args is not None else submission(req)), final())
        return NativeRequirementInterpreter(llm=model).run(req), model

    def assert_rejection(self, args, code, *, req=None):
        req = req or request()
        model = Model(call(args, "bad"), call(submission(req), "correct"), final())
        result = NativeRequirementInterpreter(llm=model).run(req)
        value = json.loads(model.requests[1]["messages"][-1]["content"])
        self.assertEqual(value["data"]["code"], code)
        self.assertEqual(result.summary.request_id, req.request_id)
        return model

    def test_complete_summary_and_grounding_preserve_original_structured_fields(self):
        req = request(); original = req.model_dump(mode="json")
        result, model = self.run_submission(req=req)
        self.assertEqual(req.model_dump(mode="json"), original)
        self.assertEqual(json.loads(model.requests[0]["messages"][-1]["content"])["trip_request"], original)
        self.assertEqual(result.summary.request_id, req.request_id)
        self.assertTrue(result.summary.requirements_id.startswith("requirements_"))
        self.assertEqual([r.kind for r in result.summary.hard_requirements], ["must_visit", "must_avoid", "must_include", "must_include"])
        self.assertEqual(result.summary.preferences, ("历史建筑", "本帮菜", "transit", "walking"))
        self.assertEqual(result.summary.lodging_changes[0].start_date.isoformat(), "2026-10-21")
        self.assertEqual(result.submission.core_goals[0].source.quote, "城市漫步")
        self.assertEqual(result.summary.assumptions, ())
        self.assertEqual(len({r.requirement_id for r in result.summary.hard_requirements}), 4)
        accepted = json.loads(model.requests[1]["messages"][-1]["content"])["data"]
        self.assertEqual(accepted["requirements"], result.summary.model_dump(mode="json"))
        self.assertEqual([t["function"]["name"] for t in model.requests[0]["tools"]], ["submit_requirements"])

    def test_datetime_source_matches_original_json_iso_format(self):
        original = request()
        for req in (original, original.model_copy(update={"arrival_at": original.arrival_at.astimezone(timezone.utc)})):
            with self.subTest(offset=req.arrival_at.utcoffset()):
                args = submission(); quote = req.model_dump(mode="json")["arrival_at"]
                args["core_goals"].append(grounded("按用户到达日期旅行", quote, "arrival_at"))
                result, _ = self.run_submission(args, req)
                self.assertEqual(result.submission.core_goals[-1].source.quote, quote)

    def test_fixed_input_and_id_overrides_are_structurally_rejected(self):
        for key, value in (("city", "北京"), ("arrival_at", "changed"), ("budget", {"amount": 1}),
                           ("request_id", "other"), ("requirements_id", "invented")):
            with self.subTest(key=key):
                args = submission(); args[key] = value
                model = Model(call(args, "bad"), call(submission(), "good"), final())
                NativeRequirementInterpreter(llm=model).run(request())
                error = json.loads(model.requests[1]["messages"][-1]["content"])["error"]
                self.assertEqual(error["code"], "INVALID_TOOL_ARGUMENTS")

    def test_required_categories_cannot_be_omitted(self):
        for key in submission():
            with self.subTest(field=key):
                args = submission(); args.pop(key)
                model = Model(call(args, "bad"), call(submission(), "good"), final())
                NativeRequirementInterpreter(llm=model).run(request())
                self.assertEqual(json.loads(model.requests[1]["messages"][-1]["content"])["error"]["code"], "INVALID_TOOL_ARGUMENTS")

    def test_hard_source_invented_or_from_wrong_field_rejected_then_corrected(self):
        for source in ({"field": "remarks", "quote": "外滩一定要去"}, {"field": "preferences", "quote": "必须去外滩"}):
            args = submission(); args["hard_requirements"][0]["source"] = source
            self.assert_rejection(args, "REQUIREMENT_SOURCE_INVALID")

    def test_goals_and_soft_preferences_also_need_source(self):
        for category in ("core_goals", "preferences"):
            args = submission(); args[category][0]["source"]["quote"] = "未经用户要求的体验"
            self.assert_rejection(args, "REQUIREMENT_SOURCE_INVALID")

    def test_structured_preferences_and_transport_cannot_be_silently_dropped(self):
        for index in (0, 3):
            args = submission(); args["preferences"].pop(index)
            self.assert_rejection(args, "REQUIREMENT_PREFERENCES_INCOMPLETE")

    def test_duplicate_hard_requirements_rejected(self):
        args = submission(); args["hard_requirements"].append(deepcopy(args["hard_requirements"][0]))
        self.assert_rejection(args, "REQUIREMENT_DUPLICATE")

    def test_known_inputs_cannot_be_called_unknown(self):
        for unknown in ("lodging_query", "budget.amount"):
            args = submission(); args["unknowns"].append(unknown)
            self.assert_rejection(args, "REQUIREMENT_UNKNOWN_INVALID")
        args = submission(); args["unknowns"].append(args["unknowns"][0])
        self.assert_rejection(args, "REQUIREMENT_UNKNOWN_INVALID")

    def test_selected_hotel_poi_is_known_even_without_query(self):
        req = request().model_copy(update={"lodging_query": None})
        args = submission(req); args["unknowns"].append("lodging_query")
        self.assert_rejection(args, "REQUIREMENT_UNKNOWN_INVALID", req=req)

    def test_structured_lodging_does_not_become_dining_or_other_hard_requirement(self):
        args = submission(); args["hard_requirements"].append({"kind": "must_include", **grounded("指定住处", "指定住处", "lodging_query")})
        self.assert_rejection(args, "REQUIREMENT_STRUCTURED_LODGING_DUPLICATE")

    def test_lodging_date_scope_duplicate_dates_and_unquoted_name_rejected(self):
        for mutate in (lambda a: a["lodging_changes"][0].update(start_date="2026-10-23"),
                       lambda a: a["lodging_changes"].append(deepcopy(a["lodging_changes"][0])),
                       lambda a: a["lodging_changes"][0].update(lodging_query="模型另选酒店")):
            args = submission(); mutate(args)
            self.assert_rejection(args, "REQUIREMENT_SOURCE_INVALID")

    def test_empty_text_and_whitespace_source_rejected(self):
        for mutate in (lambda a: a["hard_requirements"][0].update(text="  "),
                       lambda a: a["core_goals"][0]["source"].update(quote=" ")):
            args = submission(); mutate(args)
            self.assert_rejection(args, "REQUIREMENT_TEXT_INVALID")

    def test_assumptions_and_model_assigned_requirement_id_rejected(self):
        for mutate in (lambda a: a.update(assumptions=["两位成人"]),
                       lambda a: a["hard_requirements"][0].update(requirement_id="M1")):
            args = submission(); mutate(args)
            model = Model(call(args, "bad"), call(submission(), "good"), final())
            NativeRequirementInterpreter(llm=model).run(request())
            self.assertEqual(json.loads(model.requests[1]["messages"][-1]["content"])["error"]["code"], "INVALID_TOOL_ARGUMENTS")

    def test_invalid_json_duplicate_keys_nonstandard_numbers_and_types(self):
        for raw in ('{"core_goals":[],"core_goals":[]}', '{"core_goals":NaN}', '{broken', '[]'):
            model = Model(call(raw, "bad", raw=True), call(submission(), "good"), final())
            NativeRequirementInterpreter(llm=model).run(request())
            self.assertEqual(json.loads(model.requests[1]["messages"][-1]["content"])["error"]["code"], "INVALID_TOOL_ARGUMENTS")

    def test_no_tool_submit_or_invalid_only_never_creates_empty_fallback(self):
        for responses in ((final(),), (call({}), final())):
            with self.subTest(responses=responses):
                with self.assertRaises(NativeToolLoopError) as caught:
                    NativeRequirementInterpreter(llm=Model(*responses)).run(request())
                self.assertEqual(caught.exception.code, "MASTER_DID_NOT_SUBMIT_REQUIREMENTS")

    def test_accepted_summary_cannot_be_overwritten_by_later_submission(self):
        alternate = submission(); alternate["hard_requirements"] = []
        model = Model(call(submission(), "first"), call(alternate, "second"), final())
        result = NativeRequirementInterpreter(llm=model).run(request())
        self.assertEqual(len(result.summary.hard_requirements), 4)
        self.assertEqual(json.loads(model.requests[2]["messages"][-1]["content"])["data"]["status"], "already_submitted")

    def test_provider_failure_truncation_and_cancellation_have_no_summary(self):
        for response in (RuntimeError("private provider text"), NativeLLMCompletion({"role": "assistant", "content": None}, None, "length")):
            with self.assertRaises(NativeToolLoopError) as caught:
                NativeRequirementInterpreter(llm=Model(response)).run(request())
            self.assertNotIn("private", str(caught.exception))
        event = Event(); event.set(); model = Model()
        with self.assertRaises(NativeToolLoopError) as caught:
            NativeRequirementInterpreter(llm=model).run(request(), cancel_event=event)
        self.assertEqual(caught.exception.code, "CANCELLED"); self.assertEqual(model.requests, [])

    def test_protocol_history_and_provider_extra_fields_preserved_but_not_traced(self):
        response = call(submission()); response.message["provider_extra"] = {"opaque": "kept"}
        trace = NativeChainTrace(); previous = [{"role": "system", "content": "先前历史"}]
        result = NativeRequirementInterpreter(llm=Model(response, final()), emit=trace.emit).run(request(), messages=previous)
        self.assertEqual(result.protocol.messages[0], previous[0])
        self.assertTrue(any(m.get("provider_extra") for m in result.protocol.messages))
        self.assertNotIn("provider_extra", json.dumps(trace.events))

    def test_consumer_validation_rejects_foreign_summary_and_duplicate_ids(self):
        result, _ = self.run_submission()
        for summary in (result.summary.model_copy(update={"request_id": "other"}), result.summary.model_copy(update={
                "hard_requirements": (result.summary.hard_requirements[0], result.summary.hard_requirements[0])})):
            with self.assertRaises(NativeToolLoopError): validate_native_requirement_summary(request(), summary)

    def test_empty_request_categories_are_valid_and_do_not_fabricate_demands(self):
        req = request().model_copy(update={"preferences": (), "transport_preferences": (), "remarks": ""})
        args = {key: [] for key in submission()}
        result, _ = self.run_submission(args, req)
        self.assertEqual(result.summary.hard_requirements, ()); self.assertEqual(result.summary.core_goals, ())

    def test_map_tool_is_not_available_and_model_can_correct_to_submit(self):
        response = call({}, "map-attempt")
        response.message["tool_calls"][0]["function"]["name"] = "maps_text_search"
        model = Model(response, call(submission(), "correct"), final())
        with patch("app.agents.native_requirement_interpreter.get_agent_llm", side_effect=AssertionError("禁止默认真实客户端")):
            NativeRequirementInterpreter(llm=model).run(request())
        self.assertEqual(json.loads(model.requests[1]["messages"][-1]["content"])["error"]["code"], "UNKNOWN_TOOL")

    def test_repeated_invalid_outputs_stop_at_technical_limit(self):
        model = Model(call({}, "bad-1"), call({}, "bad-2"), call({}, "bad-3"))
        with self.assertRaises(NativeToolLoopError):
            NativeRequirementInterpreter(llm=model, max_model_calls=2).run(request())
        self.assertEqual(len(model.requests), 2)


class RequirementIntegrationTests(unittest.TestCase):
    """消费者用固定事实/响应验证接口，仍不需要其他测试模块或真实服务。"""
    @staticmethod
    def lodging_fixture():
        from app.services.specified_lodging_service import SpecifiedLodgingResult
        from app.models.planning_v4.facts import FactStore
        service = Mock()
        service.run.side_effect = lambda planning, **kwargs: SpecifiedLodgingResult(planning, FactStore(), (), ())
        return service

    def test_collection_preserves_global_summary_and_avoids_before_first_query(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        from app.services.amap_contract_adapter import AmapContractAdapter
        interpreted = NativeRequirementInterpreter(llm=Model(call(submission()), final())).run(request())
        summary = interpreted.summary
        def named_call(name, args, identifier):
            response = call(args, identifier); response.message["tool_calls"][0]["function"]["name"] = name
            return response
        master = Model(named_call("find_attractions", {"city": "上海", "dates": ["2026-10-20"],
            "objective": "采集外滩", "candidate_target": 1, "hard_requirements": [{"kind": "must_visit", "text": "外滩",
                "source_field": "remarks", "source_quote": "必须去外滩"}]}, "dispatch"), final())
        child = Model(named_call("maps_text_search", {"city": "上海", "keywords": "外滩"}, "query"),
            named_call("submit_attractions", {"selections": [{"poi_id": "bund", "recommendation_reason": "原文必去"}]}, "select"), final())
        maps = Mock(return_value={"result_status": "ok", "pois": [
            {"id": "bund", "name": "外滩", "location": "121.49,31.24", "cityname": "上海市", "typecode": "110000"},
            {"id": "zoo", "name": "动物园", "location": "121.48,31.24", "cityname": "上海市", "typecode": "110000"}],
            "result_source": {"api_version": "v3", "queried_at": "2026-10-13T00:00:00+08:00"}})
        result = NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=master, attraction_llm=child, adapter=AmapContractAdapter(maps),
            specified_lodging_service=self.lodging_fixture()).run(
            request(), requirements=summary, messages=interpreted.protocol.messages, run_id="formal-task")
        self.assertEqual(result.planning_input.requirements, summary)
        self.assertEqual(len(result.planning_input.requirements.hard_requirements), 4)
        self.assertEqual(result.candidates[0].requirement_ids, ())
        self.assertNotIn("zoo", [c.poi_id for c in result.candidates])
        self.assertEqual(master.requests[0]["messages"][:len(interpreted.protocol.messages)], list(interpreted.protocol.messages))
        child_input = json.loads(child.requests[0]["messages"][-1]["content"])
        self.assertEqual(child_input["hard_requirements"], [r.model_dump(mode="json") for r in summary.hard_requirements])

    def test_collection_cannot_add_or_rephrase_requirements_after_summary(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        summary = NativeRequirementInterpreter(llm=Model(call(submission()), final())).run(request()).summary
        args = {"city": "上海", "dates": ["2026-10-20"], "objective": "擅自改写要求", "candidate_target": 1,
            "hard_requirements": [{"kind": "must_visit", "text": "新的外滩要求", "source_field": "remarks", "source_quote": "必须去外滩"}]}
        response = call(args, "dispatch"); response.message["tool_calls"][0]["function"]["name"] = "find_attractions"
        master, child = Model(response, final()), Model()
        collector = NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=master, attraction_llm=child,
                                               specified_lodging_service=self.lodging_fixture())
        result = collector.run(request(), requirements=summary)
        self.assertEqual(result.planning_input.requirements, summary)
        self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["code"], "TASK_REQUIREMENT_OUTSIDE_SUMMARY")
        self.assertEqual(child.requests, [])

    def test_summary_validation_failure_stops_before_collect_and_does_not_finish_progress(self):
        import asyncio
        from app.agents.native_trip_planner import NativeTripPlanner
        updates = []
        async def progress(update): updates.append(update)
        collector = Mock()
        interpreter = NativeRequirementInterpreter(llm=Model(final()))
        with self.assertRaises(NativeToolLoopError) as caught:
            asyncio.run(NativeTripPlanner(requirement_interpreter=interpreter, collector=collector).run_async(
                request(), task_id="trip", plan_id="plan", progress=progress))
        self.assertEqual(caught.exception.code, "MASTER_DID_NOT_SUBMIT_REQUIREMENTS")
        collector.run.assert_not_called()
        self.assertEqual([(u.stage, u.completed) for u in updates], [("planning_input", 0)])

    def test_collection_rejects_foreign_prepared_summary_before_model(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        summary = NativeRequirementInterpreter(llm=Model(call(submission()), final())).run(request()).summary
        master = Model()
        with self.assertRaises(NativeToolLoopError) as caught:
            NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=master).run(request(), requirements=summary.model_copy(update={"request_id":"other"}))
        self.assertEqual(caught.exception.code, "REQUIREMENT_REQUEST_MISMATCH")
        self.assertEqual(master.requests, [])

    def test_one_master_history_complete_summary_and_formal_identity_reach_final_draft(self):
        from datetime import timezone
        from app.agents.native_trip_planner import NativeTripPlanner
        from app.agents.native_multiagent_collection import NativeMultiagentCollectionResult
        from app.agents.native_attraction_agent import NativeCandidateExecution
        from app.models.planning_v4 import CandidateRef, TaskResult
        from app.models.planning_v4.facts import FactStore, FactSource, PlaceFact
        from app.services.native_tool_loop import NativeToolLoopResult
        from app.services.planning_input_builder import build_planning_input, PlanningInputBuildRequest
        from app.services.planning_v4_rules import default_planning_v4_rules
        req = request().model_copy(update={"preferences": (), "transport_preferences": (), "lodging_query": "固定住处",
            "lodging_poi_id": None, "remarks": "必须去外滩；必须素食", "departure_at": datetime.fromisoformat("2026-10-21T15:00:00+08:00")})
        args = {key: [] for key in submission()}
        args["hard_requirements"] = [{"kind": "must_visit", **grounded("外滩", "必须去外滩")},
                                     {"kind": "must_include", **grounded("素食", "必须素食")}]
        collector = Mock()
        def collect(raw_request, **kw):
            planning = build_planning_input(PlanningInputBuildRequest(request=raw_request, requirements=kw["requirements"], rules=default_planning_v4_rules()))
            source = FactSource(tool_call_id="fixed-map", tool_name="maps_text_search", api_version="v3",
                queried_at=datetime(2026, 10, 13, tzinfo=timezone.utc), effective_params={"city": "上海"})
            places, candidates = [], []
            for category, name in (("sightseeing", "外滩"), ("meal", "测试餐馆"), ("lodging", "固定住处")):
                places.append(PlaceFact(fact_id="fact-"+category, poi_id="poi-"+category, name=name,
                    city="上海市", typecode={"sightseeing":"110000","meal":"050100","lodging":"120300"}[category],
                    location={"lng":121.49,"lat":31.24}, source_refs=(source.tool_call_id,)))
                ids = (planning.requirements.hard_requirements[0].requirement_id,) if category == "sightseeing" else ()
                candidates.append(CandidateRef(candidate_id=category, category=category, poi_id=places[-1].poi_id, place_fact_id=places[-1].fact_id,
                    requirement_ids=ids, slot_ids=tuple(s.slot_id for d in planning.days for s in d.slots if s.eligibility=="required" and
                        ((category=="meal" and s.period in ("breakfast","lunch","dinner")) or (category=="sightseeing" and s.period in ("morning","afternoon","evening")))),
                    lodging_day_ids=tuple(d.day_id for d in planning.days) if category=="lodging" else ()))
            facts=FactStore(places=tuple(places),sources=(source,))
            child=NativeCandidateExecution(TaskResult(task_id="fixed-child",run_id=kw["run_id"],context_revision=1,request_id=raw_request.request_id,
                status="complete",stop_reason="target_met",candidates=tuple(candidates)),facts)
            return NativeMultiagentCollectionResult(kw["run_id"],"",tuple(candidates),facts,(child,),(),0,1,planning,
                NativeToolLoopResult("",tuple(kw["messages"]),(),0))
        collector.run.side_effect=collect
        def plan(messages):
            payload=json.loads(next(m["content"] for m in reversed(messages) if m["role"]=="user"))
            output=[]
            for day in payload["planning_input"]["days"]:
                scopes=next(o for o in payload["day_slot_options"] if o["day_id"]==day["day_id"])
                activities=[]
                for slot in (*scopes["available_meal_slot_ids"],*scopes["available_sightseeing_slot_ids"]):
                    candidate=next(c for c in payload["candidates"] if slot in c["applicable_slot_ids"])
                    activities.append({"proposal_ref":slot,"slot_id":slot,"candidate_ref":candidate["candidate_id"],"kind":candidate["category"],
                        "requirement_ids":candidate["requirement_ids"],"recommendation_reason":"固定测试候选；允许重复以验证接口"})
                output.append({"day_id":day["day_id"],"day_date":day["day_date"],"theme":"测试","activities":activities,"lodging":{"candidate_ref":"lodging"}})
            response=call({"days":output},"daily-submit");response.message["tool_calls"][0]["function"]["name"]="submit_daily_plan"
            return response
        model=Model(call(args,"requirement-submit"),final(),plan,final())
        result=NativeTripPlanner(master_llm=model,collector=collector).run(req,task_id="formal-trip",plan_id="formal-plan")
        self.assertEqual(result.repair.status,"complete")
        self.assertEqual(result.draft.source_run_id,"formal-trip");self.assertEqual(result.draft.plan_id,"formal-plan")
        self.assertEqual(len(result.planning_input.requirements.hard_requirements),2)
        self.assertEqual(result.planning_input.requirements,collector.run.call_args.kwargs["requirements"])
        self.assertTrue(any(m.get("tool_calls", [{}])[0].get("function",{}).get("name")=="submit_requirements"
            for m in model.requests[2]["messages"] if m["role"]=="assistant"))
        diet_id=result.planning_input.requirements.hard_requirements[1].requirement_id
        self.assertIn(diet_id,[r.requirement_id for r in result.report.requirements])
        self.assertNotEqual(next(r.status for r in result.report.requirements if r.requirement_id==diet_id),"satisfied")


if __name__ == "__main__": unittest.main()
