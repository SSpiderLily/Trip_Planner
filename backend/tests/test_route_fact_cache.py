"""路线事实缓存的单行程隔离、TTL和异步请求合并测试。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from app.models.planning_v4.facts import (
    FactSource,
    MapCoordinate,
    PlaceFact,
    RouteFact,
    RouteOption,
    RouteSegment,
)
from app.services.route_fact_cache import (
    LoadedRouteFact,
    RouteFactCache,
    RouteFactCacheError,
    RouteFactMismatchError,
    RouteQueryKey,
)
from app.services.amap_contract_adapter import AmapContractAdapter


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
with patch.dict(os.environ, {"AMAP_MAPS_API_KEY": "offline-fixture-key"}):
    vendor = importlib.import_module("vendor.amap_mcp_server_0_1_11")


ORIGIN = MapCoordinate(lng=121.492127, lat=31.233516)
DESTINATION = MapCoordinate(lng=121.475213, lat=31.228827)
DEPARTURE = datetime.fromisoformat("2026-10-11T09:10:00+08:00")
ADAPTER_VERSION = "planning-v4-route-adapter-1"
_TOOL_NAMES = {
    "walking": ("maps_direction_walking_by_coordinates", "v3"),
    "bicycling": ("maps_bicycling_by_coordinates", "v4"),
    "transit": ("maps_direction_transit_integrated_by_coordinates", "v3"),
}


class FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.value = initial

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class VendorResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


def make_key(
    *,
    plan_id: str = "shanghai-plan",
    mode: str = "walking",
    reverse: bool = False,
    origin: MapCoordinate = ORIGIN,
    destination: MapCoordinate = DESTINATION,
    departure: datetime | None = None,
    strategy: str | int | None = None,
    city: str | None = None,
    destination_city: str | None = None,
    extra_params: dict[str, object] | None = None,
) -> RouteQueryKey:
    tool_name, api_version = _TOOL_NAMES[mode]
    if reverse:
        origin, destination = destination, origin
        origin_id, destination_id = "bund", "hotel"
    else:
        origin_id, destination_id = "hotel", "bund"
    params: dict[str, object] = {
        "origin": f"{origin.lng},{origin.lat}",
        "destination": f"{destination.lng},{destination.lat}",
    }
    if city is not None:
        params["city"] = city
    if destination_city is not None:
        params["cityd"] = destination_city
    if departure is not None:
        params.update(date=departure.date().isoformat(), time=departure.strftime("%H:%M"))
    if strategy is not None:
        params["strategy"] = strategy
    if mode == "transit":
        params["extensions"] = "base"
    if extra_params:
        params.update(extra_params)
    return RouteQueryKey.from_effective_params(
        plan_id=plan_id,
        provider="amap",
        api_version=api_version,
        adapter_version=ADAPTER_VERSION,
        tool_name=tool_name,
        coordinate_system="GCJ-02",
        origin_poi_id=origin_id,
        origin=origin,
        destination_poi_id=destination_id,
        destination=destination,
        city=city,
        destination_city=destination_city,
        mode=mode,  # type: ignore[arg-type]
        effective_params=params,
        requested_departure_at=departure,
    )


def make_fact(
    key: RouteQueryKey,
    *,
    status: str = "available",
    origin_poi_id: str | None = None,
    destination_poi_id: str | None = None,
    origin: MapCoordinate | None = None,
    destination: MapCoordinate | None = None,
    mode: str | None = None,
    api_version: str | None = None,
    tool_name: str | None = None,
    effective_params: dict[str, object] | None = None,
    include_departure: bool = True,
) -> RouteFact:
    params = key.params_dict() if effective_params is None else effective_params
    expected_api = api_version or key.api_version
    option = RouteOption(
        option_id="fixed-shanghai-option",
        duration_s=780,
        distance_m=1900,
        segments=(RouteSegment(sequence=0, mode="walking", provider_attributes={"segment_note": "original"}),),
        geometry_status="missing",
        provider_attributes={"option_note": "original"},
    )
    source = FactSource(
        tool_call_id="fixed-shanghai-call",
        provider="amap",
        tool_name=tool_name or key.tool_name,
        api_version=expected_api,
        queried_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc),
        effective_params=params,
    )
    requested_departure_at = key.requested_departure_at if include_departure else None
    requested_strategy = None
    if "strategy" in params:
        requested_strategy = int(params["strategy"])
    fact_mode = mode or key.mode
    return RouteFact(
        fact_id="fixed-shanghai-route-fact",
        origin_poi_id=origin_poi_id or key.origin_poi_id,
        destination_poi_id=destination_poi_id or key.destination_poi_id,
        origin=origin or MapCoordinate(lng=key.origin[0], lat=key.origin[1]),
        destination=destination or MapCoordinate(lng=key.destination[0], lat=key.destination[1]),
        mode=fact_mode,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        requested_departure_at=requested_departure_at,
        requested_strategy=requested_strategy,
        time_verification=("sent_not_echoed" if requested_departure_at is not None else "not_requested"),
        options=(option,) if status == "available" else (),
        source=source,
        error_category="timeout" if status == "error" else None,
    )


def loaded(key: RouteQueryKey, **fact_args: object) -> LoadedRouteFact:
    return LoadedRouteFact(adapter_version=key.adapter_version, fact=make_fact(key, **fact_args))  # type: ignore[arg-type]


class RouteFactCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_mode_ttls_and_empty_ttl_expire_at_boundary(self) -> None:
        cases = (("walking", None, 3600), ("bicycling", None, 3600), ("transit", DEPARTURE, 900))
        for mode, departure, ttl in cases:
            with self.subTest(mode=mode):
                clock = FakeClock()
                calls = 0

                async def loader(key: RouteQueryKey) -> LoadedRouteFact:
                    nonlocal calls
                    calls += 1
                    return loaded(key)

                cache = RouteFactCache("shanghai-plan", loader, clock=clock)
                key = make_key(mode=mode, departure=departure, strategy="0" if mode == "transit" else None,
                               city="上海市" if mode == "transit" else None,
                               destination_city="上海市" if mode == "transit" else None)
                await cache.get_or_load(key)
                clock.advance(ttl - 1)
                await cache.get_or_load(key)
                self.assertEqual(calls, 1)
                clock.advance(1)
                await cache.get_or_load(key)
                self.assertEqual(calls, 2)
                await cache.aclose()

        clock = FakeClock()
        calls = 0

        async def empty_loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            return loaded(key, status="empty")

        cache = RouteFactCache("shanghai-plan", empty_loader, clock=clock)
        key = make_key()
        self.assertEqual((await cache.get_or_load(key)).status, "empty")
        clock.advance(59)
        await cache.get_or_load(key)
        self.assertEqual(calls, 1)
        clock.advance(1)
        await cache.get_or_load(key)
        self.assertEqual(calls, 2)
        await cache.aclose()

    async def test_ttl_starts_when_loader_finishes(self) -> None:
        clock = FakeClock()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader, clock=clock)
        key = make_key()
        pending = asyncio.create_task(cache.get_or_load(key))
        await started.wait()
        clock.advance(25)
        release.set()
        await pending
        clock.advance(3599)
        await cache.get_or_load(key)
        self.assertEqual(calls, 1)
        clock.advance(1)
        await cache.get_or_load(key)
        self.assertEqual(calls, 2)
        await cache.aclose()

    async def test_key_separates_direction_coordinates_time_city_strategy_and_plan(self) -> None:
        clock = FakeClock()
        seen: list[RouteQueryKey] = []

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            seen.append(key)
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader, clock=clock)
        base = make_key()
        changed_coordinate = make_key(origin=MapCoordinate(lng=121.492128, lat=31.233516))
        reverse = make_key(reverse=True)
        await cache.get_or_load(base)
        await cache.get_or_load(changed_coordinate)
        await cache.get_or_load(reverse)
        self.assertEqual(len(seen), 3)

        transit_base = make_key(mode="transit", departure=DEPARTURE, strategy="0",
                                city="上海市", destination_city="上海市")
        transit_same_minute = make_key(
            mode="transit",
            departure=DEPARTURE.replace(second=53),
            strategy="0",
            city="上海市",
            destination_city="上海市",
        )
        transit_later = make_key(mode="transit", departure=datetime.fromisoformat("2026-10-11T09:11:00+08:00"),
                                 strategy="0", city="上海市", destination_city="上海市")
        transit_strategy = make_key(mode="transit", departure=DEPARTURE, strategy="1",
                                    city="上海市", destination_city="上海市")
        transit_city = make_key(mode="transit", departure=DEPARTURE, strategy="0",
                                city="上海市", destination_city="上海市浦东新区")
        self.assertEqual(transit_base, transit_same_minute)
        for key in (transit_base, transit_same_minute, transit_later, transit_strategy, transit_city):
            await cache.get_or_load(key)
        self.assertEqual(len(seen), 7)

        other_plan_key = make_key(plan_id="another-plan")
        other_cache = RouteFactCache("another-plan", loader, clock=clock)
        await other_cache.get_or_load(other_plan_key)
        self.assertEqual(len(seen), 8)
        with self.assertRaises(RouteFactCacheError):
            await cache.get_or_load(other_plan_key)
        await cache.aclose()
        await other_cache.aclose()

    async def test_typed_params_distinguish_bool_from_int(self) -> None:
        calls = 0

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        integer_key = make_key(extra_params={"rank": 1})
        boolean_key = make_key(extra_params={"rank": True})
        self.assertNotEqual(integer_key, boolean_key)
        await cache.get_or_load(integer_key)
        await cache.get_or_load(boolean_key)
        self.assertEqual(calls, 2)
        await cache.aclose()

    async def test_inflight_join_and_cancel_one_waiter_without_canceling_loader(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        loader_cancelled = False
        calls = 0

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal loader_cancelled, calls
            calls += 1
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                loader_cancelled = True
                raise
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        key = make_key()
        first = asyncio.create_task(cache.get_or_load(key))
        await started.wait()
        second = asyncio.create_task(cache.get_or_load(key))
        await asyncio.sleep(0)
        self.assertEqual(cache.stats.inflight_joins, 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(loader_cancelled)
        release.set()
        result = await second
        self.assertEqual(result.status, "available")
        self.assertEqual(calls, 1)
        self.assertEqual(cache.stats.external_loads, 1)
        await cache.aclose()

    async def test_error_result_is_shared_only_while_inflight_and_not_cached(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return loaded(key, status="error")
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        key = make_key()
        first = asyncio.create_task(cache.get_or_load(key))
        await started.wait()
        second = asyncio.create_task(cache.get_or_load(key))
        await asyncio.sleep(0)
        release.set()
        first_fact, second_fact = await asyncio.gather(first, second)
        self.assertEqual(first_fact.status, "error")
        self.assertEqual(second_fact.status, "error")
        self.assertEqual(cache.stats.inflight_joins, 1)
        self.assertEqual(cache.stats.errors, 1)
        self.assertEqual((await cache.get_or_load(key)).status, "available")
        self.assertEqual(calls, 2)
        self.assertEqual(cache.stats.external_loads, 2)
        self.assertEqual(cache.stats.hits, 0)
        await cache.aclose()

    async def test_loader_exception_cleans_inflight_and_all_cancelled_waiters_do_not_leak(self) -> None:
        loop = asyncio.get_running_loop()
        prior_handler = loop.get_exception_handler()
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        calls = 0

        async def loader(_key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            started.set()
            try:
                await release.wait()
                raise RuntimeError("synthetic loader failure")
            finally:
                finished.set()

        cache = RouteFactCache("shanghai-plan", loader)
        try:
            key = make_key()
            waiter = asyncio.create_task(cache.get_or_load(key))
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await finished.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
            self.assertEqual(cache.stats.errors, 1)
            self.assertEqual(len(cache._inflight), 0)
            with self.assertRaisesRegex(RuntimeError, "synthetic loader failure"):
                await cache.get_or_load(key)
            self.assertEqual(calls, 2)
            self.assertEqual(cache.stats.external_loads, 2)
            self.assertEqual(cache.stats.errors, 2)
        finally:
            await cache.aclose()
            loop.set_exception_handler(prior_handler)

    async def test_rejects_mismatched_fact_identity_params_and_adapter_version(self) -> None:
        key = make_key()
        invalid_results = (
            (loaded(key, origin_poi_id="wrong-origin"), ADAPTER_VERSION),
            (loaded(key, destination_poi_id="wrong-destination"), ADAPTER_VERSION),
            (loaded(key, origin=MapCoordinate(lng=121.492128, lat=31.233516)), ADAPTER_VERSION),
            (loaded(key, api_version="v4"), ADAPTER_VERSION),
            (loaded(key, tool_name="maps_direction_driving_by_coordinates"), ADAPTER_VERSION),
            (loaded(key, effective_params={**key.params_dict(), "rank": 2}), ADAPTER_VERSION),
            (loaded(key), "different-adapter"),
        )
        for bad, version in invalid_results:
            with self.subTest(version=version, fact=bad.fact.fact_id):
                async def loader(_key: RouteQueryKey, result: LoadedRouteFact = bad, adapter: str = version) -> LoadedRouteFact:
                    return LoadedRouteFact(adapter_version=adapter, fact=result.fact)

                cache = RouteFactCache("shanghai-plan", loader)
                with self.assertRaises(RouteFactMismatchError):
                    await cache.get_or_load(key)
                self.assertEqual(cache.stats.errors, 1)
                self.assertEqual(len(cache._inflight), 0)
                await cache.aclose()

    async def test_missing_transit_request_metadata_cannot_fill_timed_key(self) -> None:
        key = make_key(mode="transit", departure=DEPARTURE, strategy="0",
                       city="上海市", destination_city="上海市")

        async def loader(query_key: RouteQueryKey) -> LoadedRouteFact:
            return loaded(query_key, include_departure=False)

        cache = RouteFactCache("shanghai-plan", loader)
        with self.assertRaises(RouteFactMismatchError):
            await cache.get_or_load(key)
        self.assertEqual(cache.stats.errors, 1)
        await cache.aclose()

    async def test_cached_fact_nested_dicts_are_isolated_from_callers(self) -> None:
        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        key = make_key()
        first = await cache.get_or_load(key)
        first.options[0].provider_attributes["option_note"] = "caller mutation"
        first.options[0].segments[0].provider_attributes["segment_note"] = "caller mutation"
        first.source.effective_params["origin"] = "0,0"
        second = await cache.get_or_load(key)
        self.assertEqual(second.options[0].provider_attributes["option_note"], "original")
        self.assertEqual(second.options[0].segments[0].provider_attributes["segment_note"], "original")
        self.assertEqual(second.source.effective_params["origin"], f"{ORIGIN.lng},{ORIGIN.lat}")
        self.assertEqual(cache.stats.hits, 1)
        await cache.aclose()

    async def test_close_cancels_plan_loader_and_rejects_later_use(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def loader(_key: RouteQueryKey) -> LoadedRouteFact:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        cache = RouteFactCache("shanghai-plan", loader)
        pending = asyncio.create_task(cache.get_or_load(make_key()))
        await started.wait()
        await cache.aclose()
        self.assertTrue(cancelled.is_set())
        with self.assertRaises(asyncio.CancelledError):
            await pending
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            await cache.get_or_load(make_key())

    async def test_close_does_not_cache_loader_that_suppresses_cancellation(self) -> None:
        started = asyncio.Event()

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        pending = asyncio.create_task(cache.get_or_load(make_key()))
        await started.wait()
        await cache.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(len(cache._entries), 0)
        self.assertEqual(len(cache._inflight), 0)

    async def test_rejects_use_from_a_different_event_loop(self) -> None:
        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            return loaded(key)

        cache = RouteFactCache("shanghai-plan", loader)
        key = make_key()
        await cache.get_or_load(key)
        caught: list[BaseException] = []

        def use_other_loop() -> None:
            try:
                asyncio.run(cache.get_or_load(key))
            except BaseException as exc:
                caught.append(exc)

        thread = threading.Thread(target=use_other_loop)
        thread.start()
        await asyncio.to_thread(thread.join)
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], RuntimeError)
        self.assertIn("event loop", str(caught[0]))
        await cache.aclose()

    async def test_rejects_credentials_nested_params_and_non_finite_values(self) -> None:
        for params in (
            {"amap_api_key": "redacted"},
            {"access-token": "redacted"},
            {"metadata": {"nested": "value"}},
            {"rank": float("nan")},
            {"rank": float("inf")},
        ):
            with self.subTest(params=tuple(params)):
                with self.assertRaises(RouteFactCacheError):
                    make_key(extra_params=params)
        with self.assertRaises(RouteFactCacheError):
            replace(make_key(), destination=(181.0, DESTINATION.lat))

    async def test_vendor_http_params_flow_through_adapter_into_cache_for_all_modes(self) -> None:
        origin_place = PlaceFact(
            fact_id="fixture-hotel-fact",
            poi_id="fixture-hotel",
            name="上海酒店",
            location=ORIGIN,
            source_refs=("fixture-source",),
        )
        destination_place = PlaceFact(
            fact_id="fixture-bund-fact",
            poi_id="fixture-bund",
            name="外滩",
            location=DESTINATION,
            source_refs=("fixture-source",),
        )
        actual_http_params: list[dict[str, object]] = []
        response_by_url = {
            "https://restapi.amap.com/v3/direction/walking": {
                "status": "1",
                "route": {"paths": [{"duration": "600", "distance": "1000", "steps": []}]},
            },
            "https://restapi.amap.com/v4/direction/bicycling": {
                "errcode": 0,
                "data": {"paths": [{"duration": "700", "distance": "1200", "steps": []}]},
            },
            "https://restapi.amap.com/v3/direction/transit/integrated": {
                "status": "1",
                "route": {"transits": [{"duration": "800", "distance": "1300", "segments": []}]},
            },
        }

        def http_get(url: str, **kwargs: object) -> VendorResponse:
            params = dict(kwargs["params"])  # type: ignore[arg-type]
            actual_http_params.append(params)
            return VendorResponse(response_by_url[url])

        def tool_runner(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
            if tool_name == "maps_direction_walking_by_coordinates":
                return vendor.maps_direction_walking_by_coordinates(**arguments)
            if tool_name == "maps_bicycling_by_coordinates":
                return vendor.maps_bicycling_by_coordinates(**arguments)
            if tool_name == "maps_direction_transit_integrated_by_coordinates":
                return vendor.maps_direction_transit_integrated_by_coordinates(**arguments)
            raise AssertionError(f"unexpected tool {tool_name}")

        adapter = AmapContractAdapter(tool_runner)

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            result = adapter.plan_route(
                origin_place,
                destination_place,
                key.mode,
                city="上海",
                destination_city="上海",
                departure_at=DEPARTURE if key.mode == "transit" else None,
                strategy=0 if key.mode == "transit" else None,
            )
            self.assertEqual(result.status, "ok")
            return LoadedRouteFact(adapter_version=key.adapter_version, fact=result.data)

        expected_params_by_mode = {
            "walking": {"origin": f"{ORIGIN.lng},{ORIGIN.lat}", "destination": f"{DESTINATION.lng},{DESTINATION.lat}"},
            "bicycling": {"origin": f"{ORIGIN.lng},{ORIGIN.lat}", "destination": f"{DESTINATION.lng},{DESTINATION.lat}"},
            "transit": {
                "origin": f"{ORIGIN.lng},{ORIGIN.lat}",
                "destination": f"{DESTINATION.lng},{DESTINATION.lat}",
                "city": "上海",
                "cityd": "上海",
                "extensions": "base",
                "date": "2026-10-11",
                "time": "09:10",
                "strategy": "0",
            },
        }
        cache = RouteFactCache("shanghai-plan", loader)
        with patch.object(vendor, "_http_get", side_effect=http_get):
            for mode in ("walking", "bicycling", "transit"):
                tool_name, api_version = _TOOL_NAMES[mode]
                params = expected_params_by_mode[mode]
                key = RouteQueryKey.from_effective_params(
                    plan_id="shanghai-plan",
                    provider="amap",
                    api_version=api_version,
                    adapter_version=ADAPTER_VERSION,
                    tool_name=tool_name,
                    coordinate_system="GCJ-02",
                    origin_poi_id=origin_place.poi_id,
                    origin=ORIGIN,
                    destination_poi_id=destination_place.poi_id,
                    destination=DESTINATION,
                    city="上海" if mode == "transit" else None,
                    destination_city="上海" if mode == "transit" else None,
                    mode=mode,  # type: ignore[arg-type]
                    effective_params=params,
                    requested_departure_at=DEPARTURE if mode == "transit" else None,
                )
                fact = await cache.get_or_load(key)
                self.assertEqual(fact.status, "available")
                self.assertEqual(fact.source.effective_params, params)
                await cache.get_or_load(key)

        self.assertEqual(len(actual_http_params), 3)
        self.assertEqual(cache.stats.external_loads, 3)
        self.assertEqual(cache.stats.hits, 3)
        for mode, actual in zip(("walking", "bicycling", "transit"), actual_http_params):
            source_params = {name: value for name, value in actual.items() if name != "key"}
            self.assertEqual(source_params, expected_params_by_mode[mode])
        self.assertEqual(actual_http_params[2]["extensions"], "base")
        await cache.aclose()

    async def test_cache_stats_report_load_hit_join_and_error(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def loader(key: RouteQueryKey) -> LoadedRouteFact:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return loaded(key)
            return loaded(key, status="error")

        cache = RouteFactCache("shanghai-plan", loader)
        key = make_key()
        first = asyncio.create_task(cache.get_or_load(key))
        await started.wait()
        second = asyncio.create_task(cache.get_or_load(key))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        await cache.get_or_load(key)
        error_key = make_key(destination=MapCoordinate(lng=121.475214, lat=31.228827))
        await cache.get_or_load(error_key)
        self.assertEqual(cache.stats.external_loads, 2)
        self.assertEqual(cache.stats.hits, 1)
        self.assertEqual(cache.stats.inflight_joins, 1)
        self.assertEqual(cache.stats.errors, 1)
        await cache.aclose()


if __name__ == "__main__":
    unittest.main()
