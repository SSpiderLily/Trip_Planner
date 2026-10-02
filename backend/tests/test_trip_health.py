"""旅行规划健康检查路由测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.trip import router


test_app = FastAPI()
test_app.include_router(router, prefix="/api")


class TripHealthTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(test_app)

    def test_health_reports_multi_agent_planner_and_expanded_amap_tools(self):
        planner = SimpleNamespace(
            amap_tools=[object(), object(), object(), object(), object()],
            agents=[SimpleNamespace(name=name) for name in ("候选搜集", "行程安排", "行程修改")],
        )

        with patch("app.api.routes.trip.get_trip_planner_agent", return_value=planner):
            response = self.client.get("/api/trip/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "healthy",
                "service": "trip-planner",
                "agent_name": "多智能体旅行规划系统",
                "tools_count": 5,
                "agents": [
                    "候选搜集",
                    "行程安排",
                    "行程修改",
                ],
            },
        )

    def test_health_returns_503_when_planner_initialization_fails(self):
        with patch(
            "app.api.routes.trip.get_trip_planner_agent",
            side_effect=RuntimeError("planner initialization failed"),
        ):
            response = self.client.get("/api/trip/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "服务不可用: planner initialization failed"},
        )


if __name__ == "__main__":
    unittest.main()
