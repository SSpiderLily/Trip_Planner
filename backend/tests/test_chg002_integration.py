"""CHG-002 planner and signed-day integration tests (no provider/network access)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.route_planner import RouteTripPlanner
from app.agents.execution import PlanningError
from app.models.itinerary import Collection, Conditions, Draft, Place, Search
from app.models.schemas import DayEditOperation, RecalculateDayRequest, TripRequest
from pydantic import ValidationError
from app.services.cost_service import summarize_trip
from app.services.day_edit_service import DayEditService
from app.services.route_options_service import RouteOptionsService
from app.services.task_repository import TaskRepository
from app.services import amap_service


def ref_place(identity, longitude, *, opening_hours=None):
    return Place(source_id=identity, name=identity, address=identity,
                 longitude=longitude, latitude=39.9, opening_hours=opening_hours)


def fake_route(name, _arguments):
    # Return provider-shaped seconds. Walking is deliberately much slower for edit tests.
    durations = {
        "maps_direction_walking_by_coordinates": 110 * 60,
        "maps_bicycling_by_coordinates": 60 * 60,
        "maps_direction_transit_integrated_by_coordinates": 10 * 60,
        "maps_direction_driving_by_coordinates": 5 * 60,
    }
    if name not in durations:
        raise AssertionError(name)
    key = "transits" if "transit" in name else "paths"
    return {key: [{"duration": str(durations[name]), "distance": "1000"}]}


def plan_fixture(*, day_count=1, arrival=False, departure=False, durations=(90, 90), openings=None,
                 transportation="transit", forecasts=None, reserved_activity_id=None):
    dates = [f"2026-10-{day:02d}" for day in range(1, day_count + 1)]
    request = TripRequest(
        city="北京", arrival_at=f"{dates[0]}T08:00:00+08:00",
        departure_at=f"{dates[-1]}T20:00:00+08:00",
        arrival_place_id="arrival" if arrival else None,
        departure_place_id="departure" if departure else None,
    )
    arrival_place = ref_place("arrival", 116.30) if arrival else None
    departure_place = ref_place("departure", 116.38) if departure else None
    lodging = ref_place("hotel", 116.34)
    conditions = Conditions(
        city="北京", start_date=request.start_date, end_date=request.end_date,
        travel_days=request.travel_days, transportation=transportation,
        arrival_at=request.arrival_at.isoformat(), departure_at=request.departure_at.isoformat(),
        arrival_place_id=request.arrival_place_id, departure_place_id=request.departure_place_id,
        arrival_place=arrival_place, departure_place=departure_place,
    )
    pool = {p.source_id: {**p.model_dump(), "categories": ["lodging" if p.source_id == "hotel" else "endpoint"]}
            for p in [lodging, *( [arrival_place] if arrival_place else [] ), *( [departure_place] if departure_place else [] )]}
    for i in range(day_count * 2):
        opening = (openings or {}).get(f"s{i}")
        poi = ref_place(f"s{i}", 116.31 + i * 0.01, opening_hours=opening)
        pool[poi.source_id] = {**poi.model_dump(), "categories": ["sightseeing"], "opening_hours": opening,
                               "photos": [], "reference_cost": None, "cost_basis": None}
    raw_days = []
    for day_index, day_date in enumerate(dates):
        activities = []
        count = 2 if day_count == 1 else 1
        for ordinal in range(count):
            i = day_index * 2 + ordinal
            activity_id = reserved_activity_id if i == 0 and reserved_activity_id else f"a{i}"
            activities.append({"activity_id": activity_id, "type": "sightseeing", "period": "morning",
                               "title": f"景点{i}", "duration_minutes": durations[ordinal if day_count == 1 else 0],
                               "place": {"source_id": f"s{i}"}})
        raw_days.append({"date": day_date, "description": "实地候选行程", "activities": activities})
    draft = Draft.model_validate({
        "lodging_base": {"source": "recommended", "area_name": "住宿区域", "recommendation_reason": "靠近路线",
                          "place": {"source_id": "hotel"}},
        "days": raw_days,
    })
    planner = RouteTripPlanner.__new__(RouteTripPlanner)
    state = {
        "request": request, "conditions": conditions, "pool": pool,
        "route_service": RouteOptionsService(fake_route), "forecasts": forecasts or {}, "weather_error": None,
    }
    return planner.evaluate(draft, state), request


class InitialPlanningIntegrationTest(unittest.TestCase):
    def test_candidate_detail_enriches_real_metadata_and_normalizes_photos(self):
        request = TripRequest(city="北京", arrival_at="2026-10-01T08:00+08:00",
                              departure_at="2026-10-01T20:00+08:00")
        planner = RouteTripPlanner.__new__(RouteTripPlanner)
        planner.settings = SimpleNamespace(planner_place_query_limit=36)
        planner.collector = object()
        calls = []

        def tool(name, arguments):
            calls.append(name)
            if name == "maps_text_search":
                return {"pois": [{"id": "poi-1", "name": "地点一", "location": "116.1,39.9"}]}
            return {"id": "poi-1", "name": "地点一", "location": "116.1,39.9", "cost": "¥20元/人",
                    "opentime": "09:00-18:00", "photos": [{"url": "https://example.test/poi.jpg"}]}

        responses = [Collection(conditions=Conditions(city="北京"),
                                searches=[Search(keywords="景点", category="sightseeing")]),
                     Collection(searches=[])]
        planner.model = lambda *_args, **_kwargs: responses.pop(0)
        planner.tool = tool
        state = {"request": request, "pool": {}, "used": 0, "feedback": []}
        planner.collect(state, "sights")
        candidate = state["pool"]["poi-1"]
        self.assertEqual(candidate["reference_cost"], 20)
        self.assertEqual(candidate["opening_hours"], "09:00-18:00")
        self.assertEqual(candidate["photos"], ["https://example.test/poi.jpg"])
        self.assertEqual(calls, ["maps_text_search", "maps_search_detail"])

    def test_reserved_endpoint_ids_are_rejected_for_model_and_editor_activities(self):
        with self.assertRaises(PlanningError):
            plan_fixture(reserved_activity_id="departure")
        with self.assertRaises(ValidationError):
            DayEditOperation(type="add_poi", poi_id="new", client_activity_id="arrival")

    def test_arrival_and_departure_alone_derive_local_date_range(self):
        request = TripRequest(city="北京", arrival_at="2026-09-30T16:30:00Z",
                              departure_at="2026-10-01T14:00:00Z")
        self.assertEqual((request.start_date, request.end_date, request.travel_days),
                         ("2026-10-01", "2026-10-01", 1))

    def test_opening_conflict_is_reported_during_initial_evaluation(self):
        result, _ = plan_fixture(openings={"s0": "12:00-13:00"})
        conflicts = {(issue["code"], issue.get("activity_id")) for issue in result["issues"]}
        self.assertIn(("OPENING_CONFLICT", "a0"), conflicts)

    def test_short_walk_is_recommended_only_as_transit_first_last_mile(self):
        def query(name, _arguments):
            minutes = {"maps_direction_walking_by_coordinates": 8,
                       "maps_bicycling_by_coordinates": 16,
                       "maps_direction_transit_integrated_by_coordinates": 20}[name]
            key = "transits" if "transit" in name else "paths"
            return {key: [{"duration": minutes * 60, "distance": 500}]}

        # Exercise the planner's initial-route policy against fresh provider options.
        planner = RouteTripPlanner.__new__(RouteTripPlanner)
        state = {"conditions": Conditions(city="北京", transportation="transit"),
                 "request": TripRequest(city="北京", arrival_at="2026-10-01T08:00+08:00",
                                         departure_at="2026-10-01T20:00+08:00"),
                 "route_service": RouteOptionsService(query)}
        leg = planner.route("a", ref_place("A", 116.3), "b", ref_place("B", 116.31), "2026-10-01", 0, state)
        self.assertEqual(leg["selected_mode"], "walking")
        self.assertEqual(leg["selection_source"], "short_walk")
        self.assertIn("短途步行", leg["note"])

    def test_endpoint_topology_for_zero_one_and_two_explicit_places(self):
        no_endpoints, _ = plan_fixture()
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"])
                          for leg in no_endpoints["days"][0]["legs"]], [("a0", "a1")])

        arrival_only, _ = plan_fixture(arrival=True)
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"])
                          for leg in arrival_only["days"][0]["legs"]],
                         [("arrival", "a0"), ("a0", "a1")])

        both, _ = plan_fixture(arrival=True, departure=True)
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"])
                          for leg in both["days"][0]["legs"]],
                         [("arrival", "a0"), ("a0", "a1"), ("a1", "departure")])

    def test_multiday_lodging_edges_and_weather_are_day_specific(self):
        forecasts = {"2026-10-01": {"date": "2026-10-01", "day_weather": "晴"}}
        result, _ = plan_fixture(day_count=3, arrival=True, departure=True, forecasts=forecasts)
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"]) for leg in result["days"][0]["legs"]],
                         [("arrival", "a0"), ("a0", "lodging")])
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"]) for leg in result["days"][1]["legs"]],
                         [("lodging", "a2"), ("a2", "lodging")])
        self.assertEqual([(leg["from_activity_id"], leg["to_activity_id"]) for leg in result["days"][2]["legs"]],
                         [("lodging", "a4"), ("a4", "departure")])
        self.assertEqual(result["days"][0]["weather"]["day_weather"], "晴")
        self.assertIsNone(result["days"][1]["weather"])
        uncovered = {(issue["code"], issue["date"]) for issue in result["issues"]}
        self.assertIn(("WEATHER_UNCOVERED", "2026-10-02"), uncovered)
        self.assertIn(("WEATHER_UNCOVERED", "2026-10-03"), uncovered)


class EditedPlanningIntegrationTest(unittest.TestCase):
    def _persist_and_edit(self, result, request, operation, *, poi_resolver=None, query_tool=fake_route):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = TaskRepository(Path(directory.name) / "tasks.sqlite3")
        repository.initialize()
        repository.create("task", request.model_dump(mode="json"))
        repository.start("task")
        repository.succeed("task", result)
        editor = DayEditService(repository, signing_key=b"test-signing-key-32-bytes-long!!",
                                query_tool=query_tool, poi_resolver=poi_resolver)
        result = editor.attach_tokens("task", result)
        body = RecalculateDayRequest.model_validate({
            "task_id": "task", "date": result["days"][0]["date"],
            "edit_token": result["days"][0]["edit_token"], "client_revision": 1,
            "request_id": "request-1", "operations": [operation],
        })
        return editor.recalculate(body)

    def test_reordering_preserves_an_unchanged_directed_route(self):
        result, request = plan_fixture()
        day = result["days"][0]
        day["activities"].insert(1, {"activity_id": "break", "type": "free_time", "period": "afternoon",
                                     "title": "自由活动", "description": "", "duration_minutes": 30,
                                     "place": None, "requirement_ids": [], "reference_cost": None,
                                     "start_at": None, "end_at": None})
        original_leg = day["legs"][0]
        calls = []
        response = self._persist_and_edit(result, request, {
            "type": "move_activity", "activity_id": "break", "direction": "down",
        }, query_tool=lambda name, args: (calls.append((name, args)), fake_route(name, args))[1])
        edited = response["day"]
        self.assertEqual([a["activity_id"] for a in edited["activities"]], ["a0", "a1", "break"])
        self.assertEqual(len(edited["legs"]), 1)
        self.assertEqual((edited["legs"][0]["from_activity_id"], edited["legs"][0]["to_activity_id"]),
                         ("a0", "a1"))
        self.assertEqual(edited["legs"][0]["options"], original_leg["options"])
        self.assertEqual(calls, [])

    def test_planner_normalizes_provider_photo_objects(self):
        self.assertEqual(RouteTripPlanner._photos({"photos": [{"url": "https://example.test/a.jpg", "title": "a"},
                                                               {"title": "missing"}, "https://example.test/b.jpg"]}),
                         ["https://example.test/a.jpg", "https://example.test/b.jpg"])


    def test_manual_slow_mode_can_show_possible_overrun_without_changing_activities(self):
        result, request = plan_fixture(durations=(250, 250))
        original_ids = [activity["activity_id"] for activity in result["days"][0]["activities"]]
        self.assertEqual(result["days"][0]["time_summary"]["status"], "within_budget")
        edited = self._persist_and_edit(result, request, {
            "type": "select_leg_mode", "from_activity_id": "a0", "to_activity_id": "a1", "mode": "walking",
        })["day"]
        self.assertEqual(edited["legs"][0]["selected_mode"], "walking")
        self.assertEqual(edited["legs"][0]["selection_source"], "manual")
        self.assertEqual(edited["time_summary"]["status"], "possible_overrun")
        self.assertEqual([activity["activity_id"] for activity in edited["activities"]], original_ids)
        self.assertTrue(any(issue["code"] == "TIME_EXCEEDED" for issue in edited["issues"]))

    def test_addition_recalculates_routes_and_flags_opening_conflict(self):
        result, request = plan_fixture()
        new_poi = {"id": "new", "name": "新地点", "address": "北京", "location": {"longitude": 116.32,
                    "latitude": 39.9}, "opening_hours": "12:00-13:00", "reference_cost": 0, "cost_basis": "reference"}
        response = self._persist_and_edit(result, request, {
            "type": "add_poi", "poi_id": "new", "client_activity_id": "client_new_1",
        }, poi_resolver=lambda identity, city: new_poi if identity == "new" and city == "北京" else None)
        day = response["day"]
        self.assertIn("client_new_1", [activity["activity_id"] for activity in day["activities"]])
        self.assertTrue(day["legs"])
        for leg in day["legs"]:
            self.assertEqual(leg["selected_mode"], leg["fastest_mode"])
        self.assertTrue(any(issue["code"] == "OPENING_CONFLICT" and issue["activity_id"] == "client_new_1"
                            for issue in day["issues"]))


class AmapInitializationIntegrationTest(unittest.TestCase):
    def test_uvx_local_fallback_and_empty_tool_set_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_uvx = Path(temporary) / ".local" / "bin" / "uvx"
            local_uvx.parent.mkdir(parents=True)
            local_uvx.write_text("fixture")
            local_uvx.chmod(0o755)

            class Tool:
                def __init__(self, **kwargs):
                    self.server_command = kwargs["server_command"]
                    self.env = kwargs["env"]
                    self._available_tools = [{"name": "maps_text_search"}]

            with patch.dict(amap_service.os.environ, {"UV_CACHE_DIR": "/private/tmp/amap-cache-test"}), \
                 patch.object(amap_service, "_amap_mcp_tool", None), \
                 patch.object(amap_service, "get_settings", return_value=SimpleNamespace(
                     amap_api_key="fake-key", planner_tool_timeout_seconds=45)), \
                 patch.object(amap_service.shutil, "which", return_value=None), \
                 patch.object(amap_service.Path, "home", return_value=Path(temporary)), \
                 patch.object(amap_service, "MCPTool", Tool):
                tool = amap_service.get_amap_mcp_tool()
                self.assertEqual(tool.server_command[0], str(local_uvx))
                self.assertEqual(tool.env["AMAP_HTTP_TIMEOUT_SECONDS"], "45")
                self.assertEqual(tool.env["UV_CACHE_DIR"], "/private/tmp/amap-cache-test")

            class EmptyTool(Tool):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self._available_tools = []

            with patch.object(amap_service, "_amap_mcp_tool", None), \
                 patch.object(amap_service, "get_settings", return_value=SimpleNamespace(
                     amap_api_key="fake-key", planner_tool_timeout_seconds=45)), \
                 patch.object(amap_service.shutil, "which", return_value=str(local_uvx)), \
                 patch.object(amap_service, "MCPTool", EmptyTool):
                with self.assertRaisesRegex(RuntimeError, "未发现可用地图工具"):
                    amap_service.get_amap_mcp_tool()
