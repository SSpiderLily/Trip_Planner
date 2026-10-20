"""原生规划的天气事实边界：按已确认城市查询，逐日覆盖，未知不补造。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

from app.agents.execution import PlanningError
from app.models.planning_v4.facts import FactStore, FactSource, ToolResult, ToolError, WeatherFact
from app.models.planning_v4.input import PlanningInput
from app.services.planning_lodging_policy import city_for_days
from app.services.weather_query_service import (
    WeatherQueryContext, WeatherQueryExecution, WeatherQueryService, _project_result, _validate_context,
)


def _scopes(planning):
    cities = {}
    for day in planning.days:
        cities.setdefault(city_for_days(planning, (day.day_id,)), []).append(day.day_id)
    return tuple((city, tuple(day_ids)) for city, day_ids in cities.items())


@dataclass(frozen=True)
class NativeWeatherResult:
    run_id: str
    planning_input: PlanningInput = field(repr=False)
    executions: tuple[WeatherQueryExecution, ...] = field(repr=False)
    fact_store: FactStore = field(repr=False)

    def tool_payload(self):
        by_date = {item.forecast_date: (execution, item)
                   for execution in self.executions for item in execution.coverage}
        return {
            "status": "queried",
            "coverage": [{"day_id": day.day_id, "city": by_date[day.day_date][0].city,
                          **by_date[day.day_date][1].model_dump(mode="json")}
                         for day in self.planning_input.days],
            "queries": [{"city": execution.city, "tool_status": execution.tool_status,
                         "error_category": execution.tool_error_category,
                         "projection_error": execution.projection_error}
                        for execution in self.executions],
            "weather_facts": [fact.model_dump(mode="json") for fact in self.fact_store.weather],
            "policy": "仅使用同城同日预报；未知不补造，查询失败不触发地点修订。",
        }


def _merge_weather(executions):
    sources, weather = {}, {}
    for execution in executions:
        for rows, values, key_name in ((sources, execution.fact_store.sources, "tool_call_id"),
                                       (weather, execution.fact_store.weather, "fact_id")):
            for value in values:
                key = getattr(value, key_name)
                if key in rows and rows[key] != value:
                    raise ValueError("天气事实标识冲突")
                rows[key] = value
    return FactStore(sources=tuple(sources.values()), weather=tuple(weather.values()))


def validate_native_weather_result(planning, run_id, result):
    """消费者再次核对身份、城市/日期、投影与来源，拒绝外部注入错误状态。"""
    try:
        if not isinstance(result, NativeWeatherResult) or result.run_id != run_id or result.planning_input != planning:
            raise ValueError("天气运行身份不一致")
        scopes = _scopes(planning)
        if len(result.executions) != len(scopes):
            raise ValueError("天气城市范围不完整")
        for (city, day_ids), execution in zip(scopes, result.executions):
            context = WeatherQueryContext.from_planning_input(planning, run_id=run_id,
                context_revision=1, day_ids=day_ids)
            dates = tuple(day.day_date for day in planning.days if day.day_id in day_ids)
            diagnostic = execution.diagnostic_tool_result
            if (diagnostic.source.tool_name != "maps_weather"
                    or diagnostic.source.tool_call_id != diagnostic.tool_call_id
                    or (diagnostic.data is not None and diagnostic.data.source != diagnostic.source)):
                raise ValueError("天气来源不一致")
            expected = _project_result(context, city, dates, diagnostic)
            if execution != expected:
                raise ValueError("天气投影与原始结果不一致")
        if result.fact_store != _merge_weather(result.executions):
            raise ValueError("天气合并事实不一致")
    except (AttributeError, TypeError, ValueError, KeyError):
        raise PlanningError("WEATHER_RESULT_INVALID", "天气事实结果无效", step="weather_projection") from None


class NativeWeatherService:
    def __init__(self, *, adapter=None, tool_executor=None, query_service=None, emit=None):
        self.query_service = query_service or WeatherQueryService(adapter=adapter, tool_executor=tool_executor)
        self.emit = emit or (lambda event, data: None)

    def run(self, planning_input, *, run_id, cancel_event: Event | None = None):
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise PlanningError("CANCELLED", "天气查询已取消", step="weather_query")

        if not isinstance(planning_input, PlanningInput) or not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
            raise PlanningError("WEATHER_CONTEXT_INVALID", "天气查询上下文无效", step="weather_preflight")
        try:
            scopes = _scopes(planning_input)
        except (AttributeError, TypeError, ValueError):
            raise PlanningError("WEATHER_CONTEXT_INVALID", "天气查询上下文无效", step="weather_preflight") from None
        executions = []
        for city, day_ids in scopes:
            cancelled()
            try:
                context = WeatherQueryContext.from_planning_input(planning_input, run_id=run_id,
                    context_revision=1, day_ids=day_ids)
                _, dates, _ = _validate_context(context)
            except (AttributeError, TypeError, ValueError):
                raise PlanningError("WEATHER_CONTEXT_INVALID", "天气查询上下文无效", step="weather_preflight") from None
            self.emit("weather.start", {"run_id": run_id, "city": city, "day_ids": list(day_ids)})
            try:
                execution = self.query_service.run(context, cancel_event=cancel_event)
            except PlanningError:
                raise  # 上下文/取消属于执行约束，不能伪装成天气未知。
            except Exception as exc:
                cancelled()
                # 客户端初始化等发生在适配器之外的失败也不能泄漏异常原文。
                # 这里只记录本地查询尝试，不声称供应商收到参数或返回了事实。
                source = FactSource(tool_call_id=f"weather-attempt-{uuid4().hex}", tool_name="maps_weather",
                    api_version="v3", queried_at=datetime.now(timezone.utc), effective_params={})
                category = "timeout" if isinstance(exc, TimeoutError) else "network" if isinstance(exc, ConnectionError) else "provider_error"
                diagnostic = ToolResult[WeatherFact](tool_call_id=source.tool_call_id, status="error", source=source,
                    error=ToolError(category=category, retryable=category in {"timeout", "network"}))
                execution = _project_result(context, city, dates, diagnostic)
            cancelled()  # 取消后不能合并正在执行的查询晚到结果。
            executions.append(execution)
        try:
            result = NativeWeatherResult(run_id, planning_input, tuple(executions), _merge_weather(executions))
        except (AttributeError, TypeError, ValueError):
            raise PlanningError("WEATHER_RESULT_INVALID", "天气事实结果无效", step="weather_projection") from None
        validate_native_weather_result(planning_input, run_id, result)
        self.emit("weather.result", {"run_id": run_id, **result.tool_payload()})
        return result
