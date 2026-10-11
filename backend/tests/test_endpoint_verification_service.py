"""F04d 到离端点核实的离线回归。"""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.agents.execution import PlanningError
from app.models.planning_v4.facts import FactSource, MapCoordinate, PlaceFact, PlaceSearchData, ToolResult
from app.models.planning_v4.input import RequirementSummary, ReferenceWindow, RuleSet, TripBudget, TripRequest
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.endpoint_verification_service import (
    EndpointVerificationContext,
    EndpointVerificationService,
)
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.observation_service import ObservationService
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.task_repository import TaskRepository
from scripts import verify_endpoints


WINDOWS = (
    ReferenceWindow(period="breakfast", start_minute=480, end_minute=540),
    ReferenceWindow(period="morning", start_minute=540, end_minute=720),
    ReferenceWindow(period="lunch", start_minute=720, end_minute=840),
    ReferenceWindow(period="afternoon", start_minute=840, end_minute=1020),
    ReferenceWindow(period="dinner", start_minute=1020, end_minute=1140),
    ReferenceWindow(period="evening", start_minute=1140, end_minute=1320),
)


def make_context(*, arrival=None, departure=None):
    request = TripRequest(
        request_id="endpoint-request",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        arrival_place_query=arrival,
        departure_place_query=departure,
        budget=TripBudget(amount=None, currency="CNY", scope="whole_trip", party_basis="unspecified"),
    )
    planning_input = build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=RequirementSummary(requirements_id="endpoint-requirements", request_id=request.request_id),
        rules=RuleSet(rule_version="endpoint-test-v1", windows=WINDOWS),
    ))
    return EndpointVerificationContext.from_planning_input(
        planning_input, run_id="endpoint-run-1", context_revision=3, base_revision=2,
    )


def poi(poi_id="poi-1", name="上海虹桥站", *, location="121.327,31.200", city="上海市", adcode="310000"):
    value = {"id": poi_id, "name": name}
    if location is not None:
        value["location"] = location
    if city is not None:
        value["cityname"] = city
    if adcode is not None:
        value["adcode"] = adcode
    return value


def search_payload(rows, *, status="ok", category="network"):
    result = {"result_status": status, "count": len(rows), "pois": rows}
    if status == "error":
        result["result_error"] = {"category": category, "provider_code": "FIXTURE"}
    result["result_source"] = {
        "api_version": "v3",
        "queried_at": "2026-10-10T01:00:00+00:00",
        "effective_params": {"keywords": "上海虹桥站", "city": "上海"},
    }
    return result


def detail_payload(item, *, status="ok", category="network"):
    result = dict(item)
    result["result_status"] = status
    if status == "error":
        result["result_error"] = {"category": category, "provider_code": "FIXTURE"}
    result["result_source"] = {
        "api_version": "v3",
        "queried_at": "2026-10-10T01:01:00+00:00",
        "effective_params": {"id": str(item.get("id", "poi-1"))},
    }
    return result


