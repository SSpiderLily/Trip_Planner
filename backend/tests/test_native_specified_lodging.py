"""指定住宿单模块测试；地图、主模型和子模型均为本文件的模拟响应。"""
from datetime import datetime
import json
from threading import Event
import unittest
from unittest.mock import Mock

from app.agents.execution import PlanningError
from app.models.planning_v4 import TripRequest, RequirementSummary, FactStore
from app.models.planning_v4.input import LodgingChange
from app.services.native_weather_service import NativeWeatherService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.planning_lodging_policy import lodging_stays, choose_lodging_for_stay
from app.services.specified_lodging_service import SpecifiedLodgingService
from app.services.llm_service import NativeLLMCompletion
from app.services.native_tool_loop import NativeToolLoopError


def weather_fixture():
    return NativeWeatherService(adapter=AmapContractAdapter(lambda *_: {"result_status": "empty", "city": "上海", "forecasts": []}))


def request(**updates):
    values=dict(request_id="lodging-fixture",city="上海",arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-22T15:00:00+08:00"),lodging_query="旧酒店名称",lodging_poi_id="fixed-hotel")
    return TripRequest(**{**values,**updates})


def planning(req=None, changes=()):
    req=req or request()
    return build_planning_input(PlanningInputBuildRequest(request=req,requirements=RequirementSummary(
        requirements_id="fixture-summary",request_id=req.request_id,lodging_changes=tuple(changes)),rules=default_planning_v4_rules()))


def poi(identifier="fixed-hotel", **updates):
    return {"id":identifier,"name":"新酒店名称","location":"121.49,31.24","cityname":"上海市", "typecode":"100100", **updates}


def raw(rows):
    return {"result_status":"ok","pois":rows,"result_source":{"api_version":"v3","queried_at":"2026-10-13T00:00:00+08:00"}}


def change(date, query=None, city=None, quote=None):
    return LodgingChange(start_date=date, lodging_query=query, city=city, source={"field":"remarks","quote":quote})


class SpecifiedLodgingTests(unittest.TestCase):
    def service(self, responses):
        mapper=Mock(side_effect=responses if isinstance(responses,list) else responses)
        return SpecifiedLodgingService(adapter=AmapContractAdapter(mapper)),mapper

    def test_selected_id_is_authoritative_even_when_name_changes_and_reused_across_days(self):
        source=planning();original=source.model_dump(mode="json")
        service,maps=self.service([raw([poi()])])
        result=service.run(source)
        self.assertEqual(maps.call_count,1); self.assertEqual(maps.call_args.args[0],"maps_search_detail")
        self.assertEqual(maps.call_args.args[1]["id"],"fixed-hotel")
        self.assertEqual(len(result.candidates),1)
        self.assertEqual(result.candidates[0].lodging_day_ids,tuple(d.day_id for d in source.days))
        self.assertIsNone(result.candidates[0].area_hint_id)
        self.assertEqual(result.stays[0].status,"verified")
        place=result.fact_store.places[0]
        self.assertEqual(place.name,"新酒店名称"); self.assertEqual(place.source_refs,(result.fact_store.sources[0].tool_call_id,))
        self.assertEqual(source.model_dump(mode="json"),original)
        for stay in lodging_stays(source):
            self.assertEqual(choose_lodging_for_stay(source,stay,result.candidates,result.fact_store,()).poi_id,"fixed-hotel")

    def test_selected_poi_wrong_id_city_category_or_missing_coordinates_is_rejected(self):
        for updates in ({"id":"other"},{"cityname":"北京市"},{"typecode":"050100"},{"location":""}):
            with self.subTest(updates=updates):
                service,maps=self.service([raw([poi(**updates)])])
                with self.assertRaises(PlanningError) as caught:service.run(planning())
                self.assertEqual(caught.exception.code,"HOTEL_SELECTION_INVALID")
                self.assertEqual(maps.call_count,1)

    def test_unique_text_residence_without_hotel_type_filter(self):
        service,maps=self.service([raw([poi(name="明确住宅",typecode="120300")])])
        result=service.run(planning(request(lodging_poi_id=None,lodging_query="明确住宅")))
        self.assertEqual(result.stays[0].status,"verified")
        self.assertEqual(maps.call_args.args[0],"maps_text_search")
        self.assertEqual(maps.call_args.args[1]["keywords"],"明确住宅")
        self.assertNotIn("types",maps.call_args.args[1]); self.assertEqual(maps.call_count,1)

    def test_address_match_is_valid_and_no_price_only_detail_query(self):
        service,maps=self.service([raw([poi(name="住宅楼",address="测试路100号",typecode="120300")])])
        result=service.run(planning(request(lodging_poi_id=None,lodging_query="测试路100号")))
        self.assertEqual(result.stays[0].status,"verified"); self.assertIsNone(result.fact_store.places[0].cost.value)
        self.assertEqual(maps.call_count,1)

    def test_multiple_same_name_pois_remain_ambiguous_without_choosing_one(self):
        for second in (poi("second",name="同名酒店"),poi("second",name="同名酒店",location="")):
            service,maps=self.service([raw([poi(name="同名酒店"),second])])
            result=service.run(planning(request(lodging_poi_id=None,lodging_query="同名酒店")))
            self.assertEqual(result.stays[0].status,"ambiguous");self.assertEqual(result.candidates,())
            self.assertEqual(len(result.fact_store.places),2);self.assertEqual(maps.call_count,1)

    def test_duplicate_rows_for_same_poi_are_not_multiple_choices(self):
        row=poi(name="同名酒店")
        service,maps=self.service([raw([row,row])])
        result=service.run(planning(request(lodging_poi_id=None,lodging_query="同名酒店")))
        self.assertEqual(result.stays[0].status,"verified");self.assertEqual(len(result.candidates),1)

    def test_single_text_match_missing_coordinate_can_use_same_poi_detail(self):
        service,maps=self.service([raw([poi(name="具体住处",location="")]),raw([poi(name="具体住处")])])
        result=service.run(planning(request(lodging_poi_id=None,lodging_query="具体住处")))
        self.assertEqual(result.stays[0].status,"verified");self.assertEqual(maps.call_count,2)
        self.assertEqual(maps.call_args.args[1]["id"],"fixed-hotel")
        self.assertEqual(len(result.fact_store.sources),2)

    def test_detail_cannot_change_text_match_identity_or_city(self):
        for updates in ({"id":"other"},{"cityname":"北京市"},{"name":"其他住处"}):
            service,maps=self.service([raw([poi(name="具体住处",location="")]),raw([poi(name="具体住处",**updates)])] if "name" not in updates else
                [raw([poi(name="具体住处",location="")]),raw([poi(**updates)])])
            result=service.run(planning(request(lodging_poi_id=None,lodging_query="具体住处")))
            self.assertEqual(result.stays[0].status,"unverified"); self.assertEqual(result.candidates,())

    def test_region_wrong_city_empty_and_provider_error_stay_unverified(self):
        responses=(raw([poi(name="指定地点",typecode="190100")]),raw([poi(name="指定地点",cityname="北京市")]),raw([]),
            {"result_status":"error","result_error":{"category":"provider_error"}})
        for response in responses:
            service,maps=self.service([response])
            result=service.run(planning(request(lodging_poi_id=None,lodging_query="指定地点")))
            self.assertEqual(result.stays[0].status,"unverified");self.assertEqual(result.candidates,())
            self.assertEqual(maps.call_count,1)

    def test_no_specified_stay_does_not_call_hotel_verifier(self):
        hotel=Mock();hotel.verify.side_effect=AssertionError("不应核实未指定住宿")
        result=SpecifiedLodgingService(hotel_service=hotel).run(planning(request(lodging_poi_id=None,lodging_query=None)))
        self.assertEqual(result.stays[0].status,"not_specified");self.assertEqual(result.candidates,())
        hotel.verify.assert_not_called()

    def test_repeated_named_stay_reuses_query_but_has_separate_date_ranges(self):
        req=request(lodging_poi_id=None,lodging_query="住处A",remarks="21日换到住处B，22日回住处A")
        source=planning(req,(change("2026-10-21","住处B",quote="21日换到住处B"),change("2026-10-22","住处A",quote="22日回住处A")))
        service,maps=self.service([raw([poi("a",name="住处A")]),raw([poi("b",name="住处B")])])
        result=service.run(source)
        self.assertEqual(maps.call_count,2);self.assertEqual([c.poi_id for c in result.candidates],["a","b","a"])
        self.assertEqual(len({c.candidate_id for c in result.candidates}),3)
        self.assertEqual([c.lodging_day_ids for c in result.candidates],[("day-2026-10-20",),("day-2026-10-21",),("day-2026-10-22",)])
        self.assertEqual(len(result.fact_store.sources),2)
        for stay in lodging_stays(source):
            selected=choose_lodging_for_stay(source,stay,result.candidates,result.fact_store,())
            self.assertEqual(selected.lodging_day_ids,stay.day_ids)

    def test_explicit_city_change_uses_new_city_and_original_new_lodging(self):
        req=request(lodging_poi_id=None,lodging_query="上海住处",remarks="21日去杭州，住杭州住处")
        source=planning(req,(change("2026-10-21","杭州住处","杭州",quote="21日去杭州，住杭州住处"),))
        service,maps=self.service([raw([poi("a",name="上海住处")]),raw([poi("b",name="杭州住处",cityname="杭州市")])])
        result=service.run(source)
        self.assertEqual([c.args[1]["city"] for c in maps.call_args_list],["上海","杭州"])
        self.assertEqual(result.candidates[1].lodging_day_ids,("day-2026-10-21","day-2026-10-22"))

    def test_change_without_new_name_does_not_reuse_original_selected_hotel(self):
        source=planning(request(remarks="21日换住处"),(change("2026-10-21",quote="21日换住处"),))
        service,maps=self.service([raw([poi()])])
        result=service.run(source)
        self.assertEqual(maps.call_count,1);self.assertEqual([s.status for s in result.stays],["verified","not_specified"])
        self.assertEqual(result.candidates[0].lodging_day_ids,("day-2026-10-20",))

    def test_cancel_before_or_after_query_never_returns_late_verified_candidates(self):
        event=Event();event.set();service,maps=self.service([])
        with self.assertRaises(PlanningError) as caught:service.run(planning(),cancel_event=event)
        self.assertEqual(caught.exception.code,"SPECIFIED_LODGING_CANCELLED");maps.assert_not_called()
        event.clear()
        def mapper(*args):event.set();return raw([poi()])
        service,maps=self.service(mapper)
        with self.assertRaises(PlanningError):service.run(planning(),cancel_event=event)
        self.assertEqual(maps.call_count,1)

    def test_bad_injected_candidate_or_source_cannot_seed_lodging(self):
        service,_=self.service([raw([poi()])]);good=service.run(planning())
        for candidate in (good.candidates[0].model_copy(update={"poi_id":"other"}),good.candidates[0].model_copy(update={"area_hint_id":"invented"})):
            hotel=Mock();hotel.verify.return_value=(good.fact_store,(candidate,))
            with self.assertRaises(PlanningError) as caught:SpecifiedLodgingService(hotel_service=hotel).run(planning())
            self.assertEqual(caught.exception.code,"SPECIFIED_LODGING_RESULT_INVALID")
        badfacts=good.fact_store.model_copy(update={"sources":()})
        hotel=Mock();hotel.verify.return_value=(badfacts,good.candidates)
        with self.assertRaises(PlanningError):SpecifiedLodgingService(hotel_service=hotel).run(planning())

    def test_injected_service_cannot_choose_one_of_ambiguous_name_matches(self):
        service,_=self.service([raw([poi(name="同名酒店")])])
        source=planning(request(lodging_poi_id=None,lodging_query="同名酒店"))
        good=service.run(source)
        other=good.fact_store.places[0].model_copy(update={"fact_id":"other-fact","poi_id":"other-poi"})
        facts=good.fact_store.model_copy(update={"places":(*good.fact_store.places,other)})
        hotel=Mock();hotel.verify.return_value=(facts,good.candidates)
        with self.assertRaises(PlanningError) as caught:SpecifiedLodgingService(hotel_service=hotel).run(source)
        self.assertEqual(caught.exception.code,"SPECIFIED_LODGING_RESULT_INVALID")

    def test_private_exception_is_not_exposed(self):
        hotel=Mock();hotel.verify.side_effect=RuntimeError("private credential detail")
        with self.assertRaises(PlanningError) as caught:SpecifiedLodgingService(hotel_service=hotel).run(planning())
        self.assertEqual(caught.exception.code,"SPECIFIED_LODGING_QUERY_FAILED")
        self.assertNotIn("private",str(caught.exception))

    def test_query_cache_is_not_shared_between_runs(self):
        service,maps=self.service([raw([poi()]),raw([poi()])])
        first=service.run(planning());second=service.run(planning(request(request_id="other")))
        self.assertEqual(maps.call_count,2)
        self.assertNotEqual(first.fact_store.sources[0].tool_call_id,second.fact_store.sources[0].tool_call_id)
        self.assertEqual(second.planning_input.request.request_id,"other")


def tool_call(name,args,identifier):
    return NativeLLMCompletion({"role":"assistant","content":None,"tool_calls":[{"id":identifier,"type":"function",
        "function":{"name":name,"arguments":json.dumps(args,ensure_ascii=False)}}]},None,"tool_calls")


def final():return NativeLLMCompletion({"role":"assistant","content":"已提交"},None,"stop")


def user_payload(messages):
    return json.loads(next(m["content"] for m in reversed(messages) if m["role"]=="user"))


class Model:
    def __init__(self,*responses):self.responses,self.requests=list(responses),[]
    def invoke_native(self,messages,*,tools,**kwargs):
        self.requests.append({"messages":messages,"tools":tools})
        response=self.responses.pop(0)
        return response(messages) if callable(response) else response


def sight_args():
    return {"city":"上海","dates":["2026-10-20","2026-10-21","2026-10-22"],"objective":"获取测试公园候选","candidate_target":1}


class LodgingIntegrationTests(unittest.TestCase):
    def collector(self,master,mapper=None,**kwargs):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        child=Model(tool_call("maps_text_search",{"city":"上海","keywords":"测试公园"},"sight-query"),
            tool_call("submit_attractions",{"selections":[{"poi_id":"park","recommendation_reason":"测试景点"}]},"sight-submit"),final())
        lodging=Model()
        maps=Mock(side_effect=mapper or (lambda name,args:raw([poi()] if name=="maps_search_detail" else [poi("park",name="测试公园",typecode="110000")])))
        return NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=master,attraction_llm=child,lodging_llm=lodging,adapter=AmapContractAdapter(maps),**kwargs),maps,lodging

    def test_fixed_poi_seeds_collection_and_no_lodging_subagent_tool_is_open(self):
        master=Model(tool_call("find_attractions",sight_args(),"sights"),final())
        collector,maps,lodging=self.collector(master)
        result=collector.run(request(),run_id="formal-trip")
        self.assertEqual({c.category for c in result.candidates},{"lodging","sightseeing"})
        self.assertEqual([c.args[0] for c in maps.call_args_list],["maps_search_detail","maps_text_search"])
        self.assertEqual(lodging.requests,[])
        self.assertNotIn("recommend_lodging",[t["function"]["name"] for t in master.requests[0]["tools"]])
        self.assertEqual(result.specified_lodging[0].status,"verified")
        self.assertEqual([c.task_result.run_id for c in result.children],["formal-trip"])
        self.assertEqual(user_payload(master.requests[0]["messages"])["specified_lodging"][0]["lodging_poi_id"],"fixed-hotel")
        self.assertEqual(user_payload(master.requests[0]["messages"])["verified_lodging_places"][0]["name"],"新酒店名称")

    def test_repair_continuation_preserves_seeded_candidate_id_without_reverification(self):
        master=Model(tool_call("find_attractions",sight_args(),"sights"),final(),final())
        collector,maps,_=self.collector(master)
        first=collector.run(request(),run_id="formal-trip")
        second=collector.run(request(),run_id="formal-trip",initial_state=first,messages=first.protocol.messages)
        self.assertEqual(next(c for c in first.candidates if c.category=="lodging"),next(c for c in second.candidates if c.category=="lodging"))
        self.assertEqual(first.specified_lodging,second.specified_lodging);self.assertEqual(maps.call_count,2)

    def test_ambiguous_name_cannot_enable_recommendation_or_choose_subagent_alternative(self):
        master=Model(tool_call("recommend_lodging",{"city":"上海","dates":["2026-10-20"],"objective":"另选住处",
            "candidate_target":1,"anchor_poi_id":None},"try-replace"),final())
        collector,maps,lodging=self.collector(master,mapper=lambda *args:raw([poi("a",name="同名住处"),poi("b",name="同名住处")]))
        result=collector.run(request(lodging_poi_id=None,lodging_query="同名住处"))
        self.assertEqual(result.candidates,());self.assertEqual(result.specified_lodging[0].status,"ambiguous")
        self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["error"]["code"],"UNKNOWN_TOOL")
        self.assertEqual(lodging.requests,[]);self.assertEqual(maps.call_count,1)

    def test_invalid_selected_poi_stops_before_master_dispatch(self):
        master=Model()
        collector,maps,lodging=self.collector(master,mapper=lambda *args:raw([poi("forged")]))
        with self.assertRaises(NativeToolLoopError) as caught:collector.run(request())
        self.assertEqual(caught.exception.code,"HOTEL_SELECTION_INVALID")
        self.assertEqual(master.requests,[]);self.assertEqual(lodging.requests,[]);self.assertEqual(maps.call_count,1)

    def test_foreign_verification_input_rejected_before_dispatch(self):
        from app.services.specified_lodging_service import SpecifiedLodgingResult
        service=Mock()
        service.run.return_value=SpecifiedLodgingResult(planning(request(request_id="other")),FactStore(),(),())
        master=Model();collector,_,_=self.collector(master,specified_lodging_service=service)
        with self.assertRaises(NativeToolLoopError) as caught:collector.run(request())
        self.assertEqual(caught.exception.code,"SPECIFIED_LODGING_INPUT_MISMATCH");self.assertEqual(master.requests,[])

    def test_changed_unspecified_stay_recommends_different_hotel_and_binds_full_range(self):
        master=Model(tool_call("find_attractions",sight_args(),"sights"),tool_call("recommend_lodging",{
            "city":"上海","dates":["2026-10-21"],"objective":"为换住处后的范围推荐新酒店","candidate_target":1,
            "anchor_poi_id":"park"},"new-hotel-task"),final())
        def lodging_query(messages):
            scope=user_payload(messages)["area_scopes"][0]["scope_id"]
            return tool_call("maps_around_search",{"scope_id":scope,"keywords":"酒店"},"new-hotel-query")
        def lodging_submit(messages):
            scope=user_payload(messages)["area_scopes"][0]["scope_id"]
            return tool_call("submit_lodging",{"selections":[{"poi_id":"new-hotel","area_hint_id":scope,
                "recommendation_reason":"核实的新酒店"}]},"new-hotel-submit")
        def mapper(name,args):
            if name=="maps_search_detail":return raw([poi()])
            if name=="maps_around_search":return raw([poi(),poi("new-hotel",name="另一家酒店")])
            return raw([poi("park",name="测试公园",typecode="110000")])
        collector,maps,_=self.collector(master,mapper=mapper)
        lodging=Model(lodging_query,lodging_submit,final());collector.models["lodging"]=lodging
        req=request(remarks="21日换住处")
        summary=planning(req,(change("2026-10-21",quote="21日换住处"),)).requirements
        result=collector.run(req,requirements=summary)
        selected=next(c for c in result.candidates if c.poi_id=="new-hotel")
        self.assertEqual(selected.lodging_day_ids,("day-2026-10-21","day-2026-10-22"))
        self.assertEqual(user_payload(lodging.requests[0]["messages"])["original_lodging_query"],None)
        self.assertEqual(next(c for c in result.candidates if c.poi_id=="fixed-hotel").lodging_day_ids,("day-2026-10-20",))
        self.assertEqual(maps.call_count,3)

    def test_recommendation_only_allowed_for_unspecified_stay_and_cannot_mix_ranges(self):
        master=Model(tool_call("recommend_lodging",{"city":"上海","dates":["2026-10-20"],"objective":"替换原酒店",
            "candidate_target":1},"specified"),tool_call("recommend_lodging",{"city":"上海","dates":["2026-10-20","2026-10-21"],
            "objective":"混合范围","candidate_target":1},"mixed"),final())
        req=request(remarks="21日换住处")
        summary=planning(req,(change("2026-10-21",quote="21日换住处"),)).requirements
        collector,maps,lodging=self.collector(master)
        result=collector.run(req,requirements=summary)
        self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["code"],"LODGING_ALREADY_SPECIFIED")
        self.assertEqual(json.loads(master.requests[2]["messages"][-1]["content"])["data"]["code"],"TASK_SCOPE_INVALID")
        self.assertEqual(lodging.requests,[]);self.assertEqual(maps.call_count,1)
        self.assertEqual(next(c.lodging_day_ids for c in result.candidates if c.category=="lodging"),("day-2026-10-20",))

    def test_native_planner_uses_fixed_hotel_in_every_day_with_formal_identity(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        from app.agents.native_trip_planner import NativeTripPlanner
        empty={key:[] for key in ("core_goals","hard_requirements","lodging_changes","preferences","unknowns","assumptions")}
        def dining_task(messages):
            payload=user_payload(messages)
            meals=[{"date":day["day_date"],"period":slot["period"],"anchor_poi_id":"park"} for day in payload["day_slots"]
                for slot in day["slots"] if slot["eligibility"]=="required" and slot["period"] in ("breakfast","lunch","dinner")]
            return tool_call("find_restaurants",{"city":"上海","dates":[d["day_date"] for d in payload["day_slots"]],
                "objective":"核实各餐次","candidate_target":1,"meals":meals},"dining-task")
        def dining_query(messages):
            payload=user_payload(messages)
            return tool_call("maps_around_search",{"scope_id":payload["meal_scopes"][0]["scope_id"],"keywords":"餐馆"},"food-query")
        def dining_submit(messages):
            payload=user_payload(messages)
            return tool_call("submit_restaurants",{"selections":[{"poi_id":"food","slot_ids":[s["scope_id"] for s in payload["meal_scopes"]],
                "recommendation_reason":"固定测试餐馆"}]},"food-submit")
        def daily_plan(messages):
            payload=user_payload(messages);hotel=next(c for c in payload["candidates"] if c["category"]=="lodging")
            output=[]
            for day in payload["planning_input"]["days"]:
                scopes=next(o for o in payload["day_slot_options"] if o["day_id"]==day["day_id"])
                activities=[]
                for slot in (*scopes["available_meal_slot_ids"],*scopes["available_sightseeing_slot_ids"]):
                    candidate=next(c for c in payload["candidates"] if slot in c["applicable_slot_ids"])
                    activities.append({"proposal_ref":slot,"slot_id":slot,"candidate_ref":candidate["candidate_id"],"kind":candidate["category"],
                        "recommendation_reason":"固定候选；重复仅用于接口验证"})
                output.append({"day_id":day["day_id"],"day_date":day["day_date"],"theme":"测试","activities":activities,
                    "lodging":{"candidate_ref":hotel["candidate_id"]}})
            return tool_call("submit_daily_plan",{"days":output},"daily-plan")
        master=Model(tool_call("submit_requirements",empty,"requirements"),
            tool_call("find_attractions",sight_args(),"sights"),dining_task,final(),daily_plan,final())
        sight=Model(tool_call("maps_text_search",{"city":"上海","keywords":"测试公园"},"sight-query"),
            tool_call("submit_attractions",{"selections":[{"poi_id":"park","recommendation_reason":"测试景点"}]},"sight-submit"),final())
        dining=Model(dining_query,dining_submit,final());lodging=Model()
        def mapper(name,args):
            return raw([poi()] if name=="maps_search_detail" else [poi("food",name="测试餐馆",typecode="050100")] if name=="maps_around_search"
                else [poi("park",name="测试公园",typecode="110000")])
        maps=Mock(side_effect=mapper)
        collector=NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=master,attraction_llm=sight,dining_llm=dining,lodging_llm=lodging,adapter=AmapContractAdapter(maps))
        result=NativeTripPlanner(master_llm=master,collector=collector).run(request(),task_id="trip-formal",plan_id="plan-formal")
        self.assertEqual(result.repair.status,"complete")
        self.assertEqual(result.draft.plan_id,"plan-formal");self.assertEqual(result.draft.source_run_id,"trip-formal")
        self.assertTrue(all(d.lodging.poi_id=="fixed-hotel" and d.lodging.selection_source=="user" for d in result.draft.days))
        self.assertEqual(lodging.requests,[])
        self.assertEqual(sum(c.args[0]=="maps_search_detail" for c in maps.call_args_list),1);self.assertEqual(maps.call_count,3)


if __name__ == "__main__":unittest.main()
