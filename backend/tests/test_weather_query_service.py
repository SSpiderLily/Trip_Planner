"""F04c 天气查询与逐日投影的离线回归测试。"""
import asyncio
from datetime import date, datetime
import threading
import unittest
from unittest.mock import patch

from app.agents.execution import PlanningError
from app.models.planning_v4.facts import (
    FactSource,
    FactStore,
    ToolResult,
    WeatherFact,
    WeatherForecast,
)
from app.models.planning_v4.input import RequirementSummary, TripBudget, TripRequest
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.models.planning_v4.input import ReferenceWindow, RuleSet
from app.services.weather_query_service import WeatherQueryContext, WeatherQueryService


WINDOWS = (
    ReferenceWindow(period="breakfast", start_minute=480, end_minute=540),
    ReferenceWindow(period="morning", start_minute=540, end_minute=720),
    ReferenceWindow(period="lunch", start_minute=720, end_minute=840),
    ReferenceWindow(period="afternoon", start_minute=840, end_minute=1020),
    ReferenceWindow(period="dinner", start_minute=1020, end_minute=1140),
    ReferenceWindow(period="evening", start_minute=1140, end_minute=1320),
)


def make_planning_input(*, city="上海"):
    request = TripRequest(
        request_id="shanghai_request",
        city=city,
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        preferences=(),
        transport_preferences=(),
        arrival_place_query=None,
        departure_place_query=None,
        lodging_query=None,
        budget=TripBudget(amount=None, currency="CNY", scope="whole_trip", party_basis="unspecified"),
    )
    requirements = RequirementSummary(
        requirements_id="requirements-shanghai",
        request_id=request.request_id,
    )
    rules = RuleSet(rule_version="weather-test-v1", windows=WINDOWS)
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=requirements,
        rules=rules,
    ))


def make_context(planning_input=None):
    return WeatherQueryContext.from_planning_input(
        planning_input or make_planning_input(),
        run_id="weather-run-1",
        context_revision=2,
        base_revision=1,
    )


def source(params=None):
    return {
        "api_version": "v3",
        "queried_at": "2026-10-09T02:00:00+00:00",
        "effective_params": params or {"city": "上海"},
    }


def forecast(day, **fields):
    return {"date": day, **fields}


def projected_forecast(result, day):
    return next(
        item
        for fact in result.fact_store.weather
        for item in fact.forecasts
        if item.forecast_date == day
    )


def success_payload(rows, *, city="上海", adcode="310000", reporttime="2026-10-09 10:00:00"):
    return {
        "result_status": "ok",
        "city": city,
        "adcode": adcode,
        "reporttime": reporttime,
        "forecasts": rows,
        "result_source": source(),
    }


