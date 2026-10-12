"""分轮依赖、并发、角色范围与事实合并，全部模拟。"""
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime
from threading import Barrier, Event
import unittest
from unittest.mock import Mock

from app.agents.native_multiagent_collection import NativeMultiagentCollection, SharedQueryCache, merge_fact_stores
from app.models.planning_v4.facts import FactStore
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.llm_service import NativeLLMCompletion
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_tool_loop import NativeToolLoopError
from test_native_attraction_collection import FakeModel, call, choice, final, poi, raw, request, task_args


def batch(*responses):
    return NativeLLMCompletion({"role":"assistant", "content":None,
        "tool_calls":[c for r in responses for c in r.message["tool_calls"]]}, None, "tool_calls")


def dining_args(**changes):
    return {**task_args(hard_requirements=[], objective="查询晚餐"), "meals":[{
        "date":"2026-10-20", "period":"dinner", "anchor_poi_id":"p1"}], **changes}


def lodging_args(**changes):
    return {**task_args(hard_requirements=[], objective="推荐住宿"), "anchor_poi_id":"p1", **changes}


def spatial_query(messages, expand=False):
    data = json.loads(messages[1]["content"])
    return call("maps_around_search", {"scope_id":data.get("meal_scopes", data.get("area_scopes"))[0]["scope_id"],
        "keywords":"餐馆" if "meal_scopes" in data else "酒店", "expand":expand}, "expand" if expand else "map")


def spatial_submit(messages, poi_id=None):
    data = json.loads(messages[1]["content"])
    if "meal_scopes" in data:
        return call("submit_restaurants", {"selections":[{"poi_id":poi_id or "food",
            "slot_ids":[data["meal_scopes"][0]["scope_id"]], "recommendation_reason":"供应商餐饮类别"}]}, "submit")
    return call("submit_lodging", {"selections":[{"poi_id":poi_id or "hotel",
        "area_hint_id":data["area_scopes"][0]["scope_id"] if data["area_scopes"] else None,
        "recommendation_reason":"供应商住宿类别"}]}, "submit")


def map_data(name, args):
    if name == "maps_text_search":
        return raw()
    dining = args.get("types") == "050000"
    return raw([poi("food" if dining else "hotel", name="餐馆" if dining else "酒店",
        type="餐饮服务;中餐厅" if dining else "住宿服务;宾馆酒店", typecode="050100" if dining else "100100")])


