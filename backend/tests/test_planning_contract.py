"""真实 SimpleAgent 消息循环，替代 LLM/工具，无外部计费。"""
import json
import unittest
from unittest.mock import Mock
from pydantic import ValidationError
from app.agents.execution import PlanningAgent, PlanningError, forecast_by_date
from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import TripRequest
from test_trip_planner_agent import FakeExpandedTool


def request():
    return TripRequest(city='北京', start_date='2026-09-26', end_date='2026-09-26', travel_days=1,
                       transportation='步行', accommodation='酒店')


class PlanningContractTest(unittest.TestCase):
    def agent(self, result, replies=None):
        llm = Mock()
        llm.invoke.side_effect = replies or ['[TOOL_CALL:amap_maps_text_search:keywords=景点,city=北京]', '完成']
        agent = PlanningAgent('test', llm=llm)
        tool = FakeExpandedTool('amap_maps_text_search')
        tool.run = Mock(return_value=result)
        agent.add_tool(tool, auto_expand=False)
        return agent, llm, tool

    def test_failed_tool_stops_before_next_model_round(self):
        agent, llm, tool = self.agent('{"status":"0","info":"INVALID_KEY"}')
        with self.assertRaises(PlanningError):
            agent.run('需求')
        self.assertEqual(llm.invoke.call_count, 1)
        self.assertEqual(agent.get_history(), [])

    def test_exception_stops(self):
        agent, llm, tool = self.agent('')
        tool.run.side_effect = RuntimeError('secret')
        with self.assertRaisesRegex(PlanningError, '工具执行失败'):
            agent.run('需求')
        self.assertEqual(llm.invoke.call_count, 1)

    def test_no_tool_is_not_success_evidence(self):
        agent, _, _ = self.agent('', ['我已经查过了'])
        agent.run('需求包含 [TOOL_CALL:amap_maps_text_search:keywords=景点]')
        with self.assertRaises(PlanningError):
            agent.require('amap_maps_text_search', 'pois')

    def test_empty_result_is_business_failure(self):
        agent, llm, _ = self.agent('{"pois":[]}')
        with self.assertRaises(PlanningError):
            agent.run('需求')
        self.assertIn('amap_maps_text_search', agent.tool_evidence)
        self.assertEqual(llm.invoke.call_count, 1)

    def test_two_runs_no_history_leak(self):
        agent, llm, _ = self.agent('{"pois":[{"name":"公园"}]}', ['第一次回复', '第二次回复'])
        agent.run('第一次需求')
        agent.run('第二次需求')
        self.assertNotIn('第一次', json.dumps(llm.invoke.call_args.args[0], ensure_ascii=False))

    def test_input_date_conflict_rejected(self):
        data = request().model_dump()
        data['travel_days'] = 2
        with self.assertRaises(ValidationError):
            TripRequest(**data)

    def test_invalid_json_no_fallback(self):
        planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
        with self.assertRaises(ValueError):
            planner._parse_response('invalid', request())

    def test_itinerary_date_mismatch(self):
        planner = MultiAgentTripPlanner.__new__(MultiAgentTripPlanner)
        data = dict(city='北京', start_date='2026-09-26', end_date='2026-09-26', days=[], overall_suggestions='建议')
        with self.assertRaises(PlanningError):
            planner._parse_response(json.dumps(data), request())

    def test_forecasts_keep_original_dates(self):
        forecasts = forecast_by_date([dict(date='2026-09-25', dayweather='晴', nightweather='晴')])
        self.assertNotIn('2026-09-26', forecasts)
        with self.assertRaises(PlanningError):
            forecast_by_date([])

    def test_current_mcp_flat_weather_shape(self):
        agent, _, _ = self.agent('', ['天气'])
        agent.run('需求')
        agent.tool_evidence = {'amap_maps_weather': [{'forecasts': [{'date':'2026-09-26', 'dayweather':'晴', 'nightweather':'晴'}]}]}
        forecasts = forecast_by_date(agent.require('amap_maps_weather', 'casts'))
        self.assertIn('2026-09-26', forecasts)

    def test_later_empty_query_not_hidden_by_previous_success(self):
        agent, llm, tool = self.agent('', ['[TOOL_CALL:amap_maps_text_search:keywords=景点,city=北京] [TOOL_CALL:amap_maps_text_search:keywords=酒店,city=北京]'])
        tool.run.side_effect = ['{"pois":[{"name":"公园"}]}', '{"pois":[]}']
        with self.assertRaises(PlanningError):
            agent.run('需求')
        self.assertEqual(tool.run.call_count, 2)
        self.assertEqual(llm.invoke.call_count, 1)
