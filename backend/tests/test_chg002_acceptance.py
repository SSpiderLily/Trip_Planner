"""CHG-002 v4 acceptance checks for time, price, and trusted edits."""
import copy
import unittest
from types import SimpleNamespace
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.agents.route_planner import RouteTripPlanner
from app.models.schemas import TripRequest
from app.models.itinerary import Place
from app.services.cost_service import queried_reference_cost, summarize_day_costs
from app.services.amap_service import AmapService
from app.services.route_options_service import RouteOptionsService, choose_mode
from app.services.schedule_service import schedule_day
from app.services.day_edit_service import DayEditService
from app.api.main import app


def route(target, mode, duration, status="available", source="amap"):
    return {
        "from_activity_id": "origin",
        "to_activity_id": target,
        "selected_mode": mode,
        "options": {
            mode: {
                "mode": mode,
                "duration_minutes": duration,
                "status": status,
                "source": source,
            }
        },
    }


class TimeAcceptanceTest(unittest.TestCase):
    def test_unknown_leg_does_not_hide_known_overrun_or_create_times(self):
        day = {
            "date": "2026-10-01",
            "activities": [
                {"activity_id": "a", "duration_minutes": 90, "start_at": "stale", "end_at": "stale"},
                {"activity_id": "b", "duration_minutes": 650, "start_at": "stale", "end_at": "stale"},
            ],
            "legs": [route("a", "walking", None, status="failed")],
        }

        result = schedule_day(day, {})

        self.assertEqual(result["time_summary"]["unknown_leg_count"], 1)
        self.assertFalse(result["time_summary"]["complete"])
        self.assertEqual(result["time_summary"]["known_minutes"], 740)
        self.assertEqual(result["time_summary"]["possible_overrun_minutes"], 140)
        self.assertEqual(result["time_summary"]["status"], "possible_overrun")
        self.assertTrue(all(a["start_at"] is None and a["end_at"] is None for a in result["activities"]))

    def test_same_day_arrival_and_departure_each_get_one_buffer_and_routes_count_once(self):
        day = {
            "date": "2026-10-01",
            "activities": [{"activity_id": "a", "duration_minutes": 120}],
            "legs": [
                route("a", "walking", 10),
                {**route("departure", "transit", 15), "from_activity_id": "a"},
            ],
        }

        result = schedule_day(day, {
            "arrival_at": "2026-10-01T10:00:00+08:00",
            "departure_at": "2026-10-01T15:00:00+08:00",
        })

        self.assertEqual(result["activities"][0]["start_at"], "2026-10-01T10:40+08:00")
        self.assertEqual(result["activities"][0]["end_at"], "2026-10-01T12:40+08:00")
        self.assertEqual(result["time_summary"]["buffer_minutes"], 60)
        self.assertEqual(result["time_summary"]["known_minutes"], 205)
        self.assertEqual(result["time_summary"]["possible_overrun_minutes"], 0)

    def test_middle_day_has_no_endpoint_buffer(self):
        day = {"date": "2026-10-02", "activities": [{"activity_id": "a", "duration_minutes": 599}], "legs": []}

        result = schedule_day(day, {})

        self.assertEqual(result["time_summary"]["buffer_minutes"], 0)
        self.assertEqual(result["time_summary"]["budget_minutes"], 600)
        self.assertEqual(result["time_summary"]["known_minutes"], 599)
        self.assertEqual(result["time_summary"]["status"], "within_budget")