class NativeMultiagentTests(unittest.TestCase):
    def chain(self, master=None, dining=None, lodging=None, mapper=None, emit=None):
        master = master or FakeModel(call("find_attractions", task_args(), "sights"), batch(
            call("find_restaurants", dining_args(), "food-task"),
            call("recommend_lodging", lodging_args(), "hotel-task")), final("三个角色返回；价格未知"))
        dining = dining or FakeModel(spatial_query, spatial_submit, final("提交"))
        lodging = lodging or FakeModel(spatial_query, spatial_submit, final("提交"))
        attraction = FakeModel(call("maps_text_search", {"city":"上海", "keywords":"外滩"}), choice())
        maps = Mock(side_effect=mapper or map_data)
        return NativeMultiagentCollection(master_llm=master, attraction_llm=attraction, dining_llm=dining,
            lodging_llm=lodging, adapter=AmapContractAdapter(maps), emit=emit), master, maps

    def test_rounds_concurrent_overlap_ordered_results_fact_source_closure(self):
        barrier = Barrier(2)
        def mapper(name, args):
            if name == "maps_around_search":
                barrier.wait(2)  # 顺序执行不能通过。
            return map_data(name, args)
        trace = NativeChainTrace()
        chain, master, maps = self.chain(mapper=mapper, emit=trace.emit)
        result = chain.run(request())
        self.assertEqual(result.rounds, 2)
        self.assertEqual([child.task_result.status for child in result.children], ["complete"]*3)
        self.assertEqual({c.category for c in result.candidates}, {"sightseeing","meal","lodging"})
        self.assertEqual((maps.call_count,len(result.fact_store.places),len(result.fact_store.sources)), (3,3,3))
        for candidate in result.candidates:
            place = next(p for p in result.fact_store.places if p.fact_id == candidate.place_fact_id)
            self.assertEqual(place.poi_id, candidate.poi_id)
            self.assertTrue(set((*place.source_refs,*place.field_sources.values())).issubset(
                s.tool_call_id for s in result.fact_store.sources))
            self.assertIsNone(place.cost.value)
        self.assertEqual([m["tool_call_id"] for m in master.requests[2]["messages"] if m["role"]=="tool"],
                         ["sights","food-task","hotel-task"])
        tasks = [e["data"]["task"] for e in trace.events if e["event"]=="master.task"]
        self.assertEqual(tasks[1]["known_fact_ids"],tasks[2]["known_fact_ids"])
        self.assertEqual(tasks[1]["context_revision"],2)
        self.assertTrue(tasks[1]["dependencies"])
        lodging = json.loads(master.requests[2]["messages"][-1]["content"])["data"]
        self.assertEqual(lodging["lodging_dates"]["possible_overnight_dates"], ["2026-10-20"])
        self.assertEqual(lodging["lodging_dates"]["availability_status"], "unverified")

    def test_same_round_dependency_rejected(self):
        master = FakeModel(batch(call("find_attractions",task_args(),"s"),
            call("find_restaurants",dining_args(),"f")), final("缺依赖"))
        chain, _, maps = self.chain(master=master)
        self.assertEqual(len(chain.run(request()).children),1)
        self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["code"],"DEPENDENCY_UNMET")
        self.assertEqual(maps.call_count,1)

    def test_invalid_city_date_and_skipped_meal(self):
        for name,args,code in [("find_restaurants",dining_args(city="北京"),"TASK_SCOPE_INVALID"),
            ("recommend_lodging",lodging_args(dates=["2026-10-22"]),"TASK_SCOPE_INVALID"),
            ("find_restaurants",dining_args(meals=[{"date":"2026-10-20","period":"breakfast","anchor_poi_id":"p1"}]),"MEAL_SCOPE_INVALID")]:
            with self.subTest(code=code):
                master=FakeModel(call("find_attractions",task_args(),"a"),call(name,args,"b"),final("缺项"))
                chain,_,maps=self.chain(master=master)
                self.assertEqual(len(chain.run(request().model_copy(update={"arrival_at": datetime.fromisoformat("2026-10-20T10:00:00+08:00")})).children),1)
                self.assertEqual(json.loads(master.requests[2]["messages"][-1]["content"])["data"]["code"],code)

    def test_duplicate_tasks_same_batch_and_later_round(self):
        for responses in [(batch(call("find_attractions",task_args(),"a"),call("find_attractions",task_args(),"b")),),
                          (call("find_attractions",task_args(),"a"),call("find_attractions",task_args(),"b"))]:
            chain,_,maps=self.chain(master=FakeModel(*responses,final("复用")))
            self.assertEqual(len(chain.run(request()).children),1)
            self.assertEqual(maps.call_count,1)

    def test_failed_child_retains_sibling_facts(self):
        chain,_,_=self.chain(dining=FakeModel(lambda _: (_ for _ in ()).throw(RuntimeError("fixture"))))
        result=chain.run(request())
        self.assertEqual({c.category for c in result.candidates},{"sightseeing","lodging"})
        self.assertEqual([c.task_result.status for c in result.children],["complete","failed","complete"])

    def test_wrong_category_city_radius_rejects_selection(self):
        for changes in [{"typecode":"100100","type":"住宿服务"},{"cityname":"北京市"},{"location":"121.9,31.8"}]:
            with self.subTest(changes=changes):
                def mapper(name,args):
                    if name=="maps_around_search" and args["types"]=="050000":
                        row=poi("food",name="餐馆",type="餐饮服务",typecode="050100")
                        row.update(changes)
                        return raw([row])
                    return map_data(name,args)
                chain,_,_=self.chain(mapper=mapper)
                self.assertNotIn("meal",{c.category for c in chain.run(request()).candidates})

    def test_expand_after_empty_and_reject_when_cost_missing_with_valid_candidate(self):
        for empty in [True,False]:
            dining=FakeModel(spatial_query,lambda m:spatial_query(m,True),spatial_submit,final("提交"))
            def mapper(name,args):
                if empty and name=="maps_around_search" and args["types"]=="050000" and args["radius"]=="1000":
                    return raw([])
                return map_data(name,args)
            chain,_,maps=self.chain(dining=dining,mapper=mapper)
            self.assertIn("meal",{c.category for c in chain.run(request()).candidates})
            radii=[c.args[1]["radius"] for c in maps.call_args_list if c.args[0]=="maps_around_search" and c.args[1]["types"]=="050000"]
            self.assertEqual(radii,["1000","3000"] if empty else ["1000"])
            if not empty:
                self.assertEqual(json.loads(dining.requests[2]["messages"][-1]["content"])["data"]["code"],"RADIUS_EXPANSION_NOT_ALLOWED")

    def test_specified_lodging_independent_query_original_and_residence_accepted(self):
        req=request().model_copy(update={"lodging_query":"上海具体住宅"})
        master=FakeModel(batch(call("find_attractions",task_args(),"a"),
            call("recommend_lodging",lodging_args(anchor_poi_id=None),"h")),final("核实住处"))
        lodging=FakeModel(call("verify_specified_lodging",{}),spatial_submit,final("核实"))
        def mapper(name,args):
            if args.get("keywords")==req.lodging_query:
                self.assertNotIn("types",args)
                return raw([poi("hotel",name=req.lodging_query,typecode="120300",type="商务住宅;住宅区")])
            return raw()
        chain,_,_=self.chain(master=master,lodging=lodging,mapper=mapper)
        result=chain.run(req)
        self.assertEqual({c.category for c in result.candidates},{"sightseeing","lodging"})
        self.assertIsNone(next(c for c in result.candidates if c.category=="lodging").area_hint_id)

    def test_specified_lodging_ambiguous_and_region_not_selected(self):
        for places in [[poi("h1",name="具体住处",typecode="120300"),poi("h2",name="具体住处",typecode="120300")],
                       [poi("h1",name="具体住处",typecode="190100",type="地名地址信息")]]:
            master=FakeModel(call("recommend_lodging",lodging_args(anchor_poi_id=None),"h"),final("歧义"))
            lodging=FakeModel(call("verify_specified_lodging",{}),lambda m:spatial_submit(m,"h1"),final("结束"))
            chain,_,_=self.chain(master=master,lodging=lodging,mapper=lambda *a:raw(places))
            self.assertFalse(chain.run(request().model_copy(update={"lodging_query":"具体住处"})).candidates)

    def test_cancel_parallel_query_before_merge(self):
        event=Event()
        def mapper(name,args):
            if name=="maps_around_search":event.set()
            return map_data(name,args)
        trace=NativeChainTrace()
        chain,_,_=self.chain(mapper=mapper,emit=trace.emit)
        with self.assertRaises(NativeToolLoopError) as raised:chain.run(request(),cancel_event=event)
        self.assertEqual(raised.exception.code,"CANCELLED")
        self.assertEqual(len([e for e in trace.events if e["event"]=="dispatch.round_end"]),1)

    def test_shared_query_singleflight(self):
        cache=SharedQueryCache()
        barrier=Barrier(2)
        query=Mock(return_value="result")
        def get():
            barrier.wait(2)
            return cache.get_or_call("same",query)
        with ThreadPoolExecutor(max_workers=2) as pool:values=list(pool.map(lambda _:get(),range(2)))
        self.assertEqual(query.call_count,1)
        self.assertEqual(sorted(duplicate for _,duplicate in values),[False,True])

    def test_concurrent_trace_sequence_redacts(self):
        trace=NativeChainTrace(secrets=("fixture-secret",))
        with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda i:trace.emit("event",{"i":i,"content":"fixture-secret"}),range(30)))
        self.assertEqual([e["sequence"] for e in trace.events],list(range(1,31)))
        self.assertNotIn("fixture-secret",json.dumps(trace.events))

    def test_named_restaurant_verification_preserves_name_and_unknown_diet_coverage(self):
        req=request().model_copy(update={"remarks":"必须去外滩，必须吃海风餐馆，必须吃素食"})
        requirements=[{"kind":"must_include","text":"海风餐馆","source_field":"remarks",
                       "source_quote":"必须吃海风餐馆","restaurant_name":"海风餐馆"},
                      {"kind":"must_include","text":"素食","source_field":"remarks","source_quote":"必须吃素食"}]
        master=FakeModel(call("find_attractions",task_args(),"a"),
            call("find_restaurants",dining_args(hard_requirements=requirements),"f"),final("饮食未核实"))
        def verify(messages):
            data=json.loads(messages[1]["content"])
            return call("verify_restaurant",{"requirement_id":data["must_eat_restaurants"][0]["requirement_id"]})
        def submit(messages):
            data=json.loads(messages[1]["content"])
            return call("submit_restaurants",{"selections":[{"poi_id":"food",
                "slot_ids":[data["meal_scopes"][0]["scope_id"]],
                "requirement_ids":[data["must_eat_restaurants"][0]["requirement_id"]],
                "recommendation_reason":"与原文店名一致"}]},"submit")
        def mapper(name,args):
            if args.get("keywords")=="海风餐馆":
                self.assertEqual(args["types"],"050000")
                return raw([poi("food",name="海风餐馆",type="餐饮服务",typecode="050100")])
            return raw()
        chain,_,maps=self.chain(master=master,dining=FakeModel(verify,submit,final("提交")),mapper=mapper)
        result=chain.run(req)
        child=result.children[-1].task_result
        self.assertEqual(child.status,"partial")
        self.assertTrue(any(c.status=="unknown" for c in child.coverage))
        self.assertEqual(len(child.candidates[0].requirement_ids),1)

    def test_must_avoid_from_previous_round_filters_dining(self):
        req=request().model_copy(update={"remarks":"必须去外滩，不要去餐馆"})
        args=task_args(hard_requirements=[*task_args()["hard_requirements"],
            {"kind":"must_avoid","text":"餐馆","source_field":"remarks","source_quote":"不要去餐馆"}])
        master=FakeModel(call("find_attractions",args,"a"),call("find_restaurants",dining_args(),"f"),final("被排除"))
        chain,_,_=self.chain(master=master)
        self.assertNotIn("meal",{c.category for c in chain.run(req).candidates})

    def test_bad_source_and_unknown_anchor_do_not_invoke_children(self):
        for args,code in [(dining_args(meals=[{"date":"2026-10-20","period":"dinner","anchor_poi_id":"invented"}]),"DEPENDENCY_UNMET"),
            (dining_args(hard_requirements=[{"kind":"must_include","text":"虚构餐馆","source_field":"remarks","source_quote":"原文不存在"}]),"REQUIREMENT_SOURCE_MISMATCH")]:
            master=FakeModel(call("find_attractions",task_args(),"a"),call("find_restaurants",args,"f"),final("缺项"))
            chain,_,maps=self.chain(master=master)
            self.assertEqual(len(chain.run(request()).children),1)
            self.assertEqual(json.loads(master.requests[2]["messages"][-1]["content"])["data"]["code"],code)
            self.assertEqual(maps.call_count,1)

    def test_fact_id_conflict_rejected(self):
        result=AmapContractAdapter(lambda *a:raw()).search_text(keywords="外滩",city="上海")
        place=result.data.places[0]
        with self.assertRaises(NativeToolLoopError) as raised:merge_fact_stores(
            FactStore(sources=(result.source,),places=(place,)),
            FactStore(sources=(result.source,),places=(place.model_copy(update={"name":"不同地点"}),)))
        self.assertEqual(raised.exception.code,"FACT_ID_CONFLICT")
