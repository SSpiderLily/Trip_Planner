"""高德地图MCP适配：统一解析地点、天气与路线结果，不记录原始响应。"""
from __future__ import annotations

import re
import shutil
import threading
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents.tools import MCPTool

from ..agents.execution import check_tool_result, decode_result, find_items
from ..config import get_settings
from ..models.schemas import Location, POIInfo, WeatherInfo


_amap_mcp_tool = None
_amap_service = None
_amap_lock = threading.RLock()


def get_amap_mcp_tool() -> MCPTool:
    global _amap_mcp_tool
    with _amap_lock:
        if _amap_mcp_tool is None:
            settings = get_settings()
            if not settings.amap_api_key:
                raise ValueError("高德地图API Key未配置")
            uvx = shutil.which("uvx")
            local_uvx = Path.home() / ".local" / "bin" / "uvx"
            if not uvx and local_uvx.is_file() and os.access(local_uvx, os.X_OK):
                uvx = str(local_uvx)
            if not uvx:
                raise RuntimeError("未找到 uvx，请安装 uv 并将 uvx 加入 PATH 或放在 ~/.local/bin/uvx")
            try:
                tool = MCPTool(name="amap", description="高德地图服务", server_command=[uvx, "amap-mcp-server"],
                               env={"AMAP_MAPS_API_KEY": settings.amap_api_key}, auto_expand=True)
            except Exception:
                # MCPTool currently hides discovery exceptions; never expose exception text that may contain secrets.
                raise RuntimeError("地图工具初始化失败，请检查 uvx、MCP缓存和网络配置") from None
            if not tool._available_tools:
                raise RuntimeError("地图工具初始化失败：未发现可用地图工具，请检查 uvx、MCP缓存和网络配置")
            _amap_mcp_tool = tool
            print(f"✅ 高德地图MCP工具初始化成功，工具数量: {len(_amap_mcp_tool._available_tools)}")
        return _amap_mcp_tool