class CostAcceptanceTest(unittest.TestCase):
    def test_zero_real_price_is_known_but_missing_price_remains_unknown(self):
        result = summarize_day_costs([
            {"type": "sightseeing", "reference_cost": {
                "amount": 0, "unit": "person", "source": "amap", "status": "available",
            }},
            {"type": "meal", "reference_cost": {"amount": None, "status": "missing"}},
        ])

        self.assertEqual(result["tickets"]["known_total"], 0)
        self.assertEqual(result["tickets"]["unknown_count"], 0)
        self.assertEqual(result["meals"]["unknown_count"], 1)
        self.assertEqual(result["known_total"], 0)
        self.assertEqual(result["unknown_count"], 1)
        self.assertFalse(result["complete"])

    def test_model_estimate_is_not_a_query_price(self):
        missing = queried_reference_cost(
            {"estimated_cost": {"amount": 500, "basis": "model estimate"}},
            "meal",
        )
        result = summarize_day_costs([
            {"type": "meal", "estimated_cost": {"amount": 500}, "reference_cost": missing},
        ])

        self.assertEqual(missing["status"], "missing")
        self.assertIsNone(missing["amount"])
        self.assertEqual(result["known_total"], 0)
        self.assertEqual(result["meals"]["unknown_count"], 1)

    def test_lodging_requires_room_night_unit_and_multiplies_only_known_nights(self):
        person_price = queried_reference_cost({"reference_cost": 120, "cost_basis": "person"}, "lodging")
        room_night_price = queried_reference_cost({"reference_cost": 120, "cost_basis": "room_night"}, "lodging")
        missing_result = summarize_day_costs([], lodging_base={"reference_cost": person_price},
                                             lodging_nights=2, include_lodging_unknown=True)
        known_result = summarize_day_costs([], lodging_base={"reference_cost": room_night_price}, lodging_nights=2)

        self.assertEqual(person_price["status"], "missing")
        self.assertEqual(missing_result["lodging"]["unknown_count"], 1)
        self.assertEqual(known_result["lodging"]["known_total"], 240)
        self.assertEqual(known_result["unknown_count"], 0)


class RouteAcceptanceTest(unittest.TestCase):
    def test_edit_created_route_uses_fastest_available_and_preference_breaks_ties(self):
        options = {
            "walking": {"status": "available", "duration_minutes": 20},
            "bicycling": {"status": "available", "duration_minutes": 15},
            "transit": {"status": "available", "duration_minutes": 15},
        }

        self.assertEqual(choose_mode(options, "transit"), "transit")
        self.assertEqual(choose_mode(options, "walking"), "bicycling")

    def test_explicitly_selected_mode_survives_a_failed_refresh(self):
        options = {
            "walking": {"status": "failed", "duration_minutes": None},
            "transit": {"status": "available", "duration_minutes": 25},
        }

        self.assertEqual(choose_mode(options, "transit", preserve="walking"), "walking")

    def test_route_query_failure_is_unknown_and_does_not_turn_into_zero(self):
        def fail_query(_tool_name, _arguments):
            raise RuntimeError("redacted provider error")

        service = RouteOptionsService(fail_query)
        options = service.options(
            {"source_id": "A", "longitude": 0, "latitude": 0},
            {"source_id": "B", "longitude": 0.01, "latitude": 0.01},
            "北京",
        )

        self.assertEqual(set(options), {"walking", "bicycling", "transit"})
        self.assertTrue(all(option["status"] == "failed" for option in options.values()))
        self.assertTrue(all(option["duration_minutes"] is None for option in options.values()))
        self.assertIsNone(choose_mode(options, "transit"))
        self.assertEqual(service.query_count, 3)

    def test_initial_route_follows_home_preference_and_still_marks_fastest(self):
        class Options:
            def options(self, *_args, **_kwargs):
                return {
                    "walking": {"status": "available", "duration_minutes": 20, "distance_m": 1000, "source": "amap"},
                    "bicycling": {"status": "available", "duration_minutes": 8, "distance_m": 900, "source": "amap"},
                    "transit": {"status": "available", "duration_minutes": 12, "distance_m": 1300, "source": "amap"},
                }

        planner = RouteTripPlanner.__new__(RouteTripPlanner)
        state = {
            "conditions": SimpleNamespace(transportation="walking"),
            "request": SimpleNamespace(city="北京"),
            "route_service": Options(),
        }
        origin = Place(source_id="a", name="甲", longitude=116.1, latitude=39.1)
        destination = Place(source_id="b", name="乙", longitude=116.2, latitude=39.2)

        leg = planner.route("a", origin, "b", destination, "2026-10-01", 0, state)

        self.assertEqual(leg["selected_mode"], "walking")
        self.assertEqual(leg["selection_source"], "preference")
        self.assertEqual(leg["fastest_mode"], "bicycling")
        self.assertEqual(leg["duration_minutes"], 20)


