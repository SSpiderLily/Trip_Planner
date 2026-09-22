"""旅行规划 Agent 的 MCP 工具注册测试。"""

import unittest
from unittest.mock import patch

from hello_agents.tools.base import Tool

from app.agents.trip_planner_agent import MultiAgentTripPlanner


class FakeExpandedTool(Tool):
    """模拟由 MCPTool 发现并包装后的单个 MCP 工具。"""

    def __init__(self, name: str):
        super().__init__(name=name, description=f"测试工具: {name}")

    def run(self, parameters):
        return "ok"

    def get_parameters(self):
        return []


class FakeMCPTool(Tool):
    """模拟 MCPTool，保留 0.2.9 中 expandable=False 的行为。"""

    def __init__(self, **kwargs):
        super().__init__(name=kwargs["name"], description=kwargs["description"])
        prefix = f"{kwargs['name']}_"
        self._expanded_tools = [
            FakeExpandedTool(f"{prefix}maps_text_search"),
            FakeExpandedTool(f"{prefix}maps_weather"),
        ]

    def run(self, parameters):
        return "ok"

    def get_parameters(self):
        return []

    def get_expanded_tools(self):
        return self._expanded_tools


class MCPToolRegistrationTest(unittest.TestCase):
    def test_agents_register_discovered_amap_tools(self):
        with (
            patch("app.agents.trip_planner_agent.MCPTool", FakeMCPTool),
            patch("app.agents.trip_planner_agent.get_llm", return_value=object()),
        ):
            planner = MultiAgentTripPlanner()

        for agent in (
            planner.attraction_agent,
            planner.weather_agent,
            planner.hotel_agent,
        ):
            self.assertIn("amap_maps_text_search", agent.list_tools())
            self.assertIn("amap_maps_weather", agent.list_tools())


if __name__ == "__main__":
    unittest.main()
