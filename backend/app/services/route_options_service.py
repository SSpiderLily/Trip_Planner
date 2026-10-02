"""景点间路线方式查询与推荐规则。只计算参考路线，不生成导航指令。"""
from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from math import isfinite
from typing import Any, Callable

from ..agents.execution import check_tool_result, decode_result, find_items


MODES = ("walking", "bicycling", "transit", "driving")
TOOL_NAMES = {
    "walking": "maps_direction_walking_by_coordinates",
    "bicycling": "maps_bicycling_by_coordinates",
    "transit": "maps_direction_transit_integrated_by_coordinates",
    "driving": "maps_direction_driving_by_coordinates",
}


def _duration(option: dict[str, Any]) -> float | None:
    try:
        value = float(option.get("duration"))
        return value if isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def _has_coordinates(place: dict | None) -> bool:
    if not isinstance(place, dict) or place.get("longitude") is None or place.get("latitude") is None:
        return False
    try:
        return all(isfinite(float(place[key])) for key in ("longitude", "latitude"))
    except (TypeError, ValueError, KeyError):
        return False


def choose_mode(options: dict[str, dict[str, Any]], preferred: str = "transit", *, preserve: str | None = None) -> str | None:
    """选择可用方式；同耗时先匹配偏好，再按固定顺序。"""
    # Preserve an explicit choice even when its latest query failed; caller can display unknown duration.
    if preserve in options:
        return preserve
    available = [mode for mode in MODES if mode in options and options[mode].get("status") == "available"
                 and options[mode].get("duration_minutes") is not None]
    if not available:
        return None
    fastest = min(options[mode]["duration_minutes"] for mode in available)
    tied = [mode for mode in available if options[mode]["duration_minutes"] == fastest]
    if preferred in tied:
        return preferred
    return next((mode for mode in MODES if mode in tied), None)


class RouteOptionsService:
    """以注入的MCP调用器查询路线，便于单测替换且不打印外部响应。"""

    def __init__(self, query_tool: Callable[[str, dict[str, Any]], Any], *, query_limit: int = 80):
        self.query_tool = query_tool
        self.query_limit = query_limit
        self.query_count = 0
        self.cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def options(self, origin: dict | None, destination: dict | None, city: str, *, include_driving: bool = False) -> dict[str, dict[str, Any]]:
        modes = MODES if include_driving else MODES[:3]
        if not _has_coordinates(origin) or not _has_coordinates(destination):
            return {mode: self._unknown(mode, "no_route") for mode in modes}
        if origin.get("source_id") and origin.get("source_id") == destination.get("source_id"):
            result = {mode: self._unknown(mode, "available", duration=0, distance=0, source="same_place") for mode in modes}
            return result

        options = {}
        for mode in modes:
            key = (str(origin.get("source_id") or self._coord(origin)), str(destination.get("source_id") or self._coord(destination)), mode)
            if key not in self.cache:
                if self.query_count >= self.query_limit:
                    self.cache[key] = self._unknown(mode, "failed", source="query_limit")
                else:
                    self.query_count += 1
                    self.cache[key] = self._query(mode, origin, destination, city)
            options[mode] = dict(self.cache[key])
        return options

    @staticmethod
    def _coord(place: dict) -> str:
        return f"{place.get('longitude')},{place.get('latitude')}"

    @staticmethod
    def _unknown(mode: str, status: str, *, duration=None, distance=None, source=None) -> dict:
        return {"mode": mode, "duration_minutes": duration, "distance_m": distance,
                "status": status, "source": source or ("amap" if status == "failed" else "amap"),
                "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def _query(self, mode: str, origin: dict, destination: dict, city: str) -> dict:
        args = ({"origin_coordinates": self._coord(origin), "destination_coordinates": self._coord(destination)}
                if mode == "bicycling" else {"origin": self._coord(origin), "destination": self._coord(destination)})
        if mode == "transit":
            args.update(city=city, cityd=city)
        try:
            value = self.query_tool(TOOL_NAMES[mode], args)
            data = decode_result(value)
            check_tool_result(data)
            routes = find_items(data, "transits" if mode == "transit" else "paths") or []
            if not routes and mode == "transit":
                routes = find_items(data, "plans") or []
            normalized = []
            for route in routes:
                seconds = _duration(route) if isinstance(route, dict) else None
                if seconds is None:
                    continue
                try:
                    distance = float(route.get("distance")) if route.get("distance") not in (None, "") else None
                    if distance is not None and (not isfinite(distance) or distance < 0):
                        distance = None
                except (TypeError, ValueError):
                    distance = None
                normalized.append((ceil(seconds / 60), distance))
            if not normalized:
                return self._unknown(mode, "no_route")
            duration, distance = min(normalized, key=lambda item: item[0])
            return self._unknown(mode, "available", duration=duration, distance=distance, source="amap")
        except Exception:
            # Externally supplied error text may include request arguments or credentials.
            return self._unknown(mode, "failed")