class AmapBoundaryAcceptanceTest(unittest.TestCase):
    class FakeMcp:
        def __init__(self, responses):
            self.responses = responses
            self.calls = []

        def run(self, payload):
            tool_name = payload["tool_name"]
            self.calls.append((tool_name, payload["arguments"]))
            return self.responses[tool_name]

    def test_search_fills_missing_search_coordinates_from_detail(self):
        fake = self.FakeMcp({
            "maps_text_search": {"pois": [{"id": "p1", "name": "地点一", "address": "北京"}]},
            "maps_search_detail": {"data": {
                "id": "p1", "name": "地点一", "address": "北京", "location": "116.1,39.1",
            }},
        })

        pois = AmapService(fake).search_poi("地点一", "北京")

        self.assertEqual([poi.id for poi in pois], ["p1"])
        self.assertEqual(pois[0].location.longitude, 116.1)
        self.assertEqual([call[0] for call in fake.calls], ["maps_text_search", "maps_search_detail"])

    def test_weather_reads_nested_forecasts_without_borrowing_dates(self):
        fake = self.FakeMcp({"maps_weather": {"data": {"forecasts": [
            {"date": "2026-10-02", "dayweather": "晴", "nightweather": "多云", "daytemp": "23℃"},
        ]}}})

        weather = AmapService(fake).get_weather("北京")

        self.assertEqual([item.date for item in weather], ["2026-10-02"])
        self.assertEqual(weather[0].day_temp, 23)
        self.assertEqual(weather[0].night_weather, "多云")

    def test_explicit_zero_map_price_is_preserved_as_real_reference(self):
        poi = AmapService._poi({
            "id": "p1", "name": "免费公园", "location": "116.1,39.1", "cost": 0,
        })

        self.assertIsNotNone(poi)
        self.assertEqual(poi.reference_cost, 0)
        self.assertEqual(poi.cost_basis, "reference")


class InputBoundaryAcceptanceTest(unittest.TestCase):
    def test_creation_requires_both_full_arrival_and_departure_datetimes(self):
        with self.assertRaises(ValidationError):
            TripRequest(city="北京", start_date="2026-10-01", end_date="2026-10-01",
                        arrival_at="2026-10-01T10:00:00+08:00")
        with self.assertRaises(ValidationError):
            TripRequest(city="北京", start_date="2026-10-01", end_date="2026-10-01",
                        departure_at="2026-10-01T18:00:00+08:00")

    def test_departure_must_be_strictly_after_arrival(self):
        for departure in ("2026-10-01T10:00:00+08:00", "2026-10-01T09:59:00+08:00"):
            with self.subTest(departure=departure), self.assertRaises(ValidationError):
                TripRequest(city="北京", start_date="2026-10-01", end_date="2026-10-01",
                            arrival_at="2026-10-01T10:00:00+08:00", departure_at=departure)

    def test_local_natural_date_range_cannot_exceed_30_days(self):
        with self.assertRaises(ValidationError):
            TripRequest(city="北京", start_date="2026-10-01", end_date="2026-10-31",
                        arrival_at="2026-10-01T09:00:00+08:00",
                        departure_at="2026-10-31T18:00:00+08:00")

    def test_offset_timestamps_are_validated_after_conversion_to_shanghai_date(self):
        request = TripRequest(city="北京", arrival_at="2026-09-30T16:30:00+00:00",
                              departure_at="2026-10-01T03:30:00+08:00")

        self.assertEqual(request.start_date, "2026-10-01")
        self.assertEqual(request.end_date, "2026-10-01")


def _place(identity, longitude):
    return {"source": "amap", "source_id": identity, "name": f"地点{identity}", "address": "北京",
            "longitude": longitude, "latitude": 39.9}


