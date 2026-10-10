"""AMap HTTP 包装和 planning_v4 类型化适配测试，不访问外网。"""
from datetime import date, datetime
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.models.planning_v4.facts import FactSource, MapCoordinate, PlaceFact
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.amap_service import AmapService
from app.agents.route_planner import RouteTripPlanner


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
with patch.dict(os.environ, {"AMAP_MAPS_API_KEY": "fixture-key", "AMAP_HTTP_TIMEOUT_SECONDS": "7"}):
    vendor = importlib.import_module("vendor.amap_mcp_server_0_1_11")


class Response:
    def __init__(self, payload=None, status_code=200, error=None):
        self.payload = payload
        self.status_code = status_code
        self.error = error

    def raise_for_status(self):
        if self.error:
            import requests
            raise requests.HTTPError(self.error, response=self)

    def json(self):
        return self.payload


def vendor_source(params, version="v3"):
    return {
        "provider": "amap", "api_version": version,
        "queried_at": "2026-10-10T00:00:00+00:00",
        "effective_params": dict(params),
    }


class AmapContractAdapterTest(unittest.TestCase):
    def test_http_wrapper_applies_timeout_and_search_filters_pagination(self):
        payload = {"status": "1", "count": "0", "pois": []}
        with patch.object(vendor.requests, "get", return_value=Response(payload)) as request:
            result = vendor.maps_text_search("外滩", "上海", "true", "风景名胜", "2", "10", "all")
        self.assertEqual(result["result_status"], "empty")
        _, kwargs = request.call_args
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(kwargs["params"]["types"], "风景名胜")
        self.assertEqual(kwargs["params"]["page"], "2")
        self.assertEqual(kwargs["params"]["offset"], "10")
        self.assertEqual(result["result_source"]["effective_params"]["page"], "2")
        self.assertNotIn("key", result["result_source"]["effective_params"])

    def test_provider_and_http_errors_are_classified_without_echoing_secret(self):
        for code, category in (("10001", "auth"), ("10004", "rate_limit"), ("20000", "invalid_request")):
            with patch.object(vendor.requests, "get", return_value=Response({"status": "0", "infocode": code})):
                result = vendor.maps_text_search("外滩", "上海")
            self.assertEqual(result["result_error"]["category"], category)
            self.assertEqual(result["result_error"]["provider_code"], code)
            self.assertNotIn("fixture-key", json.dumps(result))
        for code, category in ((401, "auth"), (429, "rate_limit")):
            with patch.object(vendor.requests, "get", return_value=Response(status_code=code, error="fixture-key?key=fixture-key")):
                result = vendor.maps_text_search("外滩", "上海")
            self.assertEqual(result["result_error"]["category"], category)
            self.assertNotIn("fixture-key", json.dumps(result))

    def test_malicious_transport_exception_is_sanitized(self):
        import requests
        with patch.object(vendor.requests, "get", side_effect=requests.ConnectionError("fixture-key https://example.invalid/?key=fixture-key")):
            result = vendor.maps_text_search("外滩", "上海")
        rendered = json.dumps(result)
        self.assertEqual(result["result_error"]["category"], "network")
        self.assertNotIn("fixture-key", rendered)
        self.assertNotIn("example.invalid", rendered)

    def test_malformed_and_empty_provider_responses_are_distinct(self):
        with patch.object(vendor.requests, "get", return_value=Response({"status": "1"})):
            missing = vendor.maps_text_search("外滩", "上海")
        self.assertEqual(missing["result_status"], "error")
        self.assertEqual(missing["result_error"]["category"], "invalid_response")
        with patch.object(vendor.requests, "get", return_value=Response({"status": "1", "pois": [], "count": "0"})):
            empty = vendor.maps_text_search("外滩", "上海")
        self.assertEqual(empty["result_status"], "empty")

    def test_transit_http_params_include_requested_date_time_and_strategy(self):
        payload = {"status": "1", "route": {"origin": "121.4,31.2", "destination": "121.5,31.2", "transits": []}}
        with patch.object(vendor.requests, "get", return_value=Response(payload)) as request:
            result = vendor.maps_direction_transit_integrated_by_coordinates(
                "121.4,31.2", "121.5,31.2", "上海", "上海",
                date="2026-10-11", time="09:00", strategy="0",
            )
        sent = request.call_args.kwargs["params"]
        self.assertEqual((sent["date"], sent["time"], sent["strategy"]), ("2026-10-11", "09:00", "0"))
        self.assertEqual(result["result_status"], "empty")
        self.assertEqual(result["result_source"]["effective_params"]["date"], "2026-10-11")
        self.assertNotIn("key", result["result_source"]["effective_params"])

    def test_detail_preserves_biz_ext_and_old_flat_fields_for_consumers(self):
        payload = {"status": "1", "pois": [{
            "id": "poi-1", "name": "测试地点", "location": "121.5,31.2", "cost": "42",
            "biz_ext": {"cost": "35", "opentime": "08:00-20:00"},
        }]}
        with patch.object(vendor.requests, "get", return_value=Response(payload)):
            detail = vendor.maps_search_detail("poi-1")
        self.assertEqual(detail["cost"], "42")
        self.assertEqual(detail["opentime"], "08:00-20:00")
        self.assertEqual(detail["biz_ext"]["cost"], "35")

        class FakeMcp:
            def run(self, _arguments):
                return json.dumps(detail, ensure_ascii=False)
        parsed = AmapService(FakeMcp()).get_poi_info("poi-1")
        self.assertEqual(parsed["reference_cost"], 42.0)
        self.assertEqual(parsed["opening_hours"], "08:00-20:00")
        legacy = RouteTripPlanner._reference_metadata(detail)
        self.assertEqual(legacy["reference_cost"], 42.0)
        self.assertEqual(legacy["opening_hours"], "08:00-20:00")

    def test_place_parser_handles_array_fields_unknown_cost_and_missing_coordinates(self):
        raw = {
            "result_status": "ok", "count": "1",
            "result_source": vendor_source({"keywords": "酒店"}),
            "pois": [{"id": "p-1", "name": "Hotel", "location": None,
                      "address": [], "cityname": [], "adcode": [], "cost": "约35元"}],
        }
        result = AmapContractAdapter(lambda *_: raw).search_text("酒店", "上海")
        self.assertEqual(result.status, "ok")
        place = result.data.places[0]
        self.assertIsNone(place.location)
        self.assertIsNone(place.address)
        self.assertIsNone(place.city)
        self.assertIsNone(place.adcode)
        self.assertEqual(place.cost.raw, "约35元")
        self.assertIsNone(place.cost.value)
        self.assertIn("location", {item.field for item in place.missing_fields})

    def test_weather_keeps_report_date_timezone_negative_temperature_and_coverage(self):
        raw = {
            "result_status": "ok", "city": "上海", "adcode": "310101",
            "reporttime": "2026-10-09 11:30:00",
            "forecasts": [{"date": "2026-10-10", "dayweather": "晴", "nightweather": "多云",
                           "daytemp": "-2", "nighttemp": "-5"}],
            "result_source": vendor_source({"city": "310101"}),
        }
        result = AmapContractAdapter(lambda *_: raw).get_weather(
            "310101", (date(2026, 10, 10), date(2026, 10, 11)),
        )
        self.assertEqual(result.data.report_time.isoformat(), "2026-10-09T11:30:00+08:00")
        self.assertEqual(result.data.forecasts[0].day_temp_c, -2)
        self.assertEqual(result.data.missing_dates, (date(2026, 10, 11),))

    def test_missing_metadata_never_claims_transit_date_was_sent(self):
        raw = {"result_status": "ok", "route": {"transits": [{"duration": "600", "segments": []}]}}
        runner = lambda *_: raw
        origin = PlaceFact(fact_id="o", poi_id="p-o", name="Origin", location=MapCoordinate(lng=121.4, lat=31.2), source_refs=("s",))
        destination = PlaceFact(fact_id="d", poi_id="p-d", name="Destination", location=MapCoordinate(lng=121.5, lat=31.2), source_refs=("s",))
        result = AmapContractAdapter(runner).plan_route(
            origin, destination, "transit", city="上海",
            departure_at=datetime.fromisoformat("2026-10-11T09:00:00+08:00"), strategy=0,
        )
        self.assertEqual(result.data.time_verification, "not_sent")
        self.assertIsNone(result.data.provider_departure_at)
        self.assertEqual(result.source.effective_params, {})

    def test_transit_options_keep_alternative_lines_and_partial_geometry_separate(self):
        origin = PlaceFact(fact_id="o", poi_id="p-o", name="Origin", location=MapCoordinate(lng=121.4, lat=31.2), source_refs=("s",))
        destination = PlaceFact(fact_id="d", poi_id="p-d", name="Destination", location=MapCoordinate(lng=121.5, lat=31.2), source_refs=("s",))
        departure = datetime.fromisoformat("2026-10-11T09:00:00+08:00")
        args_seen = {}
        def runner(tool_name, arguments):
            args_seen.update(arguments)
            params = {key: value for key, value in arguments.items()}
            return {
                "result_status": "ok",
                "result_source": vendor_source(params),
                "route": {"transits": [
                    {"duration": "900", "distance": "2200", "cost": "3", "segments": [
                        {"walking": {"steps": [{"distance": "100", "duration": "120", "instruction": "步行", "polyline": "121.4,31.2;121.41,31.2"}]},
                         "bus": {"buslines": [
                             {"name": "一号线", "type": "地铁线路", "distance": "1500", "duration": "600",
                              "polyline": "121.41,31.2;121.45,31.2", "via_num": "2",
                              "departure_stop": {"name": "A站", "id": "a", "location": "121.41,31.2"},
                              "arrival_stop": {"name": "B站", "id": "b", "location": "121.45,31.2"},
                              "via_stops": [{"name": "中间站", "id": "m", "location": "121.43,31.2"}]},
                             {"name": "备用一号线", "type": "地铁线路", "distance": "1600", "duration": "650",
                              "polyline": "121.41,31.2;121.46,31.2"},
                         ]}, "entrance": {"name": "A口"}, "exit": {"name": "B口"}},
                    ]},
                    {"duration": "1200", "distance": "2500", "segments": [
                        {"walking": {"steps": [{"distance": "50", "duration": "80", "instruction": "道路施工绕行",
                                                "polyline": "121.4,31.2;bad-coordinate"}]}}
                    ]},
                ]},
            }
        result = AmapContractAdapter(runner).plan_route(
            origin, destination, "transit", city="上海", departure_at=departure, strategy=0,
        )
        self.assertEqual(args_seen["date"], "2026-10-11")
        self.assertEqual(args_seen["time"], "09:00")
        self.assertEqual(args_seen["strategy"], "0")
        self.assertEqual(result.data.time_verification, "sent_not_echoed")
        self.assertIsNone(result.data.provider_departure_at)
        self.assertEqual(len(result.data.options), 2)
        first, second = result.data.options
        self.assertEqual(first.geometry_status, "complete")
        self.assertEqual(second.geometry_status, "missing")
        bus_segment = first.segments[1]
        self.assertEqual(len(bus_segment.transit_alternatives), 2)
        self.assertEqual(bus_segment.transit_alternatives[0].via_stop_count, 2)
        self.assertEqual(bus_segment.transit_alternatives[0].departure_stop.name, "A站")
        self.assertEqual(bus_segment.entrance.name, "A口")
        self.assertEqual(first.provider_attributes["cost"], "3")

    def test_empty_array_optional_provider_text_fields_become_missing_values(self):
        origin = PlaceFact(fact_id="o", poi_id="p-o", name="Origin", location=MapCoordinate(lng=121.4, lat=31.2), source_refs=("s",))
        destination = PlaceFact(fact_id="d", poi_id="p-d", name="Destination", location=MapCoordinate(lng=121.5, lat=31.2), source_refs=("s",))

        def runner(tool_name, arguments):
            source = vendor_source(arguments)
            if tool_name == "maps_text_search":
                return {"result_status": "ok", "result_source": source, "count": "1", "pois": [{
                    "id": "poi-array-fields", "name": "地点", "location": "121.4,31.2",
                    "address": [], "cityname": [], "type": [], "typecode": [],
                    "business_area": [], "opentime": [],
                }]}
            if tool_name == "maps_weather":
                return {"result_status": "ok", "result_source": source, "city": [], "adcode": "310101",
                        "reporttime": "2026-10-10 08:00:00", "forecasts": [{
                            "date": "2026-10-11", "dayweather": [], "nightweather": [],
                            "daywind": [], "nightwind": [], "daypower": [], "nightpower": [],
                        }]}
            return {"result_status": "ok", "result_source": source, "route": {"transits": [{
                "duration": "600", "distance": "1000", "segments": [{
                    "walking": {"steps": [{"distance": "100", "duration": "60", "road": [],
                                            "instruction": [], "polyline": "121.4,31.2;121.41,31.2"}]},
                    "bus": {"buslines": [{"name": [], "type": [],
                                           "departure_stop": {"name": [], "id": []},
                                           "arrival_stop": {"name": [], "id": []},
                                           "polyline": "121.41,31.2;121.5,31.2"}]},
                }],
            }]}}

        adapter = AmapContractAdapter(runner)
        places = adapter.search_text("地点", "上海")
        place = places.data.places[0]
        self.assertIsNone(place.address)
        self.assertIsNone(place.city)
        self.assertIsNone(place.type_name)
        self.assertIsNone(place.typecode)
        self.assertIsNone(place.business_area)
        self.assertIsNone(place.opening_hours_raw)

        weather = adapter.get_weather("310101", (date(2026, 10, 11),))
        forecast = weather.data.forecasts[0]
        self.assertIsNone(forecast.day_weather)
        self.assertIsNone(forecast.night_weather)
        self.assertIsNone(forecast.day_wind)
        self.assertIsNone(forecast.day_power)

        route = adapter.plan_route(
            origin, destination, "transit", city="上海",
            departure_at=datetime.fromisoformat("2026-10-11T09:00:00+08:00"), strategy=0,
        )
        self.assertEqual(route.status, "ok")
        walking, bus = route.data.options[0].segments
        self.assertIsNone(walking.line_name)
        self.assertEqual(walking.instructions, ())
        transit_line = bus.transit_alternatives[0]
        self.assertIsNone(transit_line.line_name)
        self.assertIsNone(transit_line.line_type)
        self.assertIsNone(transit_line.departure_stop)
        self.assertIsNone(transit_line.arrival_stop)

    def test_invalid_response_version_and_json_are_typed_without_crashing(self):
        bad_version = {"result_status": "ok", "result_source": {"api_version": "v5", "effective_params": {}}, "pois": []}
        result = AmapContractAdapter(lambda *_: bad_version).search_text("x", "上海")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.category, "invalid_response")
        self.assertEqual(result.source.api_version, "v3")
        invalid_json = AmapContractAdapter(lambda *_: "<html>not json</html>").search_text("x", "上海")
        self.assertEqual(invalid_json.status, "error")
        self.assertEqual(invalid_json.error.category, "invalid_response")


if __name__ == "__main__":
    unittest.main()
