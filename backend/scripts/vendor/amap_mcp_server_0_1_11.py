import os
import sys
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from mcp.server.fastmcp import FastMCP

def get_api_key() -> str:
    """Get the Amap Maps API key from environment variables"""
    api_key = os.getenv("AMAP_MAPS_API_KEY")
    if not api_key:
        raise ValueError("AMAP_MAPS_API_KEY environment variable is required")
    return api_key

AMAP_MAPS_API_KEY = get_api_key()
try:
    AMAP_HTTP_TIMEOUT_SECONDS = max(0.1, min(120.0, float(os.getenv("AMAP_HTTP_TIMEOUT_SECONDS", "25"))))
except ValueError:
    AMAP_HTTP_TIMEOUT_SECONDS = 25.0


def _http_get(url: str, **kwargs):
    """Make every upstream HTTP request bounded by the configured tool timeout."""
    kwargs.setdefault("timeout", AMAP_HTTP_TIMEOUT_SECONDS)
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.RequestException:
        raise
    except Exception as error:
        raise requests.exceptions.RequestException(_safe_error(error)) from None


def _safe_error(error: Exception) -> str:
    """Return a short category without echoing request URLs or query strings."""
    if isinstance(error, requests.exceptions.JSONDecodeError):
        return "invalid_response"
    if isinstance(error, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "network"
    if isinstance(error, requests.exceptions.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in {401, 403}:
            return "auth"
        if status == 429:
            return "rate_limit"
        if status in {408, 504}:
            return "timeout"
        if status in {400, 422}:
            return "invalid_request"
        if isinstance(status, int) and status >= 500:
            return "network"
    if isinstance(error, requests.exceptions.RequestException):
        return "network"
    return "provider_error"


def _result(data: Dict[str, Any], api_version: str, effective_params: Dict[str, Any], status: str | None = None,
            *, provider_code: str | None = None, error_category: str | None = None):
    """Additive metadata for typed adapters; preserve legacy payload fields."""
    payload = dict(data)
    if status is None:
        if payload.get("error"):
            status = "error"
        else:
            status = "ok"
    payload["result_status"] = status
    safe_params = {}
    for key, value in effective_params.items():
        normalized = "".join(character for character in str(key).lower() if character.isalnum())
        if normalized not in {"key", "apikey", "amapapikey", "token", "authorization", "secret"}:
            safe_params[str(key)] = value
    payload["result_source"] = {
        "provider": "amap",
        "api_version": api_version,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "effective_params": {key: value for key, value in safe_params.items() if value is not None},
    }
    if status == "error":
        error_text = str(payload.get("error", "")).lower()
        if "timeout" in error_text:
            category, retryable = "timeout", True
        elif "network" in error_text:
            category, retryable = "network", True
        elif "auth" in error_text or "key" in error_text:
            category, retryable = "auth", False
        elif "rate" in error_text:
            category, retryable = "rate_limit", True
        elif "invalid" in error_text:
            category, retryable = "invalid_request", False
        else:
            category, retryable = "provider_error", False
        payload["result_error"] = {
            "category": error_category or category,
            "provider_code": provider_code,
            "retryable": retryable if error_category is None else error_category in {"timeout", "network", "rate_limit"},
        }
    return payload


def _provider_failure(label: str, data: Dict[str, Any], version: str, params: Dict[str, Any]):
    code = data.get("infocode") or data.get("errcode")
    code_text = str(code) if code is not None else ""
    if code_text in {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10012", "10013", "10041"}:
        category = "auth"
    elif code_text in {"10003", "10004", "10010", "10014", "10015", "10019", "10020", "10021", "10029", "10044", "10045"}:
        category = "rate_limit"
    elif code_text in {"20000", "20001", "20002", "20011", "20012", "20800"}:
        category = "invalid_request"
    else:
        category = "provider_error"
    return _result({"error": f"{label} provider error"}, version, params, "error",
                   provider_code=code_text or None, error_category=category)

mcp = FastMCP("amap-maps")

@mcp.tool()
def maps_regeocode(location: str) -> Dict[str, Any]:
    """将一个高德经纬度坐标转换为行政区划地址信息"""
    try:
        response = _http_get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={
                "key": AMAP_MAPS_API_KEY,
                "location": location
            }
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return {"error": f"RGeocoding failed: {data.get('info') or data.get('infocode')}"}

        return {
            "province": data["regeocode"]["addressComponent"]["province"],
            "city": data["regeocode"]["addressComponent"]["city"],
            "district": data["regeocode"]["addressComponent"]["district"]
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {_safe_error(e)}"}

@mcp.tool()
def maps_geo(address: str, city: Optional[str] = None) -> Dict[str, Any]:
    """将详细的结构化地址转换为经纬度坐标。支持对地标性名胜景区、建筑物名称解析为经纬度坐标"""
    try:
        params = {
            "key": AMAP_MAPS_API_KEY,
            "address": address
        }
        if city:
            params["city"] = city

        response = _http_get(
            "https://restapi.amap.com/v3/geocode/geo",
            params=params
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return {"error": f"Geocoding failed: {data.get('info') or data.get('infocode')}"}

        geocodes = data.get("geocodes", [])
        results = []
        for geo in geocodes:
            results.append({
                "country": geo.get("country"),
                "province": geo.get("province"),
                "city": geo.get("city"),
                "citycode": geo.get("citycode"),
                "district": geo.get("district"),
                "street": geo.get("street"),
                "number": geo.get("number"),
                "adcode": geo.get("adcode"),
                "location": geo.get("location"),
                "level": geo.get("level")
            })
        return {"return": results}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {_safe_error(e)}"}

@mcp.tool()
def maps_ip_location(ip: str) -> Dict[str, Any]:
    """IP 定位根据用户输入的 IP 地址，定位 IP 的所在位置"""
    try:
        response = _http_get(
            "https://restapi.amap.com/v3/ip",
            params={
                "key": AMAP_MAPS_API_KEY,
                "ip": ip
            }
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return {"error": f"IP Location failed: {data.get('info') or data.get('infocode')}"}

        return {
            "province": data.get("province"),
            "city": data.get("city"),
            "adcode": data.get("adcode"),
            "rectangle": data.get("rectangle")
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {_safe_error(e)}"}

@mcp.tool()
def maps_weather(city: str) -> Dict[str, Any]:
    """根据城市名称或者标准adcode查询指定城市的天气"""
    try:
        params = {"key": AMAP_MAPS_API_KEY, "city": city, "extensions": "all"}
        response = _http_get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return _provider_failure("Weather", data, "v3", {"city": city, "extensions": "all"})
        if not isinstance(data.get("forecasts"), list):
            return _result({"error": "Weather response invalid"}, "v3", {"city": city, "extensions": "all"},
                           "error", error_category="invalid_response")
        forecasts = data["forecasts"]
        if not forecasts:
            return _result({"city": city, "forecasts": [], "adcode": None, "reporttime": None}, "v3",
                           {"city": city, "extensions": "all"}, "empty")

        result = {
            "city": forecasts[0]["city"],
            "adcode": forecasts[0].get("adcode"),
            "reporttime": forecasts[0].get("reporttime"),
            "forecasts": forecasts[0].get("casts", [])
        }
        return _result(result, "v3", {"city": city, "extensions": "all"},
                       "ok" if result["forecasts"] else "empty")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        return _result({"error": f"Weather request failed ({category})"}, "v3",
                       {"city": city, "extensions": "all"}, "error",
                       error_category=category)
    except Exception:
        return _result({"error": "Weather response invalid"}, "v3", {"city": city, "extensions": "all"},
                       "error", error_category="invalid_response")

@mcp.tool()
def maps_bicycling_by_address(origin_address: str, destination_address: str, origin_city: Optional[str] = None, destination_city: Optional[str] = None) -> Dict[str, Any]:
    """Plans a bicycle route between two locations using addresses. Unless you have a specific reason to use coordinates, it's recommended to use this tool.

    Args:
        origin_address (str): Starting point address (e.g. "北京市朝阳区阜通东大街6号")
        destination_address (str): Ending point address (e.g. "北京市海淀区上地十街10号")
        origin_city (Optional[str]): Optional city name for the origin address to improve geocoding accuracy
        destination_city (Optional[str]): Optional city name for the destination address to improve geocoding accuracy

    Returns:
        Dict[str, Any]: Route information including distance, duration, and turn-by-turn instructions.
        Considers bridges, one-way streets, and road closures. Supports routes up to 500km.
    """
    try:
        # Convert origin address to coordinates
        origin_result = maps_geo(origin_address, origin_city)
        if "error" in origin_result:
            return {"error": f"Failed to geocode origin address: {origin_result['error']}"}

        if not origin_result.get("return") or not origin_result["return"]:
            return {"error": "No geocoding results found for origin address"}

        origin_location = origin_result["return"][0].get("location")
        if not origin_location:
            return {"error": "Could not extract coordinates from origin geocoding result"}

        # Convert destination address to coordinates
        destination_result = maps_geo(destination_address, destination_city)
        if "error" in destination_result:
            return {"error": f"Failed to geocode destination address: {destination_result['error']}"}

        if not destination_result.get("return") or not destination_result["return"]:
            return {"error": "No geocoding results found for destination address"}

        destination_location = destination_result["return"][0].get("location")
        if not destination_location:
            return {"error": "Could not extract coordinates from destination geocoding result"}

        # Use the coordinates to plan the bicycle route
        route_result = maps_bicycling_by_coordinates(origin_location, destination_location)

        # Add address information to the result
        if "error" not in route_result:
            route_result["addresses"] = {
                "origin": {
                    "address": origin_address,
                    "coordinates": origin_location
                },
                "destination": {
                    "address": destination_address,
                    "coordinates": destination_location
                }
            }

        return route_result
    except Exception as e:
        return {"error": f"Route planning failed: {_safe_error(e)}"}

@mcp.tool()
def maps_bicycling_by_coordinates(origin_coordinates: str, destination_coordinates: str) -> Dict[str, Any]:
    """Plans a bicycle route between two coordinates.

    Args:
        origin_coordinates (str): Starting point coordinates in the format "longitude,latitude" (e.g. "116.434307,39.90909")
        destination_coordinates (str): Ending point coordinates in the format "longitude,latitude" (e.g. "116.434307,39.90909")

    Returns:
        Dict[str, Any]: Route information including distance, duration, and turn-by-turn instructions.
        Considers bridges, one-way streets, and road closures. Supports routes up to 500km.
    """
    try:
        params = {"key": AMAP_MAPS_API_KEY, "origin": origin_coordinates, "destination": destination_coordinates}
        response = _http_get(
            "https://restapi.amap.com/v4/direction/bicycling",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errcode") != 0:
            return _provider_failure("Bicycling", data, "v4", {"origin": origin_coordinates, "destination": destination_coordinates})
        route = data.get("data")
        if not isinstance(route, dict) or not isinstance(route.get("paths"), list):
            return _result({"error": "Bicycling response invalid"}, "v4",
                           {"origin": origin_coordinates, "destination": destination_coordinates},
                           "error", error_category="invalid_response")
        paths = route["paths"]
        result = {"data": {**route, "paths": paths}}
        return _result(result, "v4", {"origin": origin_coordinates, "destination": destination_coordinates},
                       "ok" if paths else "empty")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        return _result({"error": f"Bicycling request failed ({category})"}, "v4",
                       {"origin": origin_coordinates, "destination": destination_coordinates}, "error",
                       error_category=category)
    except Exception:
        return _result({"error": "Bicycling response invalid"}, "v4",
                       {"origin": origin_coordinates, "destination": destination_coordinates},
                       "error", error_category="invalid_response")

@mcp.tool()
def maps_direction_walking_by_address(origin_address: str, destination_address: str, origin_city: Optional[str] = None, destination_city: Optional[str] = None) -> Dict[str, Any]:
    """Plans a walking route between two locations using addresses. Unless you have a specific reason to use coordinates, it's recommended to use this tool.

    Args:
        origin_address (str): Starting point address (e.g. "北京市朝阳区阜通东大街6号")
        destination_address (str): Ending point address (e.g. "北京市海淀区上地十街10号")
        origin_city (Optional[str]): Optional city name for the origin address to improve geocoding accuracy
        destination_city (Optional[str]): Optional city name for the destination address to improve geocoding accuracy

    Returns:
        Dict[str, Any]: Route information including distance, duration, and turn-by-turn instructions.
        Supports routes up to 100km.
    """
    try:
        # Convert origin address to coordinates
        origin_result = maps_geo(origin_address, origin_city)
        if "error" in origin_result:
            return {"error": f"Failed to geocode origin address: {origin_result['error']}"}

        if not origin_result.get("return") or not origin_result["return"]:
            return {"error": "No geocoding results found for origin address"}

        origin_location = origin_result["return"][0].get("location")
        if not origin_location:
            return {"error": "Could not extract coordinates from origin geocoding result"}

        # Convert destination address to coordinates
        destination_result = maps_geo(destination_address, destination_city)
        if "error" in destination_result:
            return {"error": f"Failed to geocode destination address: {destination_result['error']}"}

        if not destination_result.get("return") or not destination_result["return"]:
            return {"error": "No geocoding results found for destination address"}

        destination_location = destination_result["return"][0].get("location")
        if not destination_location:
            return {"error": "Could not extract coordinates from destination geocoding result"}

        # Use the coordinates to plan the walking route
        route_result = maps_direction_walking_by_coordinates(origin_location, destination_location)

        # Add address information to the result
        if "error" not in route_result:
            route_result["addresses"] = {
                "origin": {
                    "address": origin_address,
                    "coordinates": origin_location
                },
                "destination": {
                    "address": destination_address,
                    "coordinates": destination_location
                }
            }

        return route_result
    except Exception as e:
        return {"error": f"Route planning failed: {_safe_error(e)}"}

@mcp.tool()
def maps_direction_walking_by_coordinates(origin: str, destination: str) -> Dict[str, Any]:
    """步行路径规划 API 可以根据输入起点终点经纬度坐标规划100km 以内的步行通勤方案，并且返回通勤方案的数据

    Args:
        origin (str): 起点经纬度坐标，格式为"经度,纬度" (例如："116.434307,39.90909")
        destination (str): 终点经纬度坐标，格式为"经度,纬度" (例如："116.434307,39.90909")

    Returns:
        Dict[str, Any]: 包含距离、时长和详细导航信息的路线数据
    """
    try:
        params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination}
        response = _http_get(
            "https://restapi.amap.com/v3/direction/walking",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return _provider_failure("Walking", data, "v3", {"origin": origin, "destination": destination})
        route = data.get("route")
        if not isinstance(route, dict) or not isinstance(route.get("paths"), list):
            return _result({"error": "Walking response invalid"}, "v3", {"origin": origin, "destination": destination},
                           "error", error_category="invalid_response")
        paths = route["paths"]
        result = {"route": {**route, "paths": paths}}
        return _result(result, "v3", {"origin": origin, "destination": destination},
                       "ok" if paths else "empty")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        return _result({"error": f"Walking request failed ({category})"}, "v3",
                       {"origin": origin, "destination": destination}, "error",
                       error_category=category)
    except Exception:
        return _result({"error": "Walking response invalid"}, "v3", {"origin": origin, "destination": destination},
                       "error", error_category="invalid_response")

@mcp.tool()
def maps_direction_driving_by_address(origin_address: str, destination_address: str, origin_city: Optional[str] = None, destination_city: Optional[str] = None) -> Dict[str, Any]:
    """Plans a driving route between two locations using addresses. Unless you have a specific reason to use coordinates, it's recommended to use this tool.

    Args:
        origin_address (str): Starting point address (e.g. "北京市朝阳区阜通东大街6号")
        destination_address (str): Ending point address (e.g. "北京市海淀区上地十街10号")
        origin_city (Optional[str]): Optional city name for the origin address to improve geocoding accuracy
        destination_city (Optional[str]): Optional city name for the destination address to improve geocoding accuracy

    Returns:
        Dict[str, Any]: Route information including distance, duration, and turn-by-turn instructions.
        Considers traffic conditions and road restrictions.
    """
    try:
        # Convert origin address to coordinates
        origin_result = maps_geo(origin_address, origin_city)
        if "error" in origin_result:
            return {"error": f"Failed to geocode origin address: {origin_result['error']}"}

        if not origin_result.get("return") or not origin_result["return"]:
            return {"error": "No geocoding results found for origin address"}

        origin_location = origin_result["return"][0].get("location")
        if not origin_location:
            return {"error": "Could not extract coordinates from origin geocoding result"}

        # Convert destination address to coordinates
        destination_result = maps_geo(destination_address, destination_city)
        if "error" in destination_result:
            return {"error": f"Failed to geocode destination address: {destination_result['error']}"}

        if not destination_result.get("return") or not destination_result["return"]:
            return {"error": "No geocoding results found for destination address"}

        destination_location = destination_result["return"][0].get("location")
        if not destination_location:
            return {"error": "Could not extract coordinates from destination geocoding result"}

        # Use the coordinates to plan the driving route
        route_result = maps_direction_driving_by_coordinates(origin_location, destination_location)

        # Add address information to the result
        if "error" not in route_result:
            route_result["addresses"] = {
                "origin": {
                    "address": origin_address,
                    "coordinates": origin_location
                },
                "destination": {
                    "address": destination_address,
                    "coordinates": destination_location
                }
            }

        return route_result
    except Exception as e:
        return {"error": f"Route planning failed: {_safe_error(e)}"}

@mcp.tool()
def maps_direction_driving_by_coordinates(origin: str, destination: str) -> Dict[str, Any]:
    """驾车路径规划 API 可以根据用户起终点经纬度坐标规划以小客车、轿车通勤出行的方案，并且返回通勤方案的数据

    Args:
        origin (str): 起点经纬度坐标，格式为"经度,纬度" (例如："116.434307,39.90909")
        destination (str): 终点经纬度坐标，格式为"经度,纬度" (例如："116.434307,39.90909")

    Returns:
        Dict[str, Any]: 包含距离、时长和详细导航信息的路线数据
    """
    try:
        response = _http_get(
            "https://restapi.amap.com/v3/direction/driving",
            params={
                "key": AMAP_MAPS_API_KEY,
                "origin": origin,
                "destination": destination
            }
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return {"error": f"Direction Driving failed: {data.get('info') or data.get('infocode')}"}

        paths = []
        for path in data["route"]["paths"]:
            steps = []
            for step in path["steps"]:
                steps.append({
                    "instruction": step.get("instruction"),
                    "road": step.get("road"),
                    "distance": step.get("distance"),
                    "orientation": step.get("orientation"),
                    "duration": step.get("duration")
                })
            paths.append({
                "path": path.get("path"),
                "distance": path.get("distance"),
                "duration": path.get("duration"),
                "steps": steps
            })

        return {
            "route": {
                "origin": data["route"]["origin"],
                "destination": data["route"]["destination"],
                "paths": paths
            }
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {_safe_error(e)}"}

@mcp.tool()
def maps_direction_transit_integrated_by_address(origin_address: str, destination_address: str, origin_city: str, destination_city: str) -> Dict[str, Any]:
    """Plans a public transit route between two locations using addresses. Unless you have a specific reason to use coordinates, it's recommended to use this tool.

    Args:
        origin_address (str): Starting point address (e.g. "北京市朝阳区阜通东大街6号")
        destination_address (str): Ending point address (e.g. "北京市海淀区上地十街10号")
        origin_city (str): City name for the origin address (required for cross-city transit)
        destination_city (str): City name for the destination address (required for cross-city transit)

    Returns:
        Dict[str, Any]: Route information including distance, duration, and detailed transit instructions.
        Considers various public transit options including buses, subways, and trains.
    """
    try:
        # Convert origin address to coordinates
        origin_result = maps_geo(origin_address, origin_city)
        if "error" in origin_result:
            return {"error": f"Failed to geocode origin address: {origin_result['error']}"}

        if not origin_result.get("return") or not origin_result["return"]:
            return {"error": "No geocoding results found for origin address"}

        origin_location = origin_result["return"][0].get("location")
        if not origin_location:
            return {"error": "Could not extract coordinates from origin geocoding result"}

        # Convert destination address to coordinates
        destination_result = maps_geo(destination_address, destination_city)
        if "error" in destination_result:
            return {"error": f"Failed to geocode destination address: {destination_result['error']}"}

        if not destination_result.get("return") or not destination_result["return"]:
            return {"error": "No geocoding results found for destination address"}

        destination_location = destination_result["return"][0].get("location")
        if not destination_location:
            return {"error": "Could not extract coordinates from destination geocoding result"}

        # Use the coordinates to plan the transit route
        route_result = maps_direction_transit_integrated_by_coordinates(origin_location, destination_location, origin_city, destination_city)

        # Add address information to the result
        if "error" not in route_result:
            route_result["addresses"] = {
                "origin": {
                    "address": origin_address,
                    "coordinates": origin_location
                },
                "destination": {
                    "address": destination_address,
                    "coordinates": destination_location
                }
            }

        return route_result
    except Exception as e:
        return {"error": f"Route planning failed: {_safe_error(e)}"}

@mcp.tool()
def maps_direction_transit_integrated_by_coordinates(
    origin: str, destination: str, city: str, cityd: str,
    date: Optional[str] = None, time: Optional[str] = None,
    strategy: Optional[str] = None, extensions: str = "base",
) -> Dict[str, Any]:
    """查询公共交通路线；date/time/strategy 按实际请求参数发送并写入来源元数据。"""
    params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination,
              "city": city, "cityd": cityd, "extensions": extensions}
    if date is not None:
        params["date"] = date
    if time is not None:
        params["time"] = time
    if strategy is not None:
        params["strategy"] = strategy
    safe_params = {key: value for key, value in params.items() if key != "key"}
    try:
        response = _http_get("https://restapi.amap.com/v3/direction/transit/integrated", params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "1":
            return _provider_failure("Transit", data, "v3", safe_params)
        route_data = data.get("route")
        if not isinstance(route_data, dict) or not isinstance(route_data.get("transits"), list):
            return _result({"error": "Transit response invalid"}, "v3", safe_params, "error",
                           error_category="invalid_response")
        transits = route_data["transits"]
        result = {"route": {**route_data, "transits": transits}}
        return _result(result, "v3", safe_params, "ok" if transits else "empty")
    except requests.exceptions.RequestException as error:
        category = _safe_error(error)
        error_category = "timeout" if category == "timeout" else "network" if category == "network" else "provider_error"
        return _result({"error": f"Transit request failed ({category})"}, "v3", safe_params, "error",
                       error_category=error_category)
    except Exception:
        return _result({"error": "Transit response invalid"}, "v3", safe_params, "error",
                       error_category="invalid_response")

@mcp.tool()
def maps_distance(origins: str, destination: str, type: str = "1") -> Dict[str, Any]:
    """测量两个经纬度坐标之间的距离,支持驾车、步行以及球面距离测量"""
    try:
        response = _http_get(
            "https://restapi.amap.com/v3/distance",
            params={
                "key": AMAP_MAPS_API_KEY,
                "origins": origins,
                "destination": destination,
                "type": type
            }
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return {"error": f"Direction Distance failed: {data.get('info') or data.get('infocode')}"}

        results = []
        for result in data["results"]:
            results.append({
                "origin_id": result.get("origin_id"),
                "dest_id": result.get("dest_id"),
                "distance": result.get("distance"),
                "duration": result.get("duration")
            })

        return {"results": results}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {_safe_error(e)}"}

@mcp.tool()
def maps_text_search(keywords: str, city: str = "", citylimit: str = "false", types: str = "",
                     page: str = "1", offset: str = "20", extensions: str = "base") -> Dict[str, Any]:
    """关键词搜索 API 根据用户输入的关键字进行 POI 搜索，并返回相关的信息"""
    try:
        params = {"key": AMAP_MAPS_API_KEY, "keywords": keywords, "city": city,
                  "citylimit": citylimit, "page": page, "offset": offset, "extensions": extensions}
        if types:
            params["types"] = types
        response = _http_get(
            "https://restapi.amap.com/v3/place/text",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return _provider_failure("Text search", data, "v3", params)

        if not isinstance(data.get("pois"), list):
            return _result({"error": "Text search response invalid"}, "v3",
                           {key: value for key, value in params.items() if key != "key"},
                           "error", error_category="invalid_response")
        suggestion_cities = []
        if data.get("suggestion", {}).get("cities"):
            for city in data["suggestion"]["cities"]:
                suggestion_cities.append({"name": city.get("name")})

        pois = data.get("pois", [])
        result = {
            "suggestion": {
                "keywords": data.get("suggestion", {}).get("keywords"),
                "cities": suggestion_cities
            }, "pois": pois, "count": data.get("count"), "page": page, "offset": offset
        }
        return _result(result, "v3", {key: value for key, value in params.items() if key != "key"},
                       "ok" if pois else "empty")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        return _result({"error": f"Text search request failed ({category})"}, "v3",
                       {"keywords": keywords, "city": city, "citylimit": citylimit, "types": types,
                        "page": page, "offset": offset, "extensions": extensions}, "error",
                       error_category=category)
    except Exception:
        return _result({"error": "Text search response invalid"}, "v3",
                       {"keywords": keywords, "city": city, "citylimit": citylimit, "types": types,
                        "page": page, "offset": offset, "extensions": extensions}, "error",
                       error_category="invalid_response")

@mcp.tool()
def maps_around_search(location: str, radius: str = "1000", keywords: str = "", types: str = "",
                       page: str = "1", offset: str = "20", extensions: str = "base",
                       sortrule: str = "distance") -> Dict[str, Any]:
    """周边搜，根据用户传入关键词以及坐标location，搜索出radius半径范围的POI"""
    try:
        params = {"key": AMAP_MAPS_API_KEY, "location": location, "radius": radius,
                  "keywords": keywords, "page": page, "offset": offset,
                  "extensions": extensions, "sortrule": sortrule}
        if types:
            params["types"] = types
        response = _http_get(
            "https://restapi.amap.com/v3/place/around",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return _provider_failure("Around search", data, "v3", params)

        if not isinstance(data.get("pois"), list):
            return _result({"error": "Around search response invalid"}, "v3",
                           {key: value for key, value in params.items() if key != "key"},
                           "error", error_category="invalid_response")
        pois = data["pois"]
        return _result({"pois": pois, "count": data.get("count"), "page": page, "offset": offset}, "v3",
                       {key: value for key, value in params.items() if key != "key"}, "ok" if pois else "empty")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        params = {"location": location, "radius": radius, "keywords": keywords, "types": types,
                  "page": page, "offset": offset, "extensions": extensions, "sortrule": sortrule}
        return _result({"error": f"Around search request failed ({category})"}, "v3", params, "error",
                       error_category=category)
    except Exception:
        params = {"location": location, "radius": radius, "keywords": keywords, "types": types,
                  "page": page, "offset": offset, "extensions": extensions, "sortrule": sortrule}
        return _result({"error": "Around search response invalid"}, "v3", params, "error",
                       error_category="invalid_response")

@mcp.tool()
def maps_search_detail(id: str, extensions: str = "all") -> Dict[str, Any]:
    """查询关键词搜或者周边搜获取到的POI ID的详细信息"""
    try:
        params = {"key": AMAP_MAPS_API_KEY, "id": id, "extensions": extensions}
        response = _http_get(
            "https://restapi.amap.com/v3/place/detail",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

        if data["status"] != "1":
            return _provider_failure("POI detail", data, "v3", params)
        if not isinstance(data.get("pois"), list):
            return _result({"error": "POI detail response invalid"}, "v3", {"id": id, "extensions": extensions},
                           "error", error_category="invalid_response")
        if not data["pois"]:
            return _result({"error": "No POI found"}, "v3", {"id": id, "extensions": extensions}, "empty")

        poi = data["pois"][0]
        result = dict(poi)
        biz_ext = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
        for field, value in biz_ext.items():
            result.setdefault(field, value)
        result.setdefault("city", poi.get("cityname"))
        return _result(result, "v3", {"id": id, "extensions": extensions}, "ok")
    except requests.exceptions.RequestException as e:
        category = _safe_error(e)
        return _result({"error": f"POI detail request failed ({category})"}, "v3",
                       {"id": id, "extensions": extensions}, "error",
                       error_category=category)
    except Exception:
        return _result({"error": "POI detail response invalid"}, "v3", {"id": id, "extensions": extensions},
                       "error", error_category="invalid_response")

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Amap MCP Server")
    parser.add_argument('transport', nargs='?', default='stdio', choices=['stdio', 'sse', 'streamable-http'],
                        help='Transport type (stdio, sse, or streamable-http)')
    args = parser.parse_args()

    # Run the MCP server with the specified transport
    mcp.run(transport=args.transport)
