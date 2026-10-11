"""独立查询旅行日期天气，并投影为可消费的逐日事实。"""
from __future__ import annotations

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


_ADCODE = re.compile(r"^\d{6}$")
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "市")
_WEATHER_TEXT_FIELDS = (
    "day_weather", "night_weather", "day_wind", "night_wind", "day_power", "night_power",
)
_WEATHER_VALUE_FIELDS = (*_WEATHER_TEXT_FIELDS, "day_temp_c", "night_temp_c")


class WeatherQueryContext(ContractModel):
    """一次天气查询的来源版本；城市和日期只从 PlanningInput 派生。"""

    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    base_revision: int | None = Field(default=None, ge=1)
    planning_input: PlanningInput

    @model_validator(mode="after")
    def validate_request_reference(self):
        if self.request_id != self.planning_input.request.request_id:
            raise ValueError("天气上下文 request_id 与 PlanningInput 不一致")
        return self

    @classmethod
    def from_planning_input(
        cls,
        planning_input: PlanningInput,
        *,
        run_id: str,
        context_revision: int,
        base_revision: int | None = None,
    ) -> "WeatherQueryContext":
        return cls(
            request_id=planning_input.request.request_id,
            run_id=run_id,
            context_revision=context_revision,
            base_revision=base_revision,
            planning_input=planning_input,
        )


class WeatherDateCoverage(ContractModel):
    forecast_date: date
    status: Literal["available", "partial", "unknown"]
    reason: Literal[
        "matched_forecast",
        "out_of_coverage",
        "no_valid_fields",
        "duplicate_provider_date",
        "query_error",
        "city_mismatch",
        "city_unverified",
        "no_forecast",
    ] | None = None
    fact_id: str | None = None

    @model_validator(mode="after")
    def validate_coverage(self):
        if self.status == "unknown":
            if self.reason not in {
                "out_of_coverage",
                "no_valid_fields",
                "duplicate_provider_date",
                "query_error",
                "city_mismatch",
                "city_unverified",
                "no_forecast",
            } or self.fact_id is not None:
                raise ValueError("unknown 天气必须说明原因且不能引用可用事实")
        elif self.reason != "matched_forecast" or self.fact_id is None:
            raise ValueError("有效天气必须引用唯一匹配的预报事实")
        return self


class WeatherQueryExecution(ContractModel):
    """工具状态、逐日覆盖与可供后续使用的事实投影彼此分开。"""

    request_id: str
    run_id: str
    context_revision: int
    base_revision: int | None
    city: str
    travel_dates: tuple[date, ...]
    tool_status: Literal["ok", "empty", "error"]
    tool_error_category: Literal[
        "invalid_request", "auth", "rate_limit", "timeout", "network", "provider_error", "invalid_response",
    ] | None = None
    diagnostic_tool_result: ToolResult[WeatherFact]
    coverage: tuple[WeatherDateCoverage, ...]
    fact_store: FactStore
    projection_error: Literal["city_mismatch", "city_unverified"] | None = None

    @model_validator(mode="after")
    def validate_coverage_dates(self):
        if self.tool_status != self.diagnostic_tool_result.status:
            raise ValueError("tool_status 必须与诊断 ToolResult 一致")
        expected_error = self.diagnostic_tool_result.error.category if self.diagnostic_tool_result.error else None
        if self.tool_error_category != expected_error:
            raise ValueError("tool_error_category 必须与诊断 ToolResult 一致")
        if tuple(item.forecast_date for item in self.coverage) != self.travel_dates:
            raise ValueError("天气覆盖必须逐项对应原旅行日期")
        if len(self.travel_dates) != len(set(self.travel_dates)):
            raise ValueError("天气查询旅行日期不能重复")
        forecasts_by_fact = {
            fact.fact_id: {forecast.forecast_date for forecast in fact.forecasts}
            for fact in self.fact_store.weather
        }
        for item in self.coverage:
            if item.status == "unknown":
                continue
            if item.fact_id not in forecasts_by_fact or item.forecast_date not in forecasts_by_fact[item.fact_id]:
                raise ValueError("逐日天气必须引用 FactStore 中对应日期的有效事实")
        return self

class WeatherQueryService:
    """单次直接工具查询；不创建模型角色，也不接入主流程。"""

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
        with span("weather.query", "service", safe_context) as record:
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


def _validate_context(context: WeatherQueryContext) -> tuple[str, tuple[date, ...], str]:
    try:
        if not isinstance(context, WeatherQueryContext):
            raise ValueError("天气查询上下文类型无效")
        request = context.planning_input.request
        if context.request_id != request.request_id:
            raise ValueError("天气上下文 request_id 与原始请求不一致")
        if not context.run_id or type(context.context_revision) is not int or context.context_revision < 1:
            raise ValueError("天气上下文运行标识或版本无效")
        if context.base_revision is not None and (
            type(context.base_revision) is not int or context.base_revision < 1
        ):
            raise ValueError("天气上下文基准版本无效")
        city = request.city.strip()
        if not city:
            raise ValueError("旅行城市为空")
        zone = ZoneInfo(request.timezone)
        arrival_date = request.arrival_at.astimezone(zone).date()
        departure_date = request.departure_at.astimezone(zone).date()
        expected_dates = tuple(
            arrival_date + timedelta(days=offset)
            for offset in range((departure_date - arrival_date).days + 1)
        )
        actual_dates = tuple(day.day_date for day in context.planning_input.days)
        if not actual_dates or len(actual_dates) != len(set(actual_dates)):
            raise ValueError("PlanningInput 日期为空或重复")
        if actual_dates != expected_dates:
            raise ValueError("PlanningInput 日期与原始旅行请求不一致")
        return city, actual_dates, request.timezone
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PlanningError(
            "WEATHER_CONTEXT_INVALID",
            "天气查询上下文无效",
            step="weather_preflight",
        ) from exc


