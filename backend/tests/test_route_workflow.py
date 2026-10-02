"""新版路线闭环：真实三角色消息调用，地图与模型使用可控替身。"""
import json
import asyncio
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path
from app.agents.route_planner import RouteTripPlanner
from app.agents.execution import PlanningError
from app.models.schemas import TripRequest
from app.services.task_repository import TaskRepository
from app.services.observation_service import ObservationService


def place(identity, name='模型名称', lng=1):
    return {'source_id': identity, 'name': name, 'longitude': lng, 'latitude': 1}


def draft():
    return {'lodging_base': {'source': 'recommended', 'area_name': '老城区', 'recommendation_reason': '靠近游玩区域', 'place': place('hotel')},
            'days': [{'date': '2026-10-01', 'description': '沿老城游览', 'activities': [
                {'activity_id': 'a1', 'type': 'sightseeing', 'period': 'morning', 'title': '游览公园', 'duration_minutes': 90, 'place': place('sight'), 'estimated_cost': {'amount': 20, 'basis': '模型估算'}, 'requirement_ids': ['r1']},
                {'activity_id': 'a2', 'type': 'meal', 'period': 'lunch', 'title': '吃当地面食', 'duration_minutes': 60, 'place': place('food'), 'estimated_cost': {'amount': 30, 'basis': '模型估算'}},
                {'activity_id': 'a3', 'type': 'free_time', 'period': 'afternoon', 'title': '自由活动', 'duration_minutes': 60, 'place': None, 'estimated_cost': {'amount': None, 'basis': '自选活动费用未知'}},
                {'activity_id': 'a4', 'type': 'meal', 'period': 'dinner', 'title': '附近晚餐', 'duration_minutes': 60, 'place': place('food'), 'estimated_cost': {'amount': 40, 'basis': '模型估算'}}]}]}


class FakeLLM:
    model = 'fake'
    def __init__(self):
        self.inputs = []
        self.revision_invalid = False
        self.long_day = False
        self.invalid_first = False
    def invoke(self, messages, **kwargs):
        assert len(messages) == 2, '每次调用必须独立上下文'
        data = json.loads(messages[-1]['content']); self.inputs.append(data)
        stage = data['stage']
        if stage == 'sights':
            return json.dumps({'conditions': {'city': '模型不能改城市', 'transportation': 'walking', 'must_visit_requests': [{'requirement_id': 'r1', 'text': '模型识别的必去公园'}]},
                               'searches': [] if data['candidates'] else [{'keywords': '模型决定搜索词', 'category': 'sightseeing'}]}, ensure_ascii=False)
        if stage == 'support':
            return json.dumps({'searches': [] if any('meal' in p['categories'] for p in data['candidates']) else [
                {'keywords': '用餐区域', 'category': 'meal'}, {'keywords': '住宿区域', 'category': 'lodging'}]}, ensure_ascii=False)
        if self.invalid_first and stage == 'final' and not data.get('repair'):
            return 'bad json'
        if stage == 'revision' and self.revision_invalid:
            return 'bad json'
        result = draft()
        if self.long_day:
            result['days'][0]['activities'][0]['duration_minutes'] = 600
        return json.dumps(result, ensure_ascii=False)


class FakeMCP:
    def __init__(self):
        self.calls = []; self.route_failure = False; self.empty = False; self.bad_search = False
    def get_expanded_tools(self): return []
    def run(self, value):
        name, args = value['tool_name'], value['arguments']; self.calls.append((name, args))
        if name == 'maps_text_search':
            if self.bad_search: raise RuntimeError('query failure')
            if self.empty: return {'pois': []}
            identity = {'模型决定搜索词': 'sight', '用餐区域': 'food', '住宿区域': 'hotel'}[args['keywords']]
            return {'pois': [{'id': identity, 'name': identity, 'address': '查询地址'}]}
        if name == 'maps_search_detail':
            identity = args['id']; delta = {'sight': 0.01, 'food': 0.02, 'hotel': 0}[identity]
            return {'id': identity, 'name': '真实' + identity, 'address': '真实地址', 'location': f'{116.4 + delta},39.9'}
        if name == 'maps_weather':
            raise RuntimeError('weather failure')
        if name.startswith('maps_direction_'):
            if self.route_failure: raise RuntimeError('route failure')
            return {'route': {'paths': [{'duration': '600', 'distance': '800'}]}}
        raise AssertionError(name)


class RouteWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.llm, self.mcp = FakeLLM(), FakeMCP()
        self.planner = RouteTripPlanner(self.llm, self.mcp)
        self.request = TripRequest(city='北京', start_date='2026-10-01', end_date='2026-10-01', free_text_input='不要去故宫；预算两千只是举例，请理解我的完整原文。')
    def run_plan(self): return self.planner.plan_trip(self.request).model_dump()

    def test_full_flow_grounding_and_unknowns(self):
        result = self.run_plan()
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual(result['planning_conditions']['city'], '北京')
        self.assertEqual(result['planning_conditions']['remarks'], self.request.free_text_input)
        self.assertIsNone(result['planning_conditions']['budget_per_adult'])  # 程序不能从“两千”提取预算
        day = result['days'][0]
        self.assertEqual(day['activities'][0]['place']['name'], '真实sight')
        self.assertAlmostEqual(day['activities'][0]['place']['longitude'], 116.41)
        self.assertEqual(len(day['legs']), 4)  # 自由活动没有地图节点
        self.assertEqual(day['legs'][-1]['to_activity_id'], 'lodging')
        self.assertEqual(day['time_summary']['known_total'], 330)
        self.assertFalse(result['cost_summary']['complete'])
        self.assertEqual(result['cost_summary']['known_total'], 90)
        self.assertIn('WEATHER_FAILED', [p['code'] for p in result['issues']])
        self.assertNotIn('MUST_VISIT_MISSING', [p['code'] for p in result['issues']])

    def test_unknown_route_is_not_zero_or_within_budget(self):
        self.mcp.route_failure = True
        day = self.run_plan()['days'][0]
        self.assertEqual(day['time_summary']['status'], 'incomplete')
        self.assertIsNone(day['legs'][0]['duration_minutes'])
        self.assertEqual(day['legs'][2]['duration_minutes'], 0)  # 同一已核实地点无需转移

    def test_empty_search_cannot_produce_fabricated_plan(self):
        self.mcp.empty = True
        with self.assertRaises(PlanningError): self.run_plan()
        self.assertFalse(any(i['stage'] == 'final' for i in self.llm.inputs))

    def test_failed_search_is_counted_and_bounded(self):
        self.mcp.bad_search = True
        with self.assertRaises(PlanningError): self.run_plan()
        self.assertEqual(sum(n == 'maps_text_search' for n, _ in self.mcp.calls), 2)

    def test_revision_failure_retains_previous_result_and_stops(self):
        self.llm.long_day = True; self.llm.revision_invalid = True
        result = self.run_plan()
        self.assertEqual(sum(i['stage'] == 'revision' for i in self.llm.inputs), 2)
        self.assertEqual(result['days'][0]['activities'][0]['duration_minutes'], 600)
        self.assertIn('REVISION_INCOMPLETE', [p['code'] for p in result['issues']])

    def test_initial_structure_repair_once(self):
        self.llm.invalid_first = True
        result = self.run_plan()
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual(sum(bool(i.get('repair')) for i in self.llm.inputs), 1)

    def test_failed_tool_observation_and_result_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = TaskRepository(Path(folder) / 'test.sqlite3'); repository.initialize()
            repository.create('test', self.request.model_dump()); repository.start('test')
            with ObservationService(repository).task('test'):
                result = self.run_plan()
            repository.succeed('test', result)
            self.assertEqual(repository.status('test')['status'], 'succeeded')
            self.assertEqual(repository.result('test')['schema_version'], 2)
            self.assertTrue(any(s['status'] == 'failed' and s['name'] == 'tool.amap_maps_weather' for s in repository.spans('test')))

    def test_new_conflict_comparison(self):
        before = {'issues': [{'code': 'TIME_EXCEEDED', 'category': 'needs_adjustment', 'date': '2026-10-01'}]}
        after = {'issues': [{'code': 'MUST_VISIT_MISSING', 'category': 'needs_adjustment', 'activity_id': 'r1'}]}
        self.assertFalse(self.planner.conflicts(after).issubset(self.planner.conflicts(before)))

    def test_unverified_place_cannot_enter_result(self):
        original = self.llm.invoke
        def invoke(messages, **kwargs):
            result = original(messages, **kwargs)
            data = json.loads(messages[-1]['content'])
            if data['stage'] == 'final':
                value = json.loads(result)
                value['days'][0]['activities'][0]['place']['source_id'] = 'invented'
                return json.dumps(value)
            return result
        self.llm.invoke = invoke
        with self.assertRaises(PlanningError) as failure: self.run_plan()
        self.assertEqual(failure.exception.code, 'UNKNOWN_PLACE')

    def test_unlocated_user_lodging_preserved(self):
        self.request.lodging = '无法唯一定位的用户住处'
        original = self.llm.invoke
        def invoke(messages, **kwargs):
            result = original(messages, **kwargs)
            data = json.loads(messages[-1]['content'])
            if data['stage'] in ('layout', 'final'):
                value = json.loads(result)
                value['lodging_base'] = {'source': 'user', 'user_input': self.request.lodging, 'place': None}
                return json.dumps(value)
            return result
        self.llm.invoke = invoke
        result = self.run_plan()
        self.assertEqual(result['lodging_base']['user_input'], self.request.lodging)
        self.assertIsNone(result['lodging_base']['place'])
        self.assertEqual(result['days'][0]['time_summary']['status'], 'incomplete')

    def test_minimal_request_and_date_validation(self):
        self.assertEqual(self.request.travel_days, 1)
        with self.assertRaises(ValueError):
            TripRequest(city='北京', start_date='2026-10-02', end_date='2026-10-01')
        with self.assertRaises(ValueError):
            TripRequest(city='北京', start_date='2026-10-01', end_date='2026-10-03', travel_days=1)


if __name__ == '__main__': unittest.main()

class MapsTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_closes_session(self):
        from app.services.planning_maps import call_with_timeout
        closed = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): closed.append(True)
            async def call_tool(self, *args): await asyncio.sleep(10)
        with patch('app.services.planning_maps.MCPClient', Client):
            with self.assertRaises(TimeoutError):
                await call_with_timeout([], [], {}, 'maps_weather', {}, 0.01)
        self.assertEqual(closed, [True])