class EndpointVerificationServiceTests(unittest.TestCase):
    def service_for(self, search, details=None):
        details = details or {}
        calls = []

        def runner(tool_name, arguments):
            calls.append((tool_name, dict(arguments)))
            if tool_name == "maps_text_search":
                return search(arguments["keywords"])
            if tool_name == "maps_search_detail":
                return details[arguments["id"]]
            raise AssertionError(f"unexpected tool: {tool_name}")

        return EndpointVerificationService(
            adapter=AmapContractAdapter(runner), tool_timeout_seconds=2,
        ), calls

    def test_empty_original_request_returns_missing_slots_without_tool_calls(self):
        calls = []
        service = EndpointVerificationService(
            adapter=AmapContractAdapter(lambda *args: calls.append(args)),
        )

        result = service.run(make_context())

        self.assertEqual(result.arrival.status, "missing")
        self.assertEqual(result.departure.status, "missing")
        self.assertEqual(result.fact_store.places, ())
        self.assertEqual(calls, [])
        self.assertEqual(result.request_id, "endpoint-request")
        self.assertEqual(result.run_id, "endpoint-run-1")
        self.assertEqual((result.context_revision, result.base_revision), (3, 2))

    def test_unique_exact_same_city_result_is_verified_with_source_closure(self):
        service, calls = self.service_for(lambda _query: search_payload([poi()]))

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual(result.arrival.reason, "exact_name_city_and_coordinate")
        self.assertEqual(result.arrival.candidates[0].poi_id, "poi-1")
        fact = result.fact_store.places[0]
        self.assertEqual(fact.location.lng, 121.327)
        self.assertEqual(fact.city, "上海市")
        self.assertEqual(set(fact.source_refs), {source.tool_call_id for source in result.fact_store.sources})
        self.assertEqual(set(fact.field_sources.values()), {result.fact_store.sources[0].tool_call_id})
        self.assertEqual([item[0] for item in calls], ["maps_text_search"])

    def test_punctuation_and_whitespace_normalize_but_abbreviation_stays_ambiguous(self):
        service, _ = self.service_for(lambda _query: search_payload([poi(name="上海虹桥站")]))
        exact = service.run(make_context(arrival=" 上海，虹桥站 "))
        fuzzy = service.run(make_context(arrival="虹桥站"))

        self.assertEqual(exact.arrival.status, "verified")
        self.assertEqual(exact.arrival.user_query, " 上海，虹桥站 ")
        self.assertEqual(fuzzy.arrival.status, "ambiguous")
        self.assertEqual(fuzzy.arrival.reason, "name_not_exact")
        self.assertEqual(fuzzy.fact_store.places, ())

    def test_same_name_multiple_real_poi_ids_stays_ambiguous(self):
        service, _ = self.service_for(lambda _query: search_payload([
            poi("poi-1"), poi("poi-2", location="121.330,31.202"),
        ]))

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "ambiguous")
        self.assertEqual(result.arrival.reason, "multiple_exact_pois")
        self.assertEqual({item.poi_id for item in result.arrival.candidates}, {"poi-1", "poi-2"})
        self.assertEqual(result.fact_store.places, ())

    def test_duplicate_poi_conflicting_coordinates_is_not_last_write_wins(self):
        service, _ = self.service_for(lambda _query: search_payload([
            poi("poi-1", location="121.327,31.200"), poi("poi-1", location="121.900,31.800"),
        ]))

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "unverified")
        self.assertEqual(result.arrival.reason, "duplicate_poi_conflict")
        self.assertEqual(result.fact_store.places, ())

    def test_duplicate_merge_checks_later_rows_when_first_identity_and_location_are_missing(self):
        service, _ = self.service_for(lambda _query: search_payload([
            poi("same-id", location=None, city=None, adcode=None),
            poi("same-id", location="121.327,31.200", city="上海市", adcode="310000"),
            poi("same-id", location="121.900,31.800", city="北京市", adcode="110000"),
        ]))

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "unverified")
        self.assertEqual(result.arrival.reason, "duplicate_poi_conflict")
        self.assertEqual(result.fact_store.places, ())

    def test_duplicate_merge_fills_later_location_and_clears_missing_field(self):
        service, _ = self.service_for(lambda _query: search_payload([
            poi("same-id", location=None, city=None, adcode=None),
            poi("same-id", location="121.327,31.200", city="上海市", adcode="310000"),
            poi("same-id", location="121.327,31.200", city="上海", adcode="310000"),
        ]))

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "verified")
        fact = result.arrival.candidates[0]
        self.assertIsNotNone(fact.location)
        self.assertEqual(fact.city, "上海市")
        self.assertNotIn("location", {item.field for item in fact.missing_fields})

    def test_fuzzy_candidate_duplicates_are_deduplicated_by_real_poi_id(self):
        service, _ = self.service_for(lambda _query: search_payload([
            poi("same-fuzzy-id", name="上海虹桥站北广场"),
            poi("same-fuzzy-id", name="虹桥站北广场", location="121.900,31.800"),
        ]))

        result = service.run(make_context(arrival="虹桥站"))

        self.assertEqual(result.arrival.status, "ambiguous")
        self.assertEqual(result.arrival.reason, "candidate_identity_conflict")
        self.assertEqual([item.poi_id for item in result.arrival.candidates], ["same-fuzzy-id"])

    def test_city_missing_or_mismatched_never_enters_fact_store(self):
        missing_city, _ = self.service_for(
            lambda _query: search_payload([poi(city=None)]),
            {"poi-1": detail_payload(poi(city=None))},
        )
        wrong_city, _ = self.service_for(lambda _query: search_payload([poi(city="北京市", adcode="110000")]))

        missing = missing_city.run(make_context(arrival="上海虹桥站"))
        mismatch = wrong_city.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(missing.arrival.status, "unverified")
        self.assertEqual(missing.arrival.reason, "city_unverified")
        self.assertEqual(wrong_city.run(make_context(arrival="上海虹桥站")).arrival.reason, "city_mismatch")
        self.assertEqual(mismatch.fact_store.places, ())

    def test_no_result_and_provider_error_are_distinct(self):
        not_found, _ = self.service_for(lambda _query: search_payload([], status="empty"))
        error, _ = self.service_for(lambda _query: search_payload([], status="error", category="network"))

        missing = not_found.run(make_context(arrival="上海虹桥站"))
        failed = error.run(make_context(arrival="上海虹桥站"))

        self.assertEqual((missing.arrival.status, missing.arrival.reason), ("not_found", "search_empty"))
        self.assertEqual(failed.arrival.status, "error")
        self.assertEqual(failed.arrival.reason, "search_error:network")

    def test_missing_coordinates_fetch_detail_by_real_poi_id_and_close_both_sources(self):
        service, calls = self.service_for(
            lambda _query: search_payload([poi("real-amap-poi-42", location=None)]),
            {"real-amap-poi-42": detail_payload(poi("real-amap-poi-42"))},
        )

        result = service.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual(calls[1], ("maps_search_detail", {"id": "real-amap-poi-42", "extensions": "all"}))
        fact = result.fact_store.places[0]
        self.assertEqual(len(fact.source_refs), 2)
        self.assertEqual(len(result.fact_store.sources), 2)
        self.assertEqual(fact.field_sources["location"], result.arrival.detail_results[0].source.tool_call_id)
        self.assertEqual(result.arrival.fact_id, fact.fact_id)

    def test_missing_detail_coordinates_and_detail_failure_have_clear_states(self):
        missing, _ = self.service_for(
            lambda _query: search_payload([poi(location=None)]),
            {"poi-1": detail_payload(poi(location=None))},
        )
        failed, _ = self.service_for(
            lambda _query: search_payload([poi(location=None)]),
            {"poi-1": detail_payload({}, status="error", category="timeout")},
        )

        unverified = missing.run(make_context(arrival="上海虹桥站"))
        error = failed.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(unverified.arrival.status, "unverified")
        self.assertEqual(unverified.arrival.reason, "coordinate_missing")
        self.assertEqual(error.arrival.status, "error")
        self.assertEqual(error.arrival.reason, "detail_error:timeout")

    def test_detail_must_return_the_same_identity_and_coordinates(self):
        wrong_id, _ = self.service_for(
            lambda _query: search_payload([poi("poi-1", location=None)]),
            {"poi-1": detail_payload(poi("different-poi"))},
        )
        wrong_location, _ = self.service_for(
            lambda _query: search_payload([poi("poi-1", city=None)]),
            {"poi-1": detail_payload(poi("poi-1", location="121.900,31.800"))},
        )

        identity = wrong_id.run(make_context(arrival="上海虹桥站"))
        coordinate = wrong_location.run(make_context(arrival="上海虹桥站"))

        self.assertEqual(identity.arrival.reason, "detail_identity_mismatch")
        self.assertEqual(coordinate.arrival.reason, "poi_coordinate_conflict")
        self.assertEqual(identity.fact_store.places, ())
        self.assertEqual(coordinate.fact_store.places, ())

    def test_identical_arrival_and_departure_query_reuse_one_search_and_fact(self):
        service, calls = self.service_for(lambda _query: search_payload([poi()]))

        result = service.run(make_context(arrival="上海虹桥站", departure="上海虹桥站"))

        self.assertEqual(result.arrival.status, "verified")
        self.assertEqual(result.departure.status, "verified")
        self.assertEqual(result.arrival.user_query, "上海虹桥站")
        self.assertEqual(result.departure.user_query, "上海虹桥站")
        self.assertEqual(result.arrival.fact_id, result.departure.fact_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.fact_store.places), 1)

    def test_conflicting_snapshots_of_same_poi_invalidate_both_endpoints(self):
        def search(query):
            location = "121.327,31.200" if query == "上海虹桥站" else "121.900,31.800"
            return search_payload([poi("same-id", name=query, location=location)])

        service, _ = self.service_for(search)

        result = service.run(make_context(arrival="上海虹桥站", departure="虹桥火车站"))

        self.assertEqual(result.arrival.status, "unverified")
        self.assertEqual(result.departure.status, "unverified")
        self.assertEqual(result.arrival.reason, "poi_coordinate_conflict")
        self.assertEqual(result.fact_store.places, ())

    def test_same_fact_id_with_different_endpoint_coordinates_is_not_overwritten(self):
        class Adapter:
            def search_text(self, query, _city, **_kwargs):
                source = FactSource(
                    tool_call_id=f"source:{query}",
                    tool_name="maps_text_search",
                    api_version="v3",
                    queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
                    effective_params={"keywords": query, "city": "上海"},
                )
                coordinate = (
                    MapCoordinate(lng=121.327, lat=31.2)
                    if query == "上海虹桥站"
                    else MapCoordinate(lng=121.9, lat=31.8)
                )
                place = PlaceFact(
                    fact_id="same-fact-id",
                    poi_id="same-poi-id",
                    name=query,
                    location=coordinate,
                    city="上海市",
                    adcode="310000",
                    source_refs=(source.tool_call_id,),
                    field_sources={"poi_id": source.tool_call_id, "name": source.tool_call_id},
                )
                return ToolResult[PlaceSearchData](
                    tool_call_id=source.tool_call_id,
                    status="ok",
                    data=PlaceSearchData(places=(place,)),
                    source=source,
                )

        result = EndpointVerificationService(adapter=Adapter()).run(
            make_context(arrival="上海虹桥站", departure="上海虹桥火车站"),
        )

        self.assertEqual(result.arrival.status, "unverified")
        self.assertEqual(result.departure.status, "unverified")
        self.assertEqual(result.arrival.reason, "poi_coordinate_conflict")
        self.assertEqual(result.fact_store.places, ())

    def test_conflicting_source_id_payloads_invalidate_references_without_overwriting(self):
        class Adapter:
            def search_text(self, query, _city, **_kwargs):
                source = FactSource(
                    tool_call_id="reused-source-id",
                    tool_name="maps_text_search",
                    api_version="v3",
                    queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
                    effective_params={"keywords": query, "city": "上海"},
                )
                place = PlaceFact(
                    fact_id=f"fact:{query}",
                    poi_id=f"poi:{query}",
                    name=query,
                    location=MapCoordinate(lng=121.327, lat=31.2),
                    city="上海市",
                    adcode="310000",
                    source_refs=(source.tool_call_id,),
                    field_sources={"poi_id": source.tool_call_id, "name": source.tool_call_id},
                )
                return ToolResult[PlaceSearchData](
                    tool_call_id=source.tool_call_id,
                    status="ok",
                    data=PlaceSearchData(places=(place,)),
                    source=source,
                )

        result = EndpointVerificationService(adapter=Adapter()).run(
            make_context(arrival="上海虹桥站", departure="浦东机场"),
        )

        self.assertEqual(result.arrival.status, "unverified")
        self.assertEqual(result.departure.status, "unverified")
        self.assertEqual(result.arrival.reason, "source_identity_conflict")
        self.assertEqual(result.fact_store.places, ())
        self.assertEqual(result.fact_store.sources, ())

    def test_preflight_context_mismatch_stops_before_tool_calls(self):
        context = make_context(arrival="上海虹桥站").model_copy(update={"request_id": "other-request"})
        calls = []
        service = EndpointVerificationService(adapter=AmapContractAdapter(lambda *args: calls.append(args)))

        with self.assertRaises(PlanningError) as raised:
            service.run(context)

        self.assertEqual(raised.exception.code, "ENDPOINT_CONTEXT_INVALID")
        self.assertEqual(calls, [])

    def test_async_cancellation_returns_promptly_and_does_not_start_another_call(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def actual_tool(_tool_name, _arguments):
            calls.append(1)
            started.set()
            release.wait(2)
            return search_payload([poi()])

        executor = MCPToolExecutor(actual_tool)
        service = EndpointVerificationService(tool_executor=executor, tool_timeout_seconds=3)

        async def cancel_running_query():
            task = asyncio.create_task(service.run_async(make_context(arrival="上海虹桥站")))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.3)

        try:
            asyncio.run(cancel_running_query())
        finally:
            release.set()
            executor.close(wait=True)
        self.assertEqual(calls, [1])

    def test_default_live_cli_keeps_original_empty_endpoints_and_makes_zero_calls(self):
        output = io.StringIO()
        with patch("app.services.endpoint_verification_service.get_mcp_tool_executor") as shared_executor:
            with redirect_stdout(output):
                exit_code = verify_endpoints.main(["--live"])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["scenario"], "original_shanghai_three_day_request")
        self.assertEqual(report["tool_call_count"], 0)
        self.assertEqual((report["arrival"]["status"], report["departure"]["status"]), ("missing", "missing"))
        shared_executor.assert_not_called()

    def test_cli_endpoint_query_is_marked_supplemental_and_uses_shared_executor(self):
        class FixtureExecutor:
            def __init__(self):
                self.calls = []

            def run(self, tool_name, arguments, *, timeout_seconds, cancel_event=None):
                self.calls.append((tool_name, arguments, timeout_seconds, cancel_event))
                return search_payload([], status="empty")

        executor = FixtureExecutor()
        output = io.StringIO()
        error_output = io.StringIO()
        with patch("app.services.endpoint_verification_service.get_mcp_tool_executor", return_value=executor):
            with redirect_stdout(output), redirect_stderr(error_output):
                exit_code = verify_endpoints.main([
                    "--live", "--arrival-query", "Synthetic arrival fixture",
                ])

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["scenario"], "supplemental_explicit_endpoint_queries")
        self.assertIn("不代表原始上海三日请求", report["scope_notice"])
        self.assertEqual(report["tool_call_count"], 1)
        self.assertEqual(report["arrival"]["user_query"], "Synthetic arrival fixture")
        self.assertEqual(report["arrival"]["status"], "not_found")
        self.assertEqual(executor.calls[0][0], "maps_text_search")
        self.assertEqual(executor.calls[0][1]["keywords"], "Synthetic arrival fixture")
        self.assertEqual(error_output.getvalue(), "")

    def test_real_sqlite_observation_accepts_validation_operation_type(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "observation.sqlite3")
            repository.initialize()
            repository.create("task-1", {})
            service, _ = self.service_for(lambda _query: search_payload([poi()]))

            with ObservationService(repository).task("task-1"):
                result = service.run(make_context(arrival="上海虹桥站"))

            spans = repository.spans("task-1")
            self.assertEqual(result.arrival.status, "verified")
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]["name"], "endpoint.verify")
            self.assertEqual(spans[0]["operation_type"], "validation")
            self.assertEqual(spans[0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
