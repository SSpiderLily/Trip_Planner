"""原生规划的天气事实边界：按已确认城市查询，逐日覆盖，未知不补造。"""
from app.models.planning_evidence import WeatherEvidence
from app.planning.collection_evidence import weather_scopes as _scopes, _merge_weather, validate_weather_evidence
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


def validate_native_weather_result(planning, run_id, result):
    if not isinstance(result, NativeWeatherResult):
        raise PlanningError("WEATHER_RESULT_INVALID", "天气事实结果无效", step="weather_projection")
    try:
        evidence = WeatherEvidence.model_validate({key: getattr(result, key) for key in ('run_id', 'planning_input', 'executions', 'fact_store')})
    except (AttributeError, TypeError, ValueError):
        raise PlanningError("WEATHER_RESULT_INVALID", "天气事实结果无效", step="weather_projection") from None
    validate_weather_evidence(planning, run_id, evidence)

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