class AmapService:
    def __init__(self, mcp_tool=None):
        self.mcp_tool = mcp_tool if mcp_tool is not None else get_amap_mcp_tool()

    def _call(self, tool_name: str, arguments: dict) -> Any:
        value = self.mcp_tool.run({"action": "call_tool", "tool_name": tool_name, "arguments": arguments})
        data = decode_result(value)
        check_tool_result(data)
        return data

    @staticmethod
    def _location(value):
        if isinstance(value, dict):
            try:
                return float(value.get("longitude", value.get("lng"))), float(value.get("latitude", value.get("lat")))
            except (ValueError, TypeError):
                return None
        if isinstance(value, str) and "," in value:
            try:
                lng, lat = value.split(",", 1)
                return float(lng), float(lat)
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _items(data, *keys):
        for key in keys:
            values = find_items(data, key)
            if values is not None:
                return values
        return []

    @classmethod
    def _poi(cls, data: dict) -> POIInfo | None:
        coordinates = cls._location(data.get("location"))
        if not coordinates or not data.get("id") or not data.get("name"):
            return None
        biz = data.get("biz_ext") if isinstance(data.get("biz_ext"), dict) else {}
        cost = data.get("cost") if data.get("cost") is not None else biz.get("cost")
        numeric_cost = None
        if cost is not None:
            match = re.fullmatch(r"\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:元|人民币)?\s*(?:/人)?\s*", str(cost))
            if match:
                parsed = float(match.group(1))
                if parsed >= 0 and parsed != float("inf"):
                    numeric_cost = parsed
        photos = data.get("photos") or []
        if isinstance(photos, list):
            normalized_photos = []
            for photo in photos:
                url = photo.get("url") if isinstance(photo, dict) else photo
                if isinstance(url, str) and url.strip():
                    normalized_photos.append(url.strip())
            photos = normalized_photos
        else:
            photos = []
        return POIInfo(
            id=str(data["id"]), name=str(data["name"]), type=str(data.get("type") or data.get("typecode") or ""),
            address=str(data.get("address") or ""), location=Location(longitude=coordinates[0], latitude=coordinates[1]),
            tel=data.get("tel"), photos=photos,
            opening_hours=data.get("opentime") or data.get("opentime_today"),
            reference_cost=numeric_cost, cost_basis="reference" if numeric_cost is not None else None)

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        data = self._call("maps_search_detail", {"id": poi_id})
        if isinstance(data, dict):
            for key in ("poi", "detail"):
                if isinstance(data.get(key), dict):
                    return data[key]
            rows = self._items(data, "pois")
            if rows and isinstance(rows[0], dict):
                return rows[0]
            nested = data.get("data")
            if isinstance(nested, dict):
                return nested
            return data
        return {}

    def get_poi_info(self, poi_id: str) -> Dict[str, Any] | None:
        detail = self.get_poi_detail(poi_id)
        poi = self._poi(detail)
        return poi.model_dump() if poi else None

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        result = self._call("maps_text_search", {"keywords": keywords, "city": city, "citylimit": str(citylimit).lower()})
        output = []
        for item in self._items(result, "pois")[:20]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            detail = item
            if not self._location(detail.get("location")):
                try:
                    detail = {**item, **self.get_poi_detail(str(item["id"]))}
                except Exception:
                    continue
            poi = self._poi(detail)
            if poi:
                output.append(poi)
        return output

    def get_weather(self, city: str) -> List[WeatherInfo]:
        data = self._call("maps_weather", {"city": city})
        result = []
        for item in self._items(data, "casts", "forecasts"):
            if not isinstance(item, dict) or not item.get("date"):
                continue
            try:
                result.append(WeatherInfo(
                    date=str(item["date"]), day_weather=str(item.get("dayweather") or item.get("day_weather") or ""),
                    night_weather=str(item.get("nightweather") or item.get("night_weather") or ""),
                    day_temp=item.get("daytemp", item.get("day_temp")), night_temp=item.get("nighttemp", item.get("night_temp")),
                    wind_direction=str(item.get("daywind") or item.get("winddirection") or ""),
                    wind_power=str(item.get("daypower") or item.get("windpower") or "")))
            except (ValueError, TypeError):
                continue
        return result

    def plan_route(self, origin_address: str, destination_address: str, origin_city: Optional[str] = None,
                   destination_city: Optional[str] = None, route_type: str = "walking") -> Dict[str, Any]:
        tools = {"walking": "maps_direction_walking_by_address", "driving": "maps_direction_driving_by_address",
                 "transit": "maps_direction_transit_integrated_by_address"}
        if route_type not in tools:
            return {}
        arguments = {"origin_address": origin_address, "destination_address": destination_address}
        if origin_city:
            arguments["origin_city"] = origin_city
        if destination_city:
            arguments["destination_city"] = destination_city
        data = self._call(tools[route_type], arguments)
        routes = self._items(data, "transits" if route_type == "transit" else "paths")
        valid = []
        for route in routes:
            try:
                duration = float(route.get("duration"))
                if duration >= 0:
                    valid.append((duration, route))
            except (ValueError, TypeError, AttributeError):
                continue
        if not valid:
            return {}
        duration, route = min(valid, key=lambda pair: pair[0])
        try:
            distance = float(route.get("distance"))
        except (ValueError, TypeError):
            distance = None
        return {"distance": distance, "duration": duration, "route_type": route_type, "description": "路线耗时仅供参考"}

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        arguments = {"address": address}
        if city:
            arguments["city"] = city
        data = self._call("maps_geo", arguments)
        rows = self._items(data, "geocodes")
        coordinates = self._location(rows[0].get("location")) if rows and isinstance(rows[0], dict) else None
        return Location(longitude=coordinates[0], latitude=coordinates[1]) if coordinates else None


def get_amap_service() -> AmapService:
    global _amap_service
    with _amap_lock:
        if _amap_service is None:
            _amap_service = AmapService()
        return _amap_service