def _normalize_city(value: str) -> str:
    normalized = "".join(unicodedata.normalize("NFKC", value).split()).casefold()
    for suffix in _CITY_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return normalized


def _city_projection_error(
    requested_city: str,
    fact: WeatherFact,
    missing_fields: tuple[MissingField, ...],
) -> Literal["city_mismatch", "city_unverified"] | None:
    missing_city = any(item.field == "city" and item.reason == "not_returned" for item in missing_fields)
    requested = requested_city.strip()
    returned_adcode = (fact.adcode or "").strip()
    if _ADCODE.fullmatch(requested):
        if returned_adcode:
            return None if returned_adcode == requested else "city_mismatch"
        if missing_city:
            return "city_unverified"
        return None if fact.city and _normalize_city(fact.city) == _normalize_city(requested) else "city_mismatch"
    if missing_city:
        return "city_unverified"
    if not fact.city:
        return "city_unverified"
    return None if _normalize_city(fact.city) == _normalize_city(requested) else "city_mismatch"


def _has_valid_weather_fields(forecast: WeatherForecast) -> bool:
    for name in _WEATHER_VALUE_FIELDS:
        value = getattr(forecast, name)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return True
    return False


def _clean_projection_forecast(forecast: WeatherForecast) -> WeatherForecast:
    """保留原 ToolResult，只把无效/空字段从可消费投影中移除。"""
    updates: dict[str, str | float | None] = {}
    for name in _WEATHER_TEXT_FIELDS:
        value = getattr(forecast, name)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized != value:
                updates[name] = normalized or None
    for name in ("day_temp_c", "night_temp_c"):
        value = getattr(forecast, name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            updates[name] = None
    return forecast.model_copy(update=updates) if updates else forecast


def _coverage_status(forecast: WeatherForecast) -> Literal["available", "partial"]:
    has_day_weather = bool((forecast.day_weather or "").strip())
    has_night_weather = bool((forecast.night_weather or "").strip())
    return "available" if has_day_weather and has_night_weather else "partial"


def _unknown(
    forecast_date: date,
    reason: Literal[
        "out_of_coverage", "no_valid_fields", "duplicate_provider_date", "query_error",
        "city_mismatch", "city_unverified", "no_forecast",
    ],
) -> WeatherDateCoverage:
    return WeatherDateCoverage(forecast_date=forecast_date, status="unknown", reason=reason)


def _project_result(
    context: WeatherQueryContext,
    city: str,
    travel_dates: tuple[date, ...],
    result: ToolResult[WeatherFact],
) -> WeatherQueryExecution:
    data = result.data
    projection_error = None
    if data is not None:
        projection_error = _city_projection_error(city, data, result.missing_fields)

    if result.status == "error":
        coverage = tuple(_unknown(day, "query_error") for day in travel_dates)
        projected_store = FactStore()
    elif projection_error is not None:
        coverage = tuple(_unknown(day, projection_error) for day in travel_dates)
        projected_store = FactStore()
    else:
        rows_by_date: dict[date, list[WeatherForecast]] = {}
        for forecast in data.forecasts if data is not None else ():
            rows_by_date.setdefault(forecast.forecast_date, []).append(forecast)
        coverage_items: list[WeatherDateCoverage] = []
        usable_forecasts: list[WeatherForecast] = []
        missing_dates: list[date] = []
        for travel_date in travel_dates:
            rows = rows_by_date.get(travel_date, [])
            if not rows:
                reason = "out_of_coverage" if result.status == "ok" else "no_forecast"
                coverage_items.append(_unknown(travel_date, reason))
                missing_dates.append(travel_date)
                continue
            if len(rows) != 1:
                coverage_items.append(_unknown(travel_date, "duplicate_provider_date"))
                missing_dates.append(travel_date)
                continue
            forecast = _clean_projection_forecast(rows[0])
            if not _has_valid_weather_fields(forecast):
                coverage_items.append(_unknown(travel_date, "no_valid_fields"))
                missing_dates.append(travel_date)
                continue
            if data is None:
                # This branch is unreachable after rows_by_date construction, but keeps the invariant local.
                coverage_items.append(_unknown(travel_date, "no_forecast"))
                missing_dates.append(travel_date)
                continue
            coverage_items.append(WeatherDateCoverage(
                forecast_date=travel_date,
                status=_coverage_status(forecast),
                reason="matched_forecast",
                fact_id=f"{data.fact_id}:projection",
            ))
            usable_forecasts.append(forecast)

        coverage = tuple(coverage_items)
        if data is not None and usable_forecasts:
            projected_fact = data.model_copy(update={
                "fact_id": f"{data.fact_id}:projection",
                "forecasts": tuple(usable_forecasts),
                "missing_dates": tuple(missing_dates),
            })
            projected_store = FactStore(
                sources=(projected_fact.source,),
                weather=(projected_fact,),
            )
        else:
            projected_store = FactStore()

    return WeatherQueryExecution(
        request_id=context.request_id,
        run_id=context.run_id,
        context_revision=context.context_revision,
        base_revision=context.base_revision,
        city=city,
        travel_dates=travel_dates,
        tool_status=result.status,
        tool_error_category=result.error.category if result.error else None,
        diagnostic_tool_result=result,
        coverage=coverage,
        fact_store=projected_store,
        projection_error=projection_error,
    )
