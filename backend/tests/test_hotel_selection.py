"""预算和指定酒店局部回归；禁止真实模型与网络。"""
import asyncio
import json
import socket
import unittest
from unittest.mock import Mock, patch
from datetime import datetime

from app.agents.requirement_interpreter import RequirementInterpreter
from app.agents.master_decider import MasterDecider, MasterDecisionContext
from app.agents.daily_plan_generator import DailyPlanGenerator
from app.models.planning_v4.input import TripRequest, TripBudget, RequirementSummary
from app.services.hotel_selection_service import HotelSelectionService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.planning_input_builder import build_planning_input, PlanningInputBuildRequest
from app.services.planning_v4_rules import default_planning_v4_rules
from app.models.planning_v4.decision import TaskSpec
from app.agents.execution import PlanningError


def request():
    return TripRequest(request_id="hotel-offline", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-11T08:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-13T18:00:00+08:00"),
        lodging_query="上海环球港凯悦酒店", lodging_poi_id="hotel-test",
        budget=TripBudget(amount=4000, amount_basis="per_person"), remarks="喜欢吃披萨")


def row(**updates):
    return dict(id="hotel-test", name="上海环球港凯悦酒店", cityname="上海市",
                address="测试地址", location="121.41,31.23", typecode="100100",
                type="住宿服务;宾馆酒店", **updates)


def planning(req, summary=None):
    return build_planning_input(PlanningInputBuildRequest(request=req,
        requirements=summary or RequirementSummary(requirements_id="reqs", request_id=req.request_id),
        rules=default_planning_v4_rules()))


class HotelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def adapter(self, rows):
        runner = Mock(return_value={"status":"1", "pois":rows})
        return AmapContractAdapter(tool_runner=runner), runner

    def test_search_only_real_same_city_hotels_with_coordinates(self):
        rows = [row(), {**row(), "id":"cafe", "typecode":"050000"},
                {**row(), "id":"other", "cityname":"北京市"},
                {**row(), "id":"unknown", "location":""}]
        adapter, runner = self.adapter(rows)
        result = HotelSelectionService(adapter).search("凯悦", "上海")
        self.assertEqual([r['poi_id'] for r in result], ['hotel-test'])
        self.assertEqual(runner.call_args.args[1]['types'], '100000')

    def test_verification_uses_id_and_supplies_sourced_lodging_candidate(self):
        adapter, runner = self.adapter([row()])
        facts, candidates = HotelSelectionService(adapter).verify(request())
        self.assertEqual(runner.call_args.args[0], 'maps_search_detail')
        self.assertEqual(runner.call_args.args[1]['id'], 'hotel-test')
        self.assertEqual(candidates[0].place_fact_id, facts.places[0].fact_id)
        self.assertEqual(candidates[0].requirement_ids, ())
        self.assertEqual(facts.places[0].source_refs, (facts.sources[0].tool_call_id,))
        # 地图名称变化也按已选择ID识别，不因浏览器旧名称丢弃。
        req=request().model_copy(update={'lodging_query':'旧酒店名称'})
        selected=DailyPlanGenerator._unique_specified_lodging(planning(req), candidates, facts, ())
        self.assertEqual(selected, candidates[0])
        context=MasterDecisionContext(planning_input=planning(req),run_id='run',context_revision=1,
            request_id=req.request_id,fact_store=facts,candidates=candidates)
        decider=MasterDecider(); valid=decider._validate_context_data(context)
        messages=decider._messages(context,valid)
        self.assertIn('hotel-test', messages[1]['content'])

    def test_reject_forged_or_wrong_city_or_nonhotel_selection(self):
        for change in ({'id':'other'}, {'cityname':'北京市'}, {'typecode':'050000'}, {'location':''}):
            adapter,_=self.adapter([{**row(),**change}])
            with self.subTest(change=change), self.assertRaises(PlanningError):
                HotelSelectionService(adapter).verify(request())

    def test_historical_hotel_requirement_no_longer_poison_dispatch(self):
        req=request()
        llm=Mock()
        llm.invoke_with_usage.return_value=(json.dumps({'core_goals':['艺术与美食'],
            'hard_requirements':[{'requirement_id':'M1','kind':'must_include','text':'指定酒店',
               'source':{'field':'lodging_query','quote':req.lodging_query}}],
            'preferences':['喜欢吃披萨'],'unknowns':[],'assumptions':[]}),None)
        summary=RequirementInterpreter(llm=llm).interpret(req)
        self.assertEqual(summary.hard_requirements, ())
        self.assertEqual(summary.preferences, ('喜欢吃披萨',))
        sent=json.loads(llm.invoke_with_usage.call_args.args[0][1]['content'])['trip_request']
        self.assertEqual(sent['budget']['amount_basis'],'per_person')
        self.assertEqual(sent['budget']['amount'],'4000')
        self.assertEqual(sent['lodging_query'],req.lodging_query)
        # 纯文本住处也由后端直接核实；主决策不能再派发住宿子任务。
        text_request=req.model_copy(update={'lodging_poi_id':None})
        pi=planning(text_request,summary); context=MasterDecisionContext(planning_input=pi,run_id='run',
            context_revision=1,request_id=req.request_id)
        decider=MasterDecider();v=decider._validate_context_data(context)
        with self.assertRaises(PlanningError) as raised:
            decider._validate_task_spec(TaskSpec(local_ref='hotel',role='lodging',objective='核实指定酒店',
                day_ids=(pi.days[0].day_id,)),context,v,{d.day_id:d for d in pi.days},
                {s.slot_id:s for d in pi.days for s in d.slots},{},{})
        self.assertEqual(raised.exception.code, "MASTER_LODGING_SPECIFIED")

    def test_legacy_budget_not_reinterpreted_and_new_budget_round_trips(self):
        self.assertEqual(TripBudget(amount=4000).amount_basis,'whole_party')
        self.assertEqual(TripRequest.model_validate_json(request().model_dump_json()).budget.amount_basis,'per_person')

    def test_pipeline_passes_verified_hotel_to_initial_dispatch_without_lodging_agent(self):
        from types import SimpleNamespace
        from app.services.planning_v4_pipeline import PlanningV4Pipeline, PlanningV4PipelineError
        req = request()
        adapter, runner = self.adapter([row()])
        real_service = HotelSelectionService(adapter)
        captured = []
        class StopAtDispatch(Exception):
            pass
        class Dispatch:
            async def run_async(self, context):
                captured.append(context)
                raise StopAtDispatch()
        interpreter = SimpleNamespace(interpret=lambda request, **kw: RequirementSummary(
            requirements_id="summary", request_id=request.request_id))
        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _: interpreter,
            current_route_service_factory=lambda: SimpleNamespace(create_route_context=lambda *a: None),
            dispatch_service_factory=lambda _: Dispatch(),
        )
        with patch('app.services.hotel_selection_service.HotelSelectionService', return_value=real_service):
            with self.assertRaises(PlanningV4PipelineError):
                asyncio.run(pipeline.run(req, task_id="offline", plan_id="plan", progress=lambda _: None))
        self.assertEqual(len(captured), 1)
        context = captured[0]
        self.assertEqual(context.candidate_refs[0].poi_id, req.lodging_poi_id)
        self.assertEqual(context.fact_store.places[0].name, req.lodging_query)
        self.assertEqual(runner.call_count, 1)
        master = MasterDecisionContext(planning_input=context.planning_input,run_id="offline",
            context_revision=1,request_id=req.request_id,fact_store=context.fact_store,
            candidates=context.candidate_refs)
        self.assertNotIn('lodging', MasterDecider._decision_schema(master)['$defs']['TaskSpec']['properties']['role']['enum'])

    def test_hotel_api_returns_selectable_results_and_safe_errors(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes.poi import router
        app=FastAPI(); app.include_router(router)
        client=TestClient(app)
        adapter,_=self.adapter([row()])
        with patch('app.services.hotel_selection_service.HotelSelectionService', return_value=HotelSelectionService(adapter)):
            response=client.get('/poi/hotels',params={'keywords':'凯悦','city':'上海'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['items'][0]['poi_id'],'hotel-test')
        with patch('app.services.hotel_selection_service.HotelSelectionService',side_effect=RuntimeError('private-provider-error')):
            response=client.get('/poi/hotels',params={'keywords':'凯悦','city':'上海'})
            self.assertEqual(response.status_code,503)
            self.assertNotIn('private-provider-error',response.text)
        self.assertEqual(client.get('/poi/hotels',params={'keywords':'','city':'上海'}).status_code,422)
