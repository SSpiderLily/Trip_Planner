"""将 AMap MCP 原始结果转换为 planning_v4 的来源记录与地图事实。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.agents.execution import PlanningError, decode_result
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.facts import (
    FactSource,
    MapCoordinate,
    MissingField,
    PlaceFact,
    PlaceSearchData,
    ReferenceCost,
    RouteFact,
    RouteOption,
    RouteSegment,
    TransitLine,
    ToolError,
    ToolResult,
    TransitStop,
    WeatherFact,
    WeatherForecast,
)
from app.services.amap_service import get_amap_mcp_tool


T = TypeVar("T", bound=ContractModel)
ToolRunner = Callable[[str, dict[str, Any]], Any]
_ERROR_CATEGORIES = {"invalid_request", "auth", "rate_limit", "timeout", "network", "provider_error", "invalid_response"}
_RAW_COST = re.compile(r"^\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:元|人民币)?\s*(?:/人|每人)?\s*$")


@dataclass(frozen=True)
class _Call:
    tool_call_id: str
    source: FactSource
    data: Mapping[str, Any] | None
    status: str
    error: ToolError | None


def _walk_find(value: Any, key: str) -> list[Any] | None:
    if isinstance(value, Mapping):
        item = value.get(key)
        if isinstance(item, list):
            return item
        for child in value.values():
            found = _walk_find(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _walk_find(child, key)
            if found is not None:
                return found
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and parsed != float("inf") else None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _provider_text(value: Any) -> str | None:
    """Normalize optional provider text fields, including array-shaped empty values."""
    if isinstance(value, (list, tuple)):
        parts = [text for item in value if (text := _provider_text(item)) is not None]
        return ", ".join(parts) if parts else None
    return _text(value)


def _temperature(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _decimal_cost(value: Any) -> ReferenceCost:
    if value is None:
        return ReferenceCost()
    raw = str(value)
    match = _RAW_COST.fullmatch(raw)
    if not match:
        return ReferenceCost(raw=raw)
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return ReferenceCost(raw=raw)
    return ReferenceCost(raw=raw, value=amount)


def _coordinate(value: Any) -> MapCoordinate | None:
    if isinstance(value, Mapping):
        longitude, latitude = value.get("longitude", value.get("lng")), value.get("latitude", value.get("lat"))
    elif isinstance(value, str) and "," in value:
        longitude, latitude = value.split(",", 1)
    else:
        return None
    try:
        return MapCoordinate(lng=float(longitude), lat=float(latitude))
    except (TypeError, ValueError, ValidationError):
        return None


def _effective_params(raw: Mapping[str, Any]) -> dict[str, str | int | float | bool | None]:
    source = raw.get("result_source")
    params = source.get("effective_params") if isinstance(source, Mapping) else None
    if not isinstance(params, Mapping):
        return {}
    safe: dict[str, str | int | float | bool | None] = {}
    for key, value in params.items():
        normalized = "".join(character for character in str(key).lower() if character.isalnum())
        if normalized in {"key", "apikey", "amapapikey", "token", "authorization", "secret"}:
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[str(key)] = value
    return safe


def _error_category(raw: Mapping[str, Any], fallback: str = "provider_error") -> str:
    meta = raw.get("result_error")
    value = meta.get("category") if isinstance(meta, Mapping) else fallback
    return str(value) if value in _ERROR_CATEGORIES else fallback


def _map_error_category(exc: Exception) -> str:
    if isinstance(exc, PlanningError) and exc.code == "TOOL_RESPONSE_INVALID":
        return "invalid_response"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "network"
    if isinstance(exc, (ValueError, ValidationError)):
        return "invalid_response"
    return "provider_error"


def _tool_error(category: str, provider_code: Any = None) -> ToolError:
    code = str(provider_code)[:64] if provider_code is not None else None
    return ToolError(
        category=category if category in _ERROR_CATEGORIES else "provider_error",
        provider_code=code,
        message="地图查询失败",
        retryable=category in {"rate_limit", "timeout", "network"},
    )


class AmapContractAdapter:
    """保留 MCP 原工具，输出有来源和局部完整度信息的 v4 类型。"""

    def __init__(self, tool_runner: ToolRunner | None = None):
        self._tool_runner = tool_runner

    def _run(self, tool_name: str, arguments: dict[str, Any]) -> _Call:
        call_id = str(uuid4())
        trusted_api_version = "v4" if "bicycling" in tool_name and "coordinates" in tool_name else "v3"
        api_version = trusted_api_version
        try:
            if self._tool_runner is None:
                value = get_amap_mcp_tool().run({
                    "action": "call_tool", "tool_name": tool_name, "arguments": arguments,
                })
            else:
                value = self._tool_runner(tool_name, arguments)
            decoded = decode_result(value)
            if not isinstance(decoded, Mapping):
                raise ValueError("地图工具返回结构无效")
            raw = dict(decoded)
            meta = raw.get("result_source")
            if isinstance(meta, Mapping):
                reported_version = str(meta.get("api_version", api_version))
                if reported_version not in {"v3", "v4"}:
                    raise ValueError("地图工具来源版本无效")
                api_version = reported_version
            queried_at_value = meta.get("queried_at") if isinstance(meta, Mapping) else None
            try:
                queried_at = datetime.fromisoformat(str(queried_at_value).replace("Z", "+00:00")) if queried_at_value else datetime.now(timezone.utc)
                if queried_at.tzinfo is None:
                    queried_at = queried_at.replace(tzinfo=timezone.utc)
            except ValueError:
                queried_at = datetime.now(timezone.utc)
            source = FactSource(
                tool_call_id=call_id,
                provider="amap",
                tool_name=tool_name,
                api_version=api_version,
                queried_at=queried_at,
                # Tool arguments are not proof of HTTP parameters sent by the server.
                # Only vendor result metadata can establish effective_params.
                effective_params=_effective_params(raw),
            )
            status_value = raw.get("result_status")
            status = status_value if status_value in {"ok", "empty", "error"} else (
                "error" if raw.get("error") else "ok"
            )
            meta_error = raw.get("result_error")
            error = _tool_error(
                _error_category(raw),
                meta_error.get("provider_code") if isinstance(meta_error, Mapping) else None,
            ) if status == "error" else None
            return _Call(call_id, source, raw, status, error)
        except Exception as exc:
            source = FactSource(
                tool_call_id=call_id,
                provider="amap",
                tool_name=tool_name,
                api_version=trusted_api_version,
                queried_at=datetime.now(timezone.utc),
                effective_params={},
            )
            return _Call(call_id, source, None, "error", _tool_error(_map_error_category(exc)))

    @staticmethod
    def _place(item: Mapping[str, Any], call: _Call) -> PlaceFact | None:
        poi_id, name = _text(item.get("id", item.get("poi_id"))), _text(item.get("name"))
        if poi_id is None or not name:
            return None
        location = _coordinate(item.get("location"))
        biz = item.get("biz_ext") if isinstance(item.get("biz_ext"), Mapping) else {}
        cost_value = item.get("cost", biz.get("cost"))
        photos = item.get("photos") or []
        if not isinstance(photos, list):
            photos = []
        photo_urls = tuple(
            url for photo in photos
            if isinstance((url := photo.get("url") if isinstance(photo, Mapping) else photo), str) and url
        )
        missing = []
        if location is None:
            missing.append(MissingField(field="location", reason="not_returned"))
        if cost_value is None:
            missing.append(MissingField(field="cost", reason="not_returned"))
        hours = item.get("opentime") or item.get("opentime_today")
        if hours is None:
            missing.append(MissingField(field="opening_hours_raw", reason="not_returned"))
        return PlaceFact(
            fact_id=f"place:{call.tool_call_id}:{poi_id}",
            poi_id=poi_id,
            name=name,
            location=location,
            address=_provider_text(item.get("address")),
            city=_provider_text(item.get("cityname", item.get("city"))),
            adcode=_provider_text(item.get("adcode")),
            type_name=_provider_text(item.get("type", item.get("type_name"))),
            typecode=_provider_text(item.get("typecode")),
            business_area=_provider_text(item.get("business_area")),
            cost=_decimal_cost(cost_value),
            opening_hours_raw=_provider_text(hours),
            photos=photo_urls,
            source_refs=(call.tool_call_id,),
            field_sources={"poi_id": call.tool_call_id, "name": call.tool_call_id},
            missing_fields=tuple(missing),
        )

    def search_text(
        self, keywords: str, city: str, *, citylimit: bool = True, types: str | None = None,
        page: int = 1, offset: int = 20, extensions: str = "base",
    ) -> ToolResult[PlaceSearchData]:
        arguments: dict[str, Any] = {
            "keywords": keywords, "city": city, "citylimit": str(citylimit).lower(),
            "page": str(page), "offset": str(offset), "extensions": extensions,
        }
        if types:
            arguments["types"] = types
        call = self._run("maps_text_search", arguments)
        raw = call.data or {}
        found_rows = _walk_find(raw, "pois")
        rows = found_rows or []
        places = tuple(place for item in rows if isinstance(item, Mapping)
                       if (place := self._place(item, call)) is not None)
        count_value = raw.get("count")
        try:
            count = int(count_value) if count_value is not None else None
        except (ValueError, TypeError):
            count = None
        data = PlaceSearchData(places=places, page=page, offset=offset, provider_count=count)
        missing = tuple(dict.fromkeys(field for place in places for field in place.missing_fields))
        invalid = call.status != "error" and found_rows is None
        invalid = invalid or (bool(rows) and not places)
        status = "error" if call.status == "error" or invalid else "ok" if places else "empty"
        error = call.error or (_tool_error("invalid_response") if invalid else None)
        return ToolResult(tool_call_id=call.tool_call_id, status=status, data=data,
                          missing_fields=missing, source=call.source, error=error if status == "error" else None)

    def search_around(
        self, location: MapCoordinate, *, radius_m: int = 1000, keywords: str = "",
        types: str | None = None, page: int = 1, offset: int = 20,
        extensions: str = "base", sortrule: str = "distance",
    ) -> ToolResult[PlaceSearchData]:
        arguments: dict[str, Any] = {
            "location": f"{location.lng},{location.lat}", "radius": str(radius_m), "keywords": keywords,
            "page": str(page), "offset": str(offset), "extensions": extensions, "sortrule": sortrule,
        }
        if types:
            arguments["types"] = types
        call = self._run("maps_around_search", arguments)
        raw = call.data or {}
        found_rows = _walk_find(raw, "pois")
        rows = found_rows or []
        places = tuple(place for item in rows if isinstance(item, Mapping)
                       if (place := self._place(item, call)) is not None)
        try:
            count = int(raw["count"]) if raw.get("count") is not None else None
        except (ValueError, TypeError):
            count = None
        data = PlaceSearchData(places=places, page=page, offset=offset, provider_count=count)
        missing = tuple(dict.fromkeys(field for place in places for field in place.missing_fields))
        invalid = call.status != "error" and found_rows is None
        invalid = invalid or (bool(rows) and not places)
        status = "error" if call.status == "error" or invalid else "ok" if places else "empty"
        error = call.error or (_tool_error("invalid_response") if invalid else None)
        return ToolResult(tool_call_id=call.tool_call_id, status=status, data=data,
                          missing_fields=missing, source=call.source, error=error if status == "error" else None)

    def search_detail(self, poi_id: str, *, extensions: str = "all") -> ToolResult[PlaceFact]:
        call = self._run("maps_search_detail", {"id": poi_id, "extensions": extensions})
        if call.status == "error":
            return ToolResult(tool_call_id=call.tool_call_id, status="error", source=call.source, error=call.error)
        raw = call.data or {}
        rows = _walk_find(raw, "pois")
        item = rows[0] if rows else raw
        place = self._place(item, call) if isinstance(item, Mapping) else None
        if place is None:
            if call.status == "empty":
                return ToolResult(tool_call_id=call.tool_call_id, status="empty", source=call.source)
            return ToolResult(tool_call_id=call.tool_call_id, status="error", source=call.source,
                              error=_tool_error("invalid_response"))
        return ToolResult(tool_call_id=call.tool_call_id, status="ok", data=place,
                          missing_fields=place.missing_fields, source=call.source)

    def get_weather(
        self, city_or_adcode: str, expected_dates: tuple[date, ...], *, timezone_name: str = "Asia/Shanghai",
    ) -> ToolResult[WeatherFact]:
        call = self._run("maps_weather", {"city": city_or_adcode})
        raw = call.data or {}
        expected = set(expected_dates)
        if call.status == "error":
            missing_dates = tuple(sorted(expected))
            fact = WeatherFact(
                fact_id=f"weather:{call.tool_call_id}", city=city_or_adcode,
                source=call.source, missing_dates=missing_dates,
            )
            return ToolResult(tool_call_id=call.tool_call_id, status="error", data=fact,
                              source=call.source, error=call.error)
        rows = _walk_find(raw, "casts")
        has_forecast_container = rows is not None or "forecasts" in raw
        if rows is None:
            rows = _walk_find(raw, "forecasts")
        if rows is None:
            rows = []
        forecasts: list[WeatherForecast] = []
        covered: set[date] = set()
        missing_fields: list[MissingField] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                forecast_date = date.fromisoformat(str(row.get("date")))
            except ValueError:
                missing_fields.append(MissingField(field="forecast_date", reason="unparseable"))
                continue
            covered.add(forecast_date)
            forecasts.append(WeatherForecast(
                forecast_date=forecast_date,
                day_weather=_provider_text(row.get("dayweather", row.get("day_weather"))),
                night_weather=_provider_text(row.get("nightweather", row.get("night_weather"))),
                day_temp_c=_temperature(row.get("daytemp", row.get("day_temp"))),
                night_temp_c=_temperature(row.get("nighttemp", row.get("night_temp"))),
                day_wind=_provider_text(row.get("daywind", row.get("day_wind"))),
                night_wind=_provider_text(row.get("nightwind", row.get("night_wind"))),
                day_power=_provider_text(row.get("daypower", row.get("day_power"))),
                night_power=_provider_text(row.get("nightpower", row.get("night_power"))),
            ))
        missing_dates = tuple(sorted(expected - covered))
        if missing_dates:
            missing_fields.append(MissingField(field="forecast_dates", reason="out_of_coverage"))
        reporttime = raw.get("reporttime")
        report_at = None
        if reporttime:
            try:
                report_at = datetime.fromisoformat(str(reporttime).replace("Z", "+00:00"))
                if report_at.tzinfo is None:
                    report_at = report_at.replace(tzinfo=ZoneInfo(timezone_name))
            except (ValueError, TypeError):
                missing_fields.append(MissingField(field="report_time", reason="unparseable"))
        fact = WeatherFact(
            fact_id=f"weather:{call.tool_call_id}",
            city=_text(raw.get("city")) or city_or_adcode,
            adcode=str(raw["adcode"]) if raw.get("adcode") is not None else None,
            report_time=report_at,
            forecasts=tuple(forecasts),
            source=call.source,
            missing_dates=missing_dates,
        )
        invalid = call.status != "error" and not has_forecast_container
        status = "error" if call.status == "error" or invalid else "ok" if forecasts else "empty"
        error = call.error or (_tool_error("invalid_response") if invalid else None)
        return ToolResult(tool_call_id=call.tool_call_id, status=status, data=fact,
                          missing_fields=tuple(missing_fields), source=call.source,
                          error=error if status == "error" else None)

    def plan_route(
        self,
        origin: PlaceFact,
        destination: PlaceFact,
        mode: str,
        *,
        city: str,
        destination_city: str | None = None,
        departure_at: datetime | None = None,
        strategy: int | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> ToolResult[RouteFact]:
        if mode not in {"transit", "walking", "bicycling"}:
            raise ValueError("mode 必须是 transit、walking 或 bicycling")
        if origin.location is None or destination.location is None:
            raise ValueError("路线端点必须有已核实坐标")
        if origin.poi_id == destination.poi_id:
            raise ValueError("相同 POI 不需要请求外部路线")
        origin_text = f"{origin.location.lng},{origin.location.lat}"
        destination_text = f"{destination.location.lng},{destination.location.lat}"
        requested_departure = departure_at
        if requested_departure is not None and requested_departure.tzinfo is None:
            raise ValueError("departure_at 必须带时区")
        if mode == "walking":
            tool_name = "maps_direction_walking_by_coordinates"
            arguments: dict[str, Any] = {"origin": origin_text, "destination": destination_text}
            version = "v3"
        elif mode == "bicycling":
            tool_name = "maps_bicycling_by_coordinates"
            arguments = {"origin_coordinates": origin_text, "destination_coordinates": destination_text}
            version = "v4"
        else:
            tool_name = "maps_direction_transit_integrated_by_coordinates"
            arguments = {"origin": origin_text, "destination": destination_text,
                         "city": city, "cityd": destination_city or city}
            if departure_at is not None:
                local = departure_at.astimezone(ZoneInfo(timezone_name))
                arguments.update({"date": local.date().isoformat(), "time": local.strftime("%H:%M")})
            if strategy is not None:
                arguments["strategy"] = str(strategy)
            version = "v3"
        call = self._run(tool_name, arguments)
        raw = call.data or {}
        effective = call.source.effective_params
        sent_time = (
            requested_departure is not None
            and effective.get("date") == requested_departure.astimezone(ZoneInfo(timezone_name)).date().isoformat()
            and effective.get("time") == requested_departure.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
        )
        time_verification = "not_requested" if requested_departure is None else "sent_not_echoed" if sent_time else "not_sent"
        if mode == "bicycling":
            route_present = isinstance(raw.get("data"), Mapping)
            route_data = raw.get("data") if route_present else {}
            route_present = route_present and isinstance(route_data.get("paths"), list)
            route_rows = route_data.get("paths") if isinstance(route_data.get("paths"), list) else []
        else:
            route_present = isinstance(raw.get("route"), Mapping)
            route_data = raw.get("route") if route_present else {}
            route_key = "transits" if mode == "transit" else "paths"
            route_present = route_present and isinstance(route_data.get(route_key), list)
            route_rows = route_data.get(route_key) if isinstance(route_data.get(route_key), list) else []
        options_list: list[RouteOption] = []
        invalid_option_count = 0
        for index, item in enumerate(route_rows):
            if not isinstance(item, Mapping):
                invalid_option_count += 1
                continue
            try:
                option = self._route_option(item, index, call.tool_call_id, mode)
            except (ValidationError, TypeError, ValueError):
                invalid_option_count += 1
                continue
            if option is not None:
                options_list.append(option)
        options = tuple(options_list)
        invalid = call.status != "error" and (
            not route_present or (bool(route_rows) and not options)
        )
        status = "error" if call.status == "error" or invalid else "available" if options else "empty"
        call_error = call.error or (_tool_error("invalid_response") if invalid else None)
        missing_fields: list[MissingField] = []
        if status == "available" and any(option.geometry_status != "complete" for option in options):
            missing_fields.append(MissingField(field="route_geometry", reason="not_returned"))
        if status == "available" and invalid_option_count:
            missing_fields.append(MissingField(field="route_options", reason="unparseable"))
        requested_strategy = strategy if mode == "transit" else None
        fact = RouteFact(
            fact_id=f"route:{call.tool_call_id}",
            origin_poi_id=origin.poi_id,
            destination_poi_id=destination.poi_id,
            origin=origin.location,
            destination=destination.location,
            mode=mode,
            status=status,
            requested_departure_at=requested_departure if mode == "transit" else None,
            requested_strategy=requested_strategy,
            time_verification=time_verification if mode == "transit" else "not_requested",
            options=options if status == "available" else (),
            source=call.source,
            missing_fields=tuple(missing_fields),
            error_category=call_error.category if call_error else None,
        )
        result_status = "error" if status == "error" else "ok" if status == "available" else "empty"
        return ToolResult(tool_call_id=call.tool_call_id, status=result_status, data=fact,
                          missing_fields=tuple(missing_fields), source=call.source,
                          error=call_error if status == "error" else None)

    @classmethod
    def _route_option(cls, item: Mapping[str, Any], index: int, call_id: str, mode: str) -> RouteOption | None:
        parts: list[dict[str, Any]] = []
        if mode == "transit":
            outer_segments = item.get("segments") if isinstance(item.get("segments"), list) else []
            for outer in outer_segments:
                if not isinstance(outer, Mapping):
                    parts.append({"mode": "walking", "polyline": (), "provider_attributes": {}})
                    continue
                walking = outer.get("walking")
                if isinstance(walking, Mapping):
                    steps = walking.get("steps") if isinstance(walking.get("steps"), list) else []
                    if steps:
                        for step in steps:
                            if not isinstance(step, Mapping):
                                parts.append({"mode": "walking", "polyline": (), "provider_attributes": {}})
                                continue
                            parts.append({
                                "mode": "walking", "distance": step.get("distance"), "duration": step.get("duration"),
                                "polyline": cls._parse_polyline(step.get("polyline")),
                                "instructions": tuple(text for key in ("instruction", "action", "assistant_action")
                                                      if (text := _provider_text(step.get(key))) is not None),
                                "line_name": _provider_text(step.get("road")),
                                "provider_attributes": cls._scalar_attributes(step, {"distance", "duration", "instruction", "action", "assistant_action", "road", "polyline"}),
                            })
                    elif walking.get("distance") is not None or walking.get("duration") is not None:
                        parts.append({
                            "mode": "walking", "distance": walking.get("distance"), "duration": walking.get("duration"),
                            "polyline": cls._parse_polyline(walking.get("polyline")),
                            "provider_attributes": cls._scalar_attributes(walking, {"distance", "duration", "polyline"}),
                        })
                bus = outer.get("bus") if isinstance(outer.get("bus"), Mapping) else {}
                if "bus" in outer:
                    buslines = bus.get("buslines") if isinstance(bus.get("buslines"), list) else []
                    lines = tuple(cls._transit_line(line) for line in buslines if isinstance(line, Mapping))
                    mode_value = lines[0].mode if lines else "bus"
                    parts.append({
                        "mode": mode_value,
                        "line_name": lines[0].line_name if len(lines) == 1 else None,
                        "line_type": lines[0].line_type if len(lines) == 1 else None,
                        "polyline": lines[0].polyline if len(lines) == 1 else (),
                        "transit_alternatives": lines,
                        "provider_attributes": cls._scalar_attributes(bus, set()),
                    })
                railway = outer.get("railway")
                if isinstance(railway, Mapping) and (railway.get("name") or railway.get("trip")):
                    trip = railway.get("trip") if isinstance(railway.get("trip"), Mapping) else {}
                    line = TransitLine(
                        mode="railway", line_name=_text(railway.get("name")), line_type="railway",
                        distance_m=_number(trip.get("distance")), duration_s=_number(trip.get("time")),
                        polyline=cls._parse_polyline(railway.get("polyline")),
                        instructions=tuple(str(value) for value in trip.values() if isinstance(value, str)),
                    )
                    parts.append({
                        "mode": "railway", "line_name": line.line_name, "line_type": "railway",
                        "polyline": line.polyline, "transit_alternatives": (line,),
                        "entrance": cls._stop(outer.get("entrance")), "exit": cls._stop(outer.get("exit")),
                        "provider_attributes": cls._scalar_attributes(outer, {"walking", "bus", "railway", "entrance", "exit"}),
                    })
                elif parts and ("entrance" in outer or "exit" in outer):
                    parts[-1]["entrance"] = cls._stop(outer.get("entrance"))
                    parts[-1]["exit"] = cls._stop(outer.get("exit"))
                    parts[-1]["provider_attributes"].update(cls._scalar_attributes(outer, {"walking", "bus", "railway", "entrance", "exit"}))
        else:
            steps = item.get("steps") if isinstance(item.get("steps"), list) else []
            if steps:
                for step in steps:
                    if not isinstance(step, Mapping):
                        parts.append({"mode": mode, "polyline": (), "provider_attributes": {}})
                        continue
                    parts.append({
                        "mode": mode, "distance": step.get("distance"), "duration": step.get("duration"),
                        "polyline": cls._parse_polyline(step.get("polyline")),
                        "instructions": tuple(text for key in ("instruction", "action", "assistant_action")
                                              if (text := _provider_text(step.get(key))) is not None),
                        "line_name": _provider_text(step.get("road")),
                        "provider_attributes": cls._scalar_attributes(step, {"distance", "duration", "polyline", "instruction", "action", "assistant_action", "road"}),
                    })
            elif item.get("polyline"):
                parts.append({"mode": mode, "polyline": cls._parse_polyline(item.get("polyline")), "provider_attributes": {}})
        segments = tuple(
            RouteSegment(
                sequence=sequence,
                mode=part["mode"],
                distance_m=_number(part.get("distance")),
                duration_s=_number(part.get("duration")),
                polyline=tuple(part.get("polyline", ())),
                line_name=_provider_text(part.get("line_name")),
                line_type=_provider_text(part.get("line_type")),
                departure_stop=part.get("departure_stop"),
                arrival_stop=part.get("arrival_stop"),
                via_stops=tuple(part.get("via_stops", ())),
                instructions=tuple(part.get("instructions", ())),
                transit_alternatives=tuple(part.get("transit_alternatives", ())),
                entrance=part.get("entrance"),
                exit=part.get("exit"),
                provider_attributes=part.get("provider_attributes", {}),
            ) for sequence, part in enumerate(parts)
        )
        has_geometry = any(
            bool(segment.polyline) or any(line.polyline for line in segment.transit_alternatives)
            for segment in segments
        )
        complete_segments = [
            bool(segment.polyline) or (
                bool(segment.transit_alternatives)
                and all(line.polyline for line in segment.transit_alternatives)
            )
            for segment in segments
        ]
        has_missing = any(not complete for complete in complete_segments)
        geometry_status = "missing" if not has_geometry else "partial" if has_missing else "complete"
        return RouteOption(
            option_id=f"{call_id}:option:{index}",
            duration_s=_number(item.get("duration")),
            distance_m=_number(item.get("distance")),
            segments=segments,
            geometry_status=geometry_status,
            provider_attributes=cls._scalar_attributes(item, {"duration", "distance", "segments", "steps", "polyline"}),
        )

    @staticmethod
    def _scalar_attributes(item: Mapping[str, Any], excluded: set[str]) -> dict[str, str | int | float | bool | None]:
        return {
            str(key): value for key, value in item.items()
            if key not in excluded and (value is None or isinstance(value, (str, int, float, bool)))
        }

    @classmethod
    def _transit_line(cls, line: Mapping[str, Any]) -> TransitLine:
        label = f"{_text(line.get('type')) or ''} {_text(line.get('name')) or ''}".lower()
        mode = "subway" if any(marker in label for marker in ("地铁", "subway", "metro")) else "bus"
        via_values = line.get("via_stops") if isinstance(line.get("via_stops"), list) else []
        return TransitLine(
            mode=mode,
            line_name=_provider_text(line.get("name")),
            line_type=_provider_text(line.get("type")),
            distance_m=_number(line.get("distance")),
            duration_s=_number(line.get("duration")),
            polyline=cls._parse_polyline(line.get("polyline")),
            departure_stop=cls._stop(line.get("departure_stop")),
            arrival_stop=cls._stop(line.get("arrival_stop")),
            via_stops=tuple(stop for value in via_values if isinstance(value, Mapping) if (stop := cls._stop(value)) is not None),
            start_time=_provider_text(line.get("start_time")),
            end_time=_provider_text(line.get("end_time")),
            station_start_time=_provider_text(line.get("station_start_time")),
            station_end_time=_provider_text(line.get("station_end_time")),
            via_stop_count=int(line["via_num"]) if str(line.get("via_num", "")).isdigit() else None,
        )

    @staticmethod
    def _parse_polyline(value: Any) -> tuple[MapCoordinate, ...]:
        if not isinstance(value, str):
            return ()
        pairs = value.split(";")
        points = []
        for pair in pairs:
            coordinate = _coordinate(pair)
            if coordinate is None:
                return ()
            points.append(coordinate)
        return tuple(points)

    @staticmethod
    def _stop(value: Any) -> TransitStop | None:
        if not isinstance(value, Mapping):
            return None
        location = _coordinate(value.get("location"))
        name, stop_id = _provider_text(value.get("name")), _provider_text(value.get("id"))
        if not name and not stop_id and location is None:
            return None
        return TransitStop(
            name=name,
            stop_id=stop_id,
            location=location,
        )
