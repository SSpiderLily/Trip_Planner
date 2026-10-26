"""独立查询旅行日期天气，并投影为可消费的逐日事实。"""
from __future__ import annotations

from app.planning.fact_policy import _WEATHER_TEXT_FIELDS, _WEATHER_VALUE_FIELDS, _has_valid_weather_fields

import asyncio
from contextvars import copy_context
from datetime import date, timedelta
import math
import re
import unicodedata
from threading import Event
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from app.agents.execution import PlanningError
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.facts import (
    FactStore,
    MissingField,
    ToolResult,
    WeatherFact,
    WeatherForecast,
)
from app.models.planning_v4.input import PlanningInput
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import MCPToolExecutor, get_mcp_tool_executor
from app.services.observation_service import span
from app.services.planning_lodging_policy import city_for_days


from app.planning.weather_projection import (
    _ADCODE, _CITY_SUFFIXES, WeatherQueryContext, WeatherDateCoverage, WeatherQueryExecution, _validate_context, _normalize_city, _city_projection_error, _clean_projection_forecast, _coverage_status, _unknown, _project_result
)

class WeatherQueryService:
    """单次普通地图工具查询；可注入城市日期范围，不创建模型角色。"""

    def __init__(
        self,
        *,
        adapter: AmapContractAdapter | None = None,
        tool_executor: MCPToolExecutor | None = None,
        tool_timeout_seconds: float | None = None,
    ):
        self.adapter = adapter
        self.tool_executor = tool_executor
        if tool_timeout_seconds is None:
            from app.config import settings

            tool_timeout_seconds = settings.planner_tool_timeout_seconds
        if (isinstance(tool_timeout_seconds, bool) or not isinstance(tool_timeout_seconds, (int, float))
                or not math.isfinite(tool_timeout_seconds) or tool_timeout_seconds <= 0):
            raise ValueError("tool_timeout_seconds 必须是有限正数")
        self.tool_timeout_seconds = float(tool_timeout_seconds)

    async def run_async(self, context: WeatherQueryContext) -> WeatherQueryExecution:
        """异步取消返回调用方，并阻止仍在排队的 MCP 调用启动。"""
        loop = asyncio.get_running_loop()
        copied_context = copy_context()
        cancel_event = Event()
        future = loop.run_in_executor(
            None,
            copied_context.run,
            lambda: self.run(context, cancel_event=cancel_event),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    def run(
        self,
        context: WeatherQueryContext,
        *,
        cancel_event: Event | None = None,
    ) -> WeatherQueryExecution:
        city, travel_dates, timezone_name = _validate_context(context)
        safe_context = {
            "request_id": context.request_id,
            "run_id": context.run_id,
            "context_revision": context.context_revision,
            "base_revision": context.base_revision,
            "city": city,
            "travel_dates": [item.isoformat() for item in travel_dates],
        }
        with span("weather.query", "agent", safe_context) as record:
            adapter = self.adapter or self._adapter_for(cancel_event)
            result = adapter.get_weather(
                city,
                travel_dates,
                timezone_name=timezone_name,
            )
            execution = _project_result(context, city, travel_dates, result)
            record.output = {
                "tool_status": execution.tool_status,
                "tool_error_category": execution.tool_error_category,
                "coverage": [
                    {"date": item.forecast_date.isoformat(), "status": item.status, "reason": item.reason}
                    for item in execution.coverage
                ],
                "projection_error": execution.projection_error,
                "fact_count": len(execution.fact_store.weather),
            }
            return execution

    def _adapter_for(self, cancel_event: Event | None) -> AmapContractAdapter:
        executor = self.tool_executor or get_mcp_tool_executor()

        def tool_runner(tool_name: str, arguments: dict) -> object:
            return executor.run(
                tool_name,
                arguments,
                timeout_seconds=self.tool_timeout_seconds,
                cancel_event=cancel_event,
            )

        return AmapContractAdapter(tool_runner=tool_runner)