def _activity(identity, longitude, requirements=()):
    return {"activity_id": identity, "type": "sightseeing", "period": "morning", "title": f"地点{identity}",
            "description": "", "duration_minutes": 90, "place": _place(identity, longitude),
            "requirement_ids": list(requirements), "opening_hours": "09:00-18:00",
            "reference_cost": {"amount": 0, "unit": "reference", "source": "amap", "status": "available"},
            "start_at": "2026-10-01T09:00+08:00", "end_at": "2026-10-01T10:30+08:00"}


def _leg(origin, destination, selected="walking"):
    return {"leg_id": f"{origin}->{destination}", "from_activity_id": origin, "to_activity_id": destination,
            "origin": _place(origin, 116.1 if origin == "a" else 116.2),
            "destination": _place(destination, 116.2 if destination == "b" else 116.3),
            "selected_mode": selected, "selection_source": "preference", "fastest_mode": "bicycling",
            "duration_minutes": 20,
            "options": {"walking": {"mode": "walking", "duration_minutes": 20, "status": "available", "source": "amap"},
                        "bicycling": {"mode": "bicycling", "duration_minutes": 15, "status": "available", "source": "amap"},
                        "transit": {"mode": "transit", "duration_minutes": 25, "status": "available", "source": "amap"}}}


def _day(day_date, activities, legs):
    return {"day_id": f"day-{day_date}", "date": day_date, "description": "按序安排", "activities": activities, "legs": legs,
            "weather": None, "time_summary": {"known_minutes": 200, "unknown_leg_count": 0,
                                                "possible_overrun_minutes": 0, "status": "within_budget"},
            "cost_summary": summarize_day_costs(activities), "issues": []}


class _MemoryRepository:
    def __init__(self, result):
        self.saved = copy.deepcopy(result)

    def status(self, task_id):
        return {"status": "succeeded"} if task_id == "task-1" else None

    def result(self, task_id):
        return copy.deepcopy(self.saved) if task_id == "task-1" else None

    def request(self, task_id):
        return {"city": "北京"} if task_id == "task-1" else None


