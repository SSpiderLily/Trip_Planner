"""原生主→景点→MCP候选闭环，全部使用模拟模型与地图返回。"""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime
import json
import io
import sys
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import Mock, patch

from app.agents.native_attraction_collection import NativeAttractionCollection
from app.models.planning_v4.input import TripRequest
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.llm_service import NativeLLMCompletion
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.native_chain_trace import NativeChainTrace, TracedNativeModel
from app.services.native_tool_loop import NativeToolLoopError


def request():
    return TripRequest(request_id="native-fixture", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T08:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-21T20:00:00+08:00"),
        preferences=("公园",), remarks="必须去外滩")


def task_args(**changes):
    data = {"city": "上海", "dates": ["2026-10-20"], "objective": "核实外滩并获取候选",
            "preferences": ["公园"], "constraints": [], "candidate_target": 1,
            "hard_requirements": [{"kind": "must_visit", "text": "外滩",
                "source_field": "remarks", "source_quote": "必须去外滩"}]}
    return {**data, **changes}


def call(name, args, call_id="call-1"):
    return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name,
                                                         "arguments": json.dumps(args, ensure_ascii=False)}}
    ]}, None, "tool_calls")


def final(content):
    return NativeLLMCompletion({"role": "assistant", "content": content}, None, "stop")


def choice(poi="p1"):
    return final(json.dumps({"selections": [{"poi_id": poi, "recommendation_reason": "符合城市漫步偏好"}],
                            "unmet_needs": []}, ensure_ascii=False))


def poi(poi_id="p1", **changes):
    return {"id": poi_id, "name": "外滩", "location": "121.4903,31.2417", "cityname": "上海市",
            "type": "风景名胜", "typecode": "110000", **changes}


def raw(places=None, status="ok"):
    places = [poi()] if places is None else places
    return {"result_status": status, "pois": places, "count": str(len(places)),
            "result_source": {"api_version": "v3", "queried_at": "2026-10-12T00:00:00+00:00"}}


class FakeModel:
    model = "fixture"
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def invoke_native(self, messages, **options):
        self.requests.append({"messages": deepcopy(messages), **deepcopy(options)})
        response = self.responses.pop(0)
        return response(messages) if callable(response) else response