class WeatherQueryServiceTests(unittest.TestCase):
    def service_for(self, payload):
        adapter = AmapContractAdapter(lambda *_: payload)
        return WeatherQueryService(adapter=adapter)

    def test_shanghai_dates_match_only_exact_forecast_days_and_preserve_source(self):
        payload = success_payload([
            forecast("2026-10-10", dayweather="晴", nightweather="多云", daytemp="0", nighttemp="-2"),
            forecast("2026-10-11", dayweather="阴", daytemp="8"),
        ])

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.travel_dates, (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)))
        self.assertEqual(result.tool_status, "ok")
        self.assertEqual([item.status for item in result.coverage], ["available", "partial", "unknown"])
        self.assertEqual(projected_forecast(result, date(2026, 10, 10)).day_temp_c, 0)
        self.assertEqual(projected_forecast(result, date(2026, 10, 10)).night_temp_c, -2)
        self.assertEqual(result.coverage[2].reason, "out_of_coverage")
        self.assertEqual(len(result.fact_store.weather), 1)
        fact = result.fact_store.weather[0]
        self.assertEqual([item.forecast_date for item in fact.forecasts], [date(2026, 10, 10), date(2026, 10, 11)])
        self.assertEqual(fact.missing_dates, (date(2026, 10, 12),))
        self.assertEqual(fact.report_time.isoformat(), "2026-10-09T10:00:00+08:00")
        self.assertEqual(result.context_revision, 2)
        self.assertEqual(result.base_revision, 1)
        self.assertEqual(result.fact_store.sources[0].tool_name, "maps_weather")
        serialized = result.model_dump(mode="json")
        self.assertEqual(serialized["tool_status"], "ok")
        self.assertEqual([item["status"] for item in serialized["coverage"]], ["available", "partial", "unknown"])

    def test_successful_query_with_all_dates_out_of_range_stays_tool_success(self):
        payload = success_payload([
            forecast("2026-10-09", dayweather="晴", nightweather="晴"),
        ])

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.tool_status, "ok")
        self.assertTrue(all(item.status == "unknown" for item in result.coverage))
        self.assertTrue(all(item.reason == "out_of_coverage" for item in result.coverage))
        self.assertEqual(result.fact_store, FactStore())

    def test_missing_provider_report_time_stays_unknown_in_projected_fact(self):
        payload = success_payload([
            forecast("2026-10-10", dayweather="晴", nightweather="晴"),
        ], reporttime=None)

        result = self.service_for(payload).run(make_context())

        projected = result.fact_store.weather[0]
        self.assertIsNone(projected.report_time)
        self.assertNotEqual(projected.report_time, projected.source.queried_at)

    def test_date_row_without_weather_fields_is_unknown_and_not_a_fact(self):
        payload = success_payload([forecast("2026-10-10", dayweather="  ", nightweather=None, daytemp="NaN")])

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.tool_status, "ok")
        self.assertEqual(result.coverage[0].status, "unknown")
        self.assertEqual(result.coverage[0].reason, "no_valid_fields")
        self.assertEqual(result.fact_store, FactStore())

    def test_projection_removes_nonfinite_temperatures_but_keeps_valid_weather_text(self):
        tool_source = FactSource(
            tool_call_id="weather-call-1",
            tool_name="maps_weather",
            api_version="v3",
            queried_at=datetime.fromisoformat("2026-10-09T02:00:00+00:00"),
            effective_params={"city": "上海"},
        )
        malformed_forecast = WeatherForecast.model_construct(
            forecast_date=date(2026, 10, 10),
            day_weather="晴",
            night_weather=None,
            day_temp_c=float("nan"),
            night_temp_c=float("inf"),
            day_wind=None,
            night_wind=None,
            day_power=None,
            night_power=None,
        )
        malformed_fact = WeatherFact.model_construct(
            fact_id="weather:weather-call-1",
            city="上海",
            adcode="310000",
            report_time=None,
            forecasts=(malformed_forecast,),
            source=tool_source,
            missing_dates=(),
        )
        adapter_result = ToolResult[WeatherFact].model_construct(
            tool_call_id="weather-call-1",
            status="ok",
            data=malformed_fact,
            missing_fields=(),
            source=tool_source,
            error=None,
        )

        class Adapter:
            def get_weather(self, *_args, **_kwargs):
                return adapter_result

        result = WeatherQueryService(adapter=Adapter()).run(make_context())

        self.assertEqual(result.coverage[0].status, "partial")
        self.assertEqual(projected_forecast(result, date(2026, 10, 10)).day_weather, "晴")
        self.assertIsNone(projected_forecast(result, date(2026, 10, 10)).day_temp_c)
        self.assertIsNone(result.fact_store.weather[0].forecasts[0].night_temp_c)
        raw_temperature = result.diagnostic_tool_result.data.forecasts[0].day_temp_c
        self.assertTrue(raw_temperature != raw_temperature)

    def test_duplicate_provider_date_is_unknown_without_selecting_either_row(self):
        payload = success_payload([
            forecast("2026-10-10", dayweather="晴", nightweather="晴"),
            forecast("2026-10-10", dayweather="雨", nightweather="雨"),
            forecast("2026-10-11", dayweather="阴", nightweather="阴"),
        ])

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.tool_status, "ok")
        self.assertEqual(result.coverage[0].status, "unknown")
        self.assertEqual(result.coverage[0].reason, "duplicate_provider_date")
        self.assertEqual(result.coverage[1].status, "available")
        projected = result.fact_store.weather[0]
        self.assertEqual([item.forecast_date for item in projected.forecasts], [date(2026, 10, 11)])
        self.assertEqual(projected.missing_dates, (date(2026, 10, 10), date(2026, 10, 12)))

    def test_wrong_city_keeps_raw_result_for_diagnostics_but_no_consumable_fact(self):
        payload = success_payload([
            forecast("2026-10-10", dayweather="晴", nightweather="晴"),
        ], city="北京市", adcode="110000")

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.tool_status, "ok")
        self.assertEqual(result.projection_error, "city_mismatch")
        self.assertTrue(all(item.reason == "city_mismatch" for item in result.coverage))
        self.assertEqual(result.fact_store, FactStore())
        self.assertEqual(result.diagnostic_tool_result.data.city, "北京市")

    def test_provider_city_missing_is_unverified_and_adcode_match_can_verify(self):
        missing_city = success_payload([
            forecast("2026-10-10", dayweather="晴", nightweather="晴"),
        ], city=[], adcode="310000")
        text_city_result = self.service_for(missing_city).run(make_context())
        self.assertEqual(text_city_result.projection_error, "city_unverified")
        self.assertEqual(text_city_result.coverage[0].reason, "city_unverified")
        self.assertEqual(text_city_result.fact_store, FactStore())
        self.assertEqual(
            [(item.field, item.reason) for item in text_city_result.diagnostic_tool_result.missing_fields if item.field == "city"],
            [("city", "not_returned")],
        )

        adcode_input = make_planning_input(city="310000")
        adcode_result = self.service_for(missing_city).run(make_context(adcode_input))
        self.assertIsNone(adcode_result.projection_error)
        self.assertEqual(adcode_result.coverage[0].status, "available")
        self.assertEqual(adcode_result.fact_store.weather[0].city, "310000")

    def test_tool_error_is_locatable_and_every_requested_date_stays_unknown(self):
        payload = {
            "result_status": "error",
            "result_error": {"category": "network", "provider_code": "NETWORK_ERROR"},
            "result_source": source(),
        }

        result = self.service_for(payload).run(make_context())

        self.assertEqual(result.tool_status, "error")
        self.assertEqual(result.diagnostic_tool_result.error.category, "network")
        self.assertTrue(all(item.reason == "query_error" for item in result.coverage))
        self.assertEqual(result.fact_store, FactStore())

    def test_preflight_rejects_duplicate_planning_dates_before_any_tool_call(self):
        planning_input = make_planning_input()
        duplicate_days = (planning_input.days[0], planning_input.days[0], planning_input.days[2])
        invalid_input = planning_input.model_copy(update={"days": duplicate_days})
        context = WeatherQueryContext.model_construct(
            request_id=planning_input.request.request_id,
            run_id="weather-run-1",
            context_revision=2,
            base_revision=1,
            planning_input=invalid_input,
        )
        calls = []
        service = WeatherQueryService(adapter=AmapContractAdapter(lambda *args: calls.append(args)))

        with self.assertRaises(PlanningError) as raised:
            service.run(context)

        self.assertEqual(raised.exception.code, "WEATHER_CONTEXT_INVALID")
        self.assertEqual(raised.exception.step, "weather_preflight")
        self.assertEqual(calls, [])

    def test_preflight_rejects_context_request_mismatch_without_fabricating_tool_result(self):
        context = make_context().model_copy(update={"request_id": "other-request"})
        calls = []
        service = WeatherQueryService(adapter=AmapContractAdapter(lambda *args: calls.append(args)))

        with self.assertRaises(PlanningError) as raised:
            service.run(context)

        self.assertEqual(raised.exception.code, "WEATHER_CONTEXT_INVALID")
        self.assertEqual(calls, [])

    def test_service_uses_shared_executor_contract_and_configured_timeout(self):
        class ExecutorSpy:
            def __init__(self):
                self.calls = []

            def run(self, tool_name, arguments, *, timeout_seconds, cancel_event=None):
                self.calls.append((tool_name, arguments, timeout_seconds, cancel_event))
                return success_payload([forecast("2026-10-10", dayweather="晴", nightweather="晴")])

        executor = ExecutorSpy()
        cancellation = threading.Event()
        service = WeatherQueryService(tool_timeout_seconds=7.5)

        with patch("app.services.weather_query_service.get_mcp_tool_executor", return_value=executor) as get_shared:
            result = service.run(make_context(), cancel_event=cancellation)

        self.assertEqual(result.tool_status, "ok")
        self.assertEqual(executor.calls, [("maps_weather", {"city": "上海"}, 7.5, cancellation)])
        get_shared.assert_called_once_with()

    def test_sync_cancellation_before_tool_start_returns_unknown_without_calling_provider(self):
        calls = []
        executor = MCPToolExecutor(lambda *args: calls.append(args))
        cancellation = threading.Event()
        cancellation.set()
        service = WeatherQueryService(tool_executor=executor, tool_timeout_seconds=1)
        try:
            result = service.run(make_context(), cancel_event=cancellation)
        finally:
            executor.close(wait=True)

        self.assertEqual(result.tool_status, "error")
        self.assertEqual(result.diagnostic_tool_result.error.category, "timeout")
        self.assertTrue(all(item.reason == "query_error" for item in result.coverage))
        self.assertEqual(calls, [])

    def test_async_cancellation_returns_promptly_and_keeps_started_mcp_call_single(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def actual_tool(tool_name, arguments):
            calls.append((tool_name, arguments))
            started.set()
            release.wait(2)
            return success_payload([forecast("2026-10-10", dayweather="晴", nightweather="晴")])

        executor = MCPToolExecutor(actual_tool)
        service = WeatherQueryService(tool_executor=executor, tool_timeout_seconds=3)

        async def cancel_running_query():
            task = asyncio.create_task(service.run_async(make_context()))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.3)

        try:
            asyncio.run(cancel_running_query())
        finally:
            release.set()
            executor.close(wait=True)

        self.assertEqual(calls, [("maps_weather", {"city": "上海"})])


if __name__ == "__main__":
    unittest.main()