class SignedDayEditEndpointAcceptanceTest(unittest.TestCase):
    """Exercise the actual FastAPI route with an isolated in-memory trusted task."""

    def setUp(self):
        self.first = _day("2026-10-01", [_activity("a", 116.1, ["req-must"]),
                                           _activity("b", 116.2), _activity("c", 116.3)],
                          [_leg("a", "b"), _leg("b", "c")])
        self.second = _day("2026-10-02", [], [])
        self.original = {"schema_version": 3,
                         "planning_conditions": {"city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-02",
                                                 "arrival_at": "2026-10-01T09:00+08:00", "departure_at": "2026-10-02T18:00+08:00",
                                                 "transportation": "walking", "budget_per_person": 1000,
                                                 "must_visit_requests": [{"requirement_id": "req-must", "name": "必去地点"}]},
                         "lodging_base": {"source": "recommended", "place": None, "reference_cost": None},
                         "days": [self.first, self.second], "issues": [],
                         "cost_summary": {"known_total": 0, "unknown_count": 0, "complete": True}}
        self.repository = _MemoryRepository(self.original)
        self.editor = DayEditService(self.repository, signing_key=b"acceptance-only-key" * 2,
                                     query_tool=lambda _name, _args: {"paths": [{"duration": "600"}]},
                                     poi_resolver=lambda identity, _city: {
                                         "id": identity, "name": f"地点{identity}", "address": "北京",
                                         "location": {"longitude": 116.25, "latitude": 39.9},
                                     })
        app.state.day_edit_service = self.editor
        self.client = TestClient(app)
        self.token = self.editor.attach_tokens("task-1", self.original)["days"][0]["edit_token"]

    def tearDown(self):
        if hasattr(app.state, "day_edit_service"):
            delattr(app.state, "day_edit_service")

    def payload(self, operations, token=None, **extra):
        return {"task_id": "task-1", "date": "2026-10-01", "edit_token": token or self.token,
                "client_revision": 1, "request_id": "acceptance-1", "operations": operations, **extra}

    def post(self, payload):
        return self.client.post("/api/trip/recalculate-day", json=payload)

    def test_endpoint_rejects_missing_task_and_tampered_signature(self):
        missing = self.payload([{"type": "move_activity", "activity_id": "a", "direction": "down"}], task_id="absent")
        self.assertEqual(self.post(missing).status_code, 404)
        tampered = self.token[:-1] + ("0" if self.token[-1] != "0" else "1")
        response = self.post(self.payload([{"type": "move_activity", "activity_id": "a", "direction": "down"}], token=tampered))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "EDIT_TOKEN_INVALID")

    def test_client_cannot_overwrite_global_plan_or_another_day(self):
        before = copy.deepcopy(self.repository.saved)
        response = self.post(self.payload([{"type": "select_leg_mode", "from_activity_id": "a",
                                            "to_activity_id": "b", "mode": "transit"}],
                                          planning_conditions={"city": "伪造城市"}, revoked_requirement_ids=["req-must"],
                                          day={"activities": [], "legs": []}))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["day"]["legs"][0]["selected_mode"], "transit")
        self.assertEqual(self.repository.saved, before)
        self.assertEqual(self.repository.saved["planning_conditions"]["city"], "北京")
        self.assertEqual(self.repository.saved["days"][1], before["days"][1])

    def test_same_day_duplicate_and_add_then_move_preserve_operation_order(self):
        duplicate = self.post(self.payload([{"type": "add_poi", "poi_id": "a", "client_activity_id": "new-a"}]))
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "POI_ALREADY_IN_DAY")

        added = self.post(self.payload([{"type": "add_poi", "poi_id": "new-poi", "client_activity_id": "new-activity"}],
                                       request_id="add-1"))
        self.assertEqual(added.status_code, 200, added.text)
        ids_after_add = [item["activity_id"] for item in added.json()["day"]["activities"]]
        self.assertIn("new-activity", ids_after_add)
        self.assertEqual([item for item in ids_after_add if item != "new-activity"], ["a", "b", "c"])

        # Sequential operations are applied to the signed working day; B moves up then back down.
        response = self.post(self.payload([{"type": "move_activity", "activity_id": "b", "direction": "up"},
                                          {"type": "move_activity", "activity_id": "b", "direction": "down"}]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["activity_id"] for item in response.json()["day"]["activities"]], ["a", "b", "c"])
        self.assertGreaterEqual(len(response.json()["day"]["edit_token"]), 20)

    def test_must_visit_delete_revocation_survives_a_later_edit(self):
        removed = self.post(self.payload([{"type": "delete_activity", "activity_id": "a", "confirmed": True}]))
        self.assertEqual(removed.status_code, 200, removed.text)
        first_response = removed.json()["day"]
        self.assertNotIn("MUST_VISIT_MISSING", {issue["code"] for issue in first_response["issues"]})
        self.assertEqual([item["activity_id"] for item in first_response["activities"]], ["b", "c"])

        later = self.post(self.payload([{"type": "move_activity", "activity_id": "c", "direction": "up"}],
                                       token=first_response["edit_token"], request_id="acceptance-2", client_revision=2))
        self.assertEqual(later.status_code, 200, later.text)
        second_response = later.json()["day"]
        self.assertNotIn("MUST_VISIT_MISSING", {issue["code"] for issue in second_response["issues"]})
        self.assertEqual([item["activity_id"] for item in second_response["activities"]], ["c", "b"])
        self.assertIn("req-must", self.repository.saved["days"][0]["activities"][0]["requirement_ids"])

    def test_deleting_all_activities_leaves_an_empty_day_without_stale_legs(self):
        response = self.post(self.payload([{"type": "delete_activity", "activity_id": identity, "confirmed": True}
                                           for identity in ("a", "b", "c")]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["day"]["activities"], [])
        self.assertEqual(response.json()["day"]["legs"], [])


if __name__ == "__main__":
    unittest.main()
