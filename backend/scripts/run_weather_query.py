"""显式执行一次上海三日天气 MCP 核验；默认不会联网。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import sys
from uuid import uuid4

from app.agents.execution import PlanningError
from app.models.planning_v4.input import (
    ReferenceWindow,
    RequirementSummary,
    RuleSet,
    TripBudget,
    TripRequest,
)
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.weather_query_service import WeatherQueryContext, WeatherQueryService


WINDOWS = (
    ReferenceWindow(period="breakfast", start_minute=480, end_minute=540),
    ReferenceWindow(period="morning", start_minute=540, end_minute=720),
    ReferenceWindow(period="lunch", start_minute=720, end_minute=840),
    ReferenceWindow(period="afternoon", start_minute=840, end_minute=1020),
    ReferenceWindow(period="dinner", start_minute=1020, end_minute=1140),
    ReferenceWindow(period="evening", start_minute=1140, end_minute=1320),
)


def shanghai_weather_context() -> WeatherQueryContext:
    """构造固定 F04c 输入，不运行 F01 或景点查询。"""
    request = TripRequest(
        request_id="DEMO_REQ",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        preferences=("城市漫步", "美食"),
        transport_preferences=("transit", "walking"),
        arrival_place_query=None,
        departure_place_query=None,
        lodging_query=None,
        budget=TripBudget(amount=None, currency="CNY", scope="whole_trip", party_basis="unspecified"),
        remarks="想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中，尽量少折返；安排顺路午餐和晚餐。",
    )
    planning_input = build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=RequirementSummary(
            requirements_id="F04C_LIVE",
            request_id=request.request_id,
        ),
        rules=RuleSet(rule_version="f04c-live-v1", windows=WINDOWS),
    ))
    return WeatherQueryContext.from_planning_input(
        planning_input,
        run_id=f"weather-live-{uuid4().hex}",
        context_revision=1,
        base_revision=None,
    )


def _report(result) -> dict:
    tool_result = result.diagnostic_tool_result
    data = tool_result.data
    source = tool_result.source
    city_missing = any(item.field == "city" and item.reason == "not_returned" for item in tool_result.missing_fields)
    forecasts_by_date = {
        forecast.forecast_date: forecast
        for fact in result.fact_store.weather
        for forecast in fact.forecasts
    }
    return {
        "request_id": result.request_id,
        "run_id": result.run_id,
        "context_revision": result.context_revision,
        "base_revision": result.base_revision,
        "city": result.city,
        "tool_status": result.tool_status,
        "tool_error_category": result.tool_error_category,
        "projection_error": result.projection_error,
        "source": {
            "provider": source.provider,
            "tool_name": source.tool_name,
            "api_version": source.api_version,
            "queried_at": source.queried_at.isoformat(),
        },
        "provider_city": data.city if data and not city_missing else None,
        "provider_adcode": data.adcode if data else None,
        "report_time": data.report_time.isoformat() if data and data.report_time else None,
        "coverage": [
            {
                "date": item.forecast_date.isoformat(),
                "status": item.status,
                "reason": item.reason,
                "forecast": (
                    forecasts_by_date[item.forecast_date].model_dump(mode="json")
                    if item.forecast_date in forecasts_by_date else None
                ),
            }
            for item in result.coverage
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="独立核验上海三日天气查询")
    parser.add_argument(
        "--live",
        action="store_true",
        help="确认本次执行一次真实高德天气 MCP 查询；不调用模型或景点工具",
    )
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"error": {"code": "LIVE_CONFIRMATION_REQUIRED"}}, ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        context = shanghai_weather_context()
        # MCP 初始化提示不能混入机器可读输出；不回显环境变量或原始工具响应。
        with redirect_stdout(io.StringIO()):
            result = WeatherQueryService().run(context)
        print(json.dumps(_report(result), ensure_ascii=False, indent=2))
        return 0
    except PlanningError as exc:
        print(json.dumps({"error": {"code": exc.code, "step": exc.step}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "WEATHER_QUERY_FAILED", "step": "weather_query"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