class NativeAttractionCollectionTests(unittest.TestCase):
    def test_live_script_keeps_stderr_file_descriptor_available_to_mcp(self):
        from scripts import run_native_attraction_collection as script
        llm = FakeModel()
        settings = SimpleNamespace(llm_api_key="fixture-key", openai_api_key="", amap_api_key="fixture-map")
        def run(*args, **kwargs):
            sys.stderr.fileno()  # FastMCP 的stdio子进程在真实启动边界使用这个描述符。
            return SimpleNamespace(children=(SimpleNamespace(task_result=SimpleNamespace(status="complete")),),
                candidates=(SimpleNamespace(model_dump=lambda **kw: {}),), run_id="fixture-run", model_calls=2,
                fact_store=SimpleNamespace(places=(), sources=()))
        with patch.object(script, "get_agent_llm", return_value=llm), \
                patch.object(script, "get_settings", return_value=settings), \
                patch.object(script.NativeAttractionCollection, "run", side_effect=run), \
                patch.object(sys, "argv", ["script", "--live"]), redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(), 0)

    def test_live_report_preserves_failed_attempt_but_accepts_recovered_candidates(self):
        from scripts import run_native_attraction_collection as script
        settings = SimpleNamespace(llm_api_key="fixture-key", openai_api_key="", amap_api_key="fixture-map")
        result = SimpleNamespace(
            children=(SimpleNamespace(task_result=SimpleNamespace(status="failed")),
                      SimpleNamespace(task_result=SimpleNamespace(status="complete"))),
            candidates=(SimpleNamespace(model_dump=lambda **kw: {}),), run_id="fixture-run", model_calls=3,
            fact_store=SimpleNamespace(places=(), sources=()))
        capture = io.StringIO()
        with patch.object(script, "get_agent_llm", return_value=FakeModel()), \
                patch.object(script, "get_settings", return_value=settings), \
                patch.object(script.NativeAttractionCollection, "run", return_value=result), \
                patch.object(sys, "argv", ["script", "--live"]), redirect_stdout(capture):
            self.assertEqual(script.main(), 0)
        report = json.loads(capture.getvalue().strip())["data"]
        self.assertEqual(report["status"], "candidates_returned")
        self.assertEqual(report["child_statuses"], ["failed", "complete"])

    def setup_chain(self, child, *, master=None, map_return=None, trace=None, executor=None):
        master = master or FakeModel(call("find_attractions", task_args()), final("已核实外滩；价格未知"))
        map_runner = Mock(return_value=map_return if map_return is not None else raw())
        chain = NativeAttractionCollection(master_llm=master, attraction_llm=child,
            adapter=None if executor else AmapContractAdapter(map_runner), tool_executor=executor,
            emit=trace.emit if trace else None)
        return chain, master, map_runner

    def test_native_nested_tool_calls_close_candidate_and_fact_sources(self):
        trace = NativeChainTrace()
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}), choice())
        chain, master, maps = self.setup_chain(child, trace=trace)
        result = chain.run(request())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        place = next(place for place in result.fact_store.places if place.fact_id == candidate.place_fact_id)
        self.assertEqual(place.poi_id, candidate.poi_id)
        self.assertTrue(set(place.source_refs).issubset(source.tool_call_id for source in result.fact_store.sources))
        self.assertIsNone(place.cost.value)
        self.assertIsNone(place.opening_hours_raw)
        self.assertEqual(candidate.slot_ids, ())
        self.assertEqual(result.children[0].task_result.status, "complete")
        self.assertEqual(candidate.requirement_ids, ())
        self.assertEqual(maps.call_args.args[0], "maps_text_search")
        self.assertEqual(maps.call_args.args[1]["citylimit"], "true")
        self.assertEqual(master.requests[0]["tools"][0]["function"]["name"], "find_attractions")
        self.assertEqual([item["function"]["name"] for item in child.requests[0]["tools"]],
                         ["maps_text_search", "maps_search_detail", "submit_attractions"])

        tool_message = master.requests[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(json.loads(tool_message["content"])["data"]["places"][0]["poi_id"], "p1")
        self.assertEqual([event["event"] for event in trace.events],
                         ["collection.start", "master.task", "map.request", "map.result", "child.result", "master.result"])

    def test_executor_adapter_path_invokes_actual_registered_mcp_name(self):
        maps = Mock(return_value=raw())
        executor = MCPToolExecutor(maps)
        self.addCleanup(executor.close)
        chain, _, _ = self.setup_chain(FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
                                              choice()), executor=executor)
        result = chain.run(request())
        self.assertEqual(result.children[0].task_result.status, "complete")
        self.assertEqual(maps.call_args.args[0], "maps_text_search")

    def test_native_submit_accepts_candidates_without_parsing_plain_final_text(self):
        answer = {"selections": [{"poi_id": "p1", "recommendation_reason": "符合城市漫步偏好"}],
                  "unmet_needs": ["价格和营业时间未知"]}
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
                          call("submit_attractions", answer, "submit-1"), final("候选已提交。"))
        chain, master, maps = self.setup_chain(child)
        result = chain.run(request())
        self.assertEqual(result.children[0].task_result.status, "complete")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(json.loads(child.requests[2]["messages"][-1]["content"])["data"]["status"], "accepted")
        self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["unmet_needs"],
                         ["价格和营业时间未知"])
        maps.assert_called_once()

    def test_native_submit_rejects_invented_poi_and_allows_corrected_submission(self):
        def submission(poi, identity):
            return call("submit_attractions", {"selections": [{"poi_id": poi,
                "recommendation_reason": "符合偏好"}], "unmet_needs": []}, identity)
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
                          submission("invented", "submit-1"), submission("p1", "submit-2"), final("已提交"))
        chain, _, _ = self.setup_chain(child)
        result = chain.run(request())
        self.assertEqual(result.candidates[0].poi_id, "p1")
        self.assertEqual(json.loads(child.requests[2]["messages"][-1]["content"])["data"]["code"],
                         "CHILD_SELECTION_INVALID")

    def test_queries_after_accepted_submission_do_not_access_mcp_again(self):
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
            call("submit_attractions", {"selections": [{"poi_id":"p1","recommendation_reason":"符合偏好"}]},
                 "submit-1"),
            call("maps_text_search", {"keywords":"公园","city":"上海"}, "call-2"), final("已提交"))
        chain, _, maps = self.setup_chain(child)
        result = chain.run(request())
        self.assertEqual(len(result.candidates), 1)
        maps.assert_called_once()

    def test_map_tool_contract_preserves_decision_fields_without_duplicating_fact_metadata(self):
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}), choice())
        chain, _, _ = self.setup_chain(child)
        result = chain.run(request())
        payload = json.loads(child.requests[1]["messages"][-1]["content"])["data"]
        self.assertTrue(payload["places"][0]["eligible"])
        self.assertEqual(payload["places"][0]["poi_id"], "p1")
        self.assertNotIn("field_sources", payload["places"][0])
        self.assertNotIn("available_candidates", payload)
        self.assertTrue(result.fact_store.places[0].field_sources)

    def test_main_rejects_foreign_city_date_and_invented_requirement_before_child(self):
        for changes in ({"city": "杭州"}, {"dates": ["2027-01-01"]},
                        {"hard_requirements": [{"kind": "must_visit", "text": "外滩",
                            "source_field": "remarks", "source_quote": "用户没有说过"}]}):
            with self.subTest(changes=changes):
                master = FakeModel(call("find_attractions", task_args(**changes)), final("无法派发"))
                child = FakeModel()
                chain, _, maps = self.setup_chain(child, master=master)
                with self.assertRaisesRegex(NativeToolLoopError, "MASTER_DID_NOT_DELEGATE"):
                    chain.run(request())
                maps.assert_not_called()
                self.assertFalse(child.requests)
                self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["status"],
                                 "rejected")

    def test_child_cannot_query_another_city_or_invent_detail_id(self):
        child = FakeModel(call("maps_text_search", {"keywords": "景点", "city": "杭州"}),
                          call("maps_search_detail", {"poi_id": "invented"}, "call-2"),
                          final('{"selections":[],"unmet_needs":["没有合法候选"]}'))
        chain, _, maps = self.setup_chain(child)
        result = chain.run(request())
        maps.assert_not_called()
        self.assertFalse(result.candidates)
        self.assertEqual(result.children[0].task_result.status, "partial")

    def test_invented_excluded_wrong_city_or_unlocated_candidates_are_rejected(self):
        for selected, place in (("invented", poi()), ("p1", poi(cityname="杭州市")),
                                ("p1", poi(type="餐饮服务", typecode="050000")),
                                ("p1", poi(location=None))):
            with self.subTest(place=place, selected=selected):
                chain, _, _ = self.setup_chain(FakeModel(
                    call("maps_text_search", {"keywords": "外滩", "city": "上海"}), choice(selected)),
                    map_return=raw([place]))
                result = chain.run(request())
                self.assertFalse(result.candidates)
                self.assertEqual(result.children[0].task_result.stop_reason, "invalid_output")

    def test_detail_can_complete_core_coordinate_and_keeps_both_sources(self):
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
                          call("maps_search_detail", {"poi_id": "p1"}, "call-2"), choice())
        chain, _, maps = self.setup_chain(child)
        maps.side_effect = [raw([poi(location=None)]), raw([poi(type=None, typecode=None)])]
        result = chain.run(request())
        self.assertEqual(result.children[0].task_result.status, "complete")
        candidate = result.candidates[0]
        place = next(item for item in result.fact_store.places if item.fact_id == candidate.place_fact_id)
        self.assertEqual(len(place.source_refs), 2)
        self.assertEqual(maps.call_count, 2)

    def test_duplicate_queries_are_cached_and_can_be_followed_by_different_queries(self):
        query = {"keywords": "外滩", "city": "上海"}
        child = FakeModel(call("maps_text_search", query), call("maps_text_search", query, "call-2"),
                          call("maps_text_search", {"keywords": "公园", "city": "上海"}, "call-3"), choice())
        chain, _, maps = self.setup_chain(child)
        result = chain.run(request())
        self.assertEqual(maps.call_count, 2)
        self.assertEqual(json.loads(child.requests[2]["messages"][-1]["content"])["data"]["query_status"],
                         "duplicate_skipped")
        self.assertEqual(len(result.candidates), 1)

    def test_main_can_delegate_followup_from_actual_results_and_reuse_facts(self):
        def followup(messages):
            payload = json.loads(messages[-1]["content"])["data"]
            self.assertEqual(payload["task_result"]["status"], "partial")
            return call("find_attractions", task_args(), "master-2")
        master = FakeModel(call("find_attractions", task_args(candidate_target=2)), followup, final("找到一个候选"))
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}), choice(), choice())
        chain, _, maps = self.setup_chain(child, master=master)
        result = chain.run(request())
        self.assertEqual(len(result.children), 2)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.children[1].task_result.status, "complete")
        maps.assert_called_once()

    def test_empty_and_provider_error_remain_partial_without_invented_places(self):
        for response in (raw([], "empty"), {"result_status": "error", "result_error": {"category": "rate_limit"}}):
            with self.subTest(response=response):
                child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}),
                                  final('{"selections":[],"unmet_needs":["未找到候选"]}'))
                chain, _, _ = self.setup_chain(child, map_return=response)
                result = chain.run(request())
                self.assertFalse(result.candidates)
                self.assertEqual(result.children[0].task_result.status, "partial")
                self.assertTrue(result.children[0].task_result.missing)

    def test_ambiguous_same_name_does_not_claim_must_visit_covered(self):
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}), choice())
        chain, _, _ = self.setup_chain(child, map_return=raw([poi("p1"), poi("p2")]))
        result = chain.run(request())
        self.assertEqual(result.children[0].task_result.stop_reason, "target_met")
        self.assertEqual(result.candidates[0].requirement_ids, ())

    def test_invalid_child_json_and_duplicate_selections_return_failed_not_success(self):
        for output in ('not-json', '{"selections":[{"poi_id":"p1","recommendation_reason":"x"},'
                       '{"poi_id":"p1","recommendation_reason":"x"}]}'):
            with self.subTest(output=output):
                chain, _, _ = self.setup_chain(FakeModel(final(output)))
                result = chain.run(request())
                self.assertEqual(result.children[0].task_result.status, "failed")
                self.assertFalse(result.candidates)

    def test_cancellation_after_map_does_not_merge_results_or_continue_main(self):
        cancel = Event()
        child = FakeModel(call("maps_text_search", {"keywords": "外滩", "city": "上海"}))
        chain, master, maps = self.setup_chain(child)
        def cancel_map(*args):
            cancel.set()
            return raw()
        maps.side_effect = cancel_map
        with self.assertRaisesRegex(NativeToolLoopError, "CANCELLED"):
            chain.run(request(), cancel_event=cancel)
        self.assertEqual(len(master.requests), 1)

    def test_trace_excludes_reasoning_provider_fields_credentials_and_error_text(self):
        trace = NativeChainTrace(secrets=("fixture-private-key",))
        completion = final("查询结果 fixture-private-key")
        completion.message.update(reasoning_content="private-reasoning", provider_state="private-state")
        wrapper = TracedNativeModel(FakeModel(completion), "master", trace)
        wrapper.invoke_native([], tools=[])
        dump = json.dumps(trace.events)
        for secret in ("fixture-private-key", "private-reasoning", "private-state"):
            self.assertNotIn(secret, dump)
        broken = Mock()
        broken.invoke_native.side_effect = RuntimeError("fixture-private-key")
        with self.assertRaises(RuntimeError):
            TracedNativeModel(broken, "attractions", trace).invoke_native([], tools=[])
        self.assertNotIn("fixture-private-key", json.dumps(trace.events))


if __name__ == "__main__":
    unittest.main()
