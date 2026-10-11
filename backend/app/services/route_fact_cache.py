"""按单个行程范围复用已核实的路线事实。

RouteQueryKey.effective_params 必须来自脱敏后的实际 HTTP 参数，而不是 MCP
工具 kwargs。固定版本 vendor 会把骑行工具的 origin_coordinates / destination_coordinates
映射为 HTTP origin / destination，并为公交请求补入 extensions=base；键由调用方
按实际参数构造，本服务不会推测或补齐 vendor 默认值。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import math
import time
from typing import Awaitable, Callable, Literal, Mapping, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.planning_v4.facts import MapCoordinate, RouteFact


RouteMode: TypeAlias = Literal["walking", "bicycling", "transit"]
_Scalar: TypeAlias = str | int | float | bool | None
_ParamFingerprint: TypeAlias = tuple[tuple[str, str, _Scalar], ...]
_LOADER_TTL_S = {"walking": 60 * 60, "bicycling": 60 * 60, "transit": 15 * 60}
_EMPTY_TTL_S = 60
_TOOL_CONTRACT: dict[str, tuple[str, str]] = {
    "walking": ("maps_direction_walking_by_coordinates", "v3"),
    "bicycling": ("maps_bicycling_by_coordinates", "v4"),
    "transit": ("maps_direction_transit_integrated_by_coordinates", "v3"),
}


class RouteFactCacheError(ValueError):
    """查询键或路线事实不符合当前缓存契约。"""


class RouteFactMismatchError(RouteFactCacheError):
    """加载结果与请求身份或实际参数不一致。"""


def _normalized_param_name(name: str) -> str:
    return "".join(char.lower() for char in name if char.isalnum())


def _validate_param_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise RouteFactCacheError("effective_params 的名称必须是非空字符串")
    normalized = _normalized_param_name(name)
    sensitive_fragments = ("key", "token", "secret", "authorization", "credential", "password", "apikey")
    if any(fragment in normalized for fragment in sensitive_fragments):
        raise RouteFactCacheError("effective_params 不得包含凭据字段")
    return name


def _typed_scalar(value: object) -> tuple[str, _Scalar]:
    if value is None:
        return "none", None
    if type(value) is bool:
        return "bool", value
    if type(value) is int:
        return "int", value
    if type(value) is float:
        if not math.isfinite(value):
            raise RouteFactCacheError("effective_params 不得包含 NaN 或无穷值")
        return "float", value
    if type(value) is str:
        return "str", value
    raise RouteFactCacheError("effective_params 仅支持脱敏标量值")


def _fingerprint_params(params: Mapping[str, object]) -> _ParamFingerprint:
    if not isinstance(params, Mapping):
        raise RouteFactCacheError("effective_params 必须是映射")
    rows: list[tuple[str, str, _Scalar]] = []
    for name, value in params.items():
        key = _validate_param_name(name)
        kind, scalar = _typed_scalar(value)
        rows.append((key, kind, scalar))
    rows.sort(key=lambda row: row[0])
    return tuple(rows)


def _unfingerprint_params(fingerprint: _ParamFingerprint) -> dict[str, _Scalar]:
    return {name: value for name, _, value in fingerprint}


def _coordinate_pair(value: MapCoordinate | tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, MapCoordinate):
        pair = (float(value.lng), float(value.lat))
    elif isinstance(value, tuple) and len(value) == 2:
        pair = (float(value[0]), float(value[1]))
    else:
        raise RouteFactCacheError("路线端点必须是经纬度坐标")
    lng, lat = pair
    if not math.isfinite(lng) or not math.isfinite(lat) or not -180 <= lng <= 180 or not -90 <= lat <= 90:
        raise RouteFactCacheError("路线坐标必须是有限的有效经纬度")
    return pair


@dataclass(frozen=True, slots=True)
class RouteQueryKey:
    """单次实际路线请求的身份；revision 和用户选择不属于查询条件。"""

    plan_id: str
    provider: str
    api_version: str
    adapter_version: str
    tool_name: str
    coordinate_system: str
    origin_poi_id: str
    origin: tuple[float, float]
    destination_poi_id: str
    destination: tuple[float, float]
    city: str | None
    destination_city: str | None
    mode: RouteMode
    effective_params: _ParamFingerprint
    requested_departure_at: datetime | None = None
    timezone_name: str = "Asia/Shanghai"

    @classmethod
    def from_effective_params(
        cls,
        *,
        plan_id: str,
        provider: str,
        api_version: str,
        adapter_version: str,
        tool_name: str,
        coordinate_system: str,
        origin_poi_id: str,
        origin: MapCoordinate | tuple[float, float],
        destination_poi_id: str,
        destination: MapCoordinate | tuple[float, float],
        city: str | None,
        destination_city: str | None,
        mode: RouteMode,
        effective_params: Mapping[str, object],
        requested_departure_at: datetime | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> RouteQueryKey:
        """从脱敏后的实际 HTTP 参数构造键，不接受 MCP 工具 kwargs 替代。

        例如，骑行键使用 vendor 输出的 origin/destination；公交键还要带上
        vendor 展开的 extensions 值。调用方必须提供完整 effective_params。
        """
        return cls(
            plan_id=plan_id,
            provider=provider,
            api_version=api_version,
            adapter_version=adapter_version,
            tool_name=tool_name,
            coordinate_system=coordinate_system,
            origin_poi_id=origin_poi_id,
            origin=_coordinate_pair(origin),
            destination_poi_id=destination_poi_id,
            destination=_coordinate_pair(destination),
            city=city,
            destination_city=destination_city,
            mode=mode,
            effective_params=_fingerprint_params(effective_params),
            requested_departure_at=requested_departure_at,
            timezone_name=timezone_name,
        )

    def __post_init__(self) -> None:
        required = (
            self.plan_id, self.provider, self.api_version, self.adapter_version, self.tool_name,
            self.coordinate_system, self.origin_poi_id, self.destination_poi_id,
        )
        if any(not isinstance(value, str) or not value for value in required):
            raise RouteFactCacheError("路线查询键的身份字段不能为空")
        if self.provider != "amap":
            raise RouteFactCacheError("当前路线缓存只接受 amap provider")
        if not isinstance(self.mode, str) or self.mode not in _TOOL_CONTRACT:
            raise RouteFactCacheError("不支持的路线交通方式")
        expected_tool, expected_api = _TOOL_CONTRACT[self.mode]
        if self.tool_name != expected_tool or self.api_version != expected_api:
            raise RouteFactCacheError("路线工具名与交通方式/API版本不匹配")
        if self.coordinate_system != "GCJ-02":
            raise RouteFactCacheError("当前路线事实只支持 GCJ-02 坐标")
        if (
            _coordinate_pair(self.origin) != self.origin
            or _coordinate_pair(self.destination) != self.destination
            or any(type(value) is not float for value in (*self.origin, *self.destination))
        ):
            raise RouteFactCacheError("路线坐标必须使用规范浮点值")
        if not isinstance(self.timezone_name, str) or not self.timezone_name:
            raise RouteFactCacheError("timezone_name 不能为空")
        try:
            timezone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RouteFactCacheError("timezone_name 必须是有效时区") from exc

        try:
            params = self.params_dict()
            canonical_params = _fingerprint_params(params)
        except (TypeError, ValueError) as exc:
            raise RouteFactCacheError("effective_params 必须是规范化的类型化标量") from exc
        if canonical_params != self.effective_params:
            raise RouteFactCacheError("effective_params 指纹必须唯一、排序且保留原值类型")
        if (
            params.get("origin") != f"{self.origin[0]},{self.origin[1]}"
            or params.get("destination") != f"{self.destination[0]},{self.destination[1]}"
        ):
            raise RouteFactCacheError("effective_params 端点坐标与查询键不匹配")
        if self.mode == "transit" and (self.city is None or self.destination_city is None):
            raise RouteFactCacheError("公共交通查询必须明确起点和终点城市")
        if self.mode == "transit" and type(params.get("extensions")) is not str:
            raise RouteFactCacheError("公交 effective_params 必须包含vendor展开后的 extensions")
        self._validate_city_param(params, "city", self.city)
        self._validate_city_param(params, "cityd", self.destination_city)

        if self.requested_departure_at is not None:
            if (
                self.mode != "transit"
                or not isinstance(self.requested_departure_at, datetime)
                or self.requested_departure_at.tzinfo is None
                or self.requested_departure_at.utcoffset() is None
            ):
                raise RouteFactCacheError("只有带时区的公共交通请求可以指定出发时刻")
            local = self.requested_departure_at.astimezone(timezone).replace(second=0, microsecond=0)
            # AMap 公交参数只发送到分钟。把请求时刻按实际发送精度收敛，
            # 使同一分钟内不同秒数不会造成错误的额外缓存分叉。
            object.__setattr__(self, "requested_departure_at", local)
            if (
                type(params.get("date")) is not str
                or type(params.get("time")) is not str
                or params.get("date") != local.date().isoformat()
                or params.get("time") != local.strftime("%H:%M")
            ):
                raise RouteFactCacheError("公交请求时刻必须与实际发送的 date/time 参数一致")
        elif "date" in params or "time" in params:
            raise RouteFactCacheError("effective_params 含公交 date/time 时必须记录请求出发时刻")

        if self.mode != "transit" and any(key in params for key in ("city", "cityd", "date", "time", "strategy")):
            raise RouteFactCacheError("步行和骑行请求不能含公共交通参数")
        if ("date" in params) != ("time" in params):
            raise RouteFactCacheError("公交 effective_params 的 date/time 必须同时提供")
        if "strategy" in params and self.mode != "transit":
            raise RouteFactCacheError("strategy 只适用于公共交通查询")
        if "strategy" in params and type(params["strategy"]) not in {int, str}:
            raise RouteFactCacheError("公交 strategy 参数必须保持整数或字符串类型")

    @staticmethod
    def _validate_city_param(params: dict[str, _Scalar], name: str, expected: str | None) -> None:
        if expected is None:
            if name in params:
                raise RouteFactCacheError(f"effective_params 的 {name} 未在查询键中声明")
            return
        if type(params.get(name)) is not str or params[name] != expected:
            raise RouteFactCacheError(f"effective_params 的 {name} 与查询键不匹配")

    def params_dict(self) -> dict[str, _Scalar]:
        """返回新的参数字典，调用者不能改写键内数据。"""
        return _unfingerprint_params(self.effective_params)


@dataclass(frozen=True, slots=True)
class LoadedRouteFact:
    """loader 的返回封套；adapter_version 不在当前 RouteFact 契约内。"""

    adapter_version: str
    fact: RouteFact


@dataclass(frozen=True, slots=True)
class RouteCacheStats:
    """缓存生命周期内的轻量计数，不包含页面或持久化观测。"""

    external_loads: int = 0
    hits: int = 0
    inflight_joins: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    fact: RouteFact
    expires_at: float


Loader = Callable[[RouteQueryKey], Awaitable[LoadedRouteFact]]


class RouteFactCache:
    """单 plan、单 event loop 的路线事实缓存与 in-flight 合并器。

    普通等待者取消不会取消共享 loader。显式 aclose 会取消本 plan 的 loader。
    失败只在并发等待者之间合并；本规划轮的失败记忆、重试预算和冷却由上层
    scheduler 负责，本服务不会声称已提供这些控制。
    """

    def __init__(
        self,
        plan_id: str,
        loader: Loader,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan_id 不能为空")
        self.plan_id = plan_id
        self._loader = loader
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._entries: dict[RouteQueryKey, _CacheEntry] = {}
        self._inflight: dict[RouteQueryKey, asyncio.Task[RouteFact]] = {}
        self._closed = False
        self._external_loads = 0
        self._hits = 0
        self._inflight_joins = 0
        self._errors = 0

    @property
    def stats(self) -> RouteCacheStats:
        return RouteCacheStats(
            external_loads=self._external_loads,
            hits=self._hits,
            inflight_joins=self._inflight_joins,
            errors=self._errors,
        )

    def _check_loop(self, *, allow_closed: bool = False) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("RouteFactCache 只能在首次使用它的 event loop 中调用")
        if self._closed and not allow_closed:
            raise RuntimeError("RouteFactCache 已关闭")
        return loop

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise RuntimeError("单调时钟必须返回有限数值")
        return value

    async def get_or_load(self, key: RouteQueryKey) -> RouteFact:
        """读取未过期事实，或合并到同键正在进行的 loader。"""
        loop = self._check_loop()
        if not isinstance(key, RouteQueryKey):
            raise TypeError("key 必须是 RouteQueryKey")
        if key.plan_id != self.plan_id:
            raise RouteFactCacheError("查询键 plan_id 与缓存作用域不一致")

        now = self._now()
        entry = self._entries.get(key)
        if entry is not None:
            if now < entry.expires_at:
                self._hits += 1
                return entry.fact.model_copy(deep=True)
            self._entries.pop(key, None)

        task = self._inflight.get(key)
        if task is None:
            self._external_loads += 1
            task = loop.create_task(self._load_and_cache(key))
            task.add_done_callback(self._consume_unobserved_exception)
            self._inflight[key] = task
        else:
            self._inflight_joins += 1

        fact = await asyncio.shield(task)
        return fact.model_copy(deep=True)

    async def _load_and_cache(self, key: RouteQueryKey) -> RouteFact:
        try:
            loaded = await self._loader(key)
            if self._closed:
                raise asyncio.CancelledError
            completed_at = self._now()
            fact = self._validate_loaded(key, loaded)
            if fact.status == "error":
                self._errors += 1
                return fact.model_copy(deep=True)

            # 使用 loader 完成时刻，而不是请求开始或缓存写入前的时刻。
            ttl = _EMPTY_TTL_S if fact.status == "empty" else _LOADER_TTL_S[key.mode]
            self._entries[key] = _CacheEntry(fact=fact.model_copy(deep=True), expires_at=completed_at + ttl)
            return fact.model_copy(deep=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._errors += 1
            raise
        finally:
            task = asyncio.current_task()
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    def _validate_loaded(self, key: RouteQueryKey, loaded: LoadedRouteFact) -> RouteFact:
        if not isinstance(loaded, LoadedRouteFact) or not isinstance(loaded.fact, RouteFact):
            raise RouteFactMismatchError("loader 必须返回 LoadedRouteFact 和 RouteFact")
        if loaded.adapter_version != key.adapter_version:
            raise RouteFactMismatchError("loader 返回的 adapter_version 与请求键不匹配")
        fact = loaded.fact
        expected_origin = MapCoordinate(
            lng=key.origin[0], lat=key.origin[1], coordinate_system=key.coordinate_system
        )
        expected_destination = MapCoordinate(
            lng=key.destination[0], lat=key.destination[1], coordinate_system=key.coordinate_system
        )
        source = fact.source
        params = key.params_dict()
        try:
            returned_params = _fingerprint_params(source.effective_params)
        except RouteFactCacheError as exc:
            raise RouteFactMismatchError("RouteFact 来源参数不符合安全标量契约") from exc

        if (
            fact.origin_poi_id != key.origin_poi_id
            or fact.destination_poi_id != key.destination_poi_id
            or fact.origin != expected_origin
            or fact.destination != expected_destination
            or fact.mode != key.mode
            or source.provider != key.provider
            or source.api_version != key.api_version
            or source.tool_name != key.tool_name
            or returned_params != key.effective_params
        ):
            raise RouteFactMismatchError("RouteFact 身份或 effective_params 与请求键不匹配")

        if key.mode == "transit":
            raw_strategy = params.get("strategy")
            expected_strategy: int | None = None
            if raw_strategy is not None:
                if type(raw_strategy) is int:
                    expected_strategy = raw_strategy
                elif type(raw_strategy) is str:
                    try:
                        expected_strategy = int(raw_strategy)
                    except ValueError as exc:
                        raise RouteFactMismatchError("公交 strategy 参数必须是整数") from exc
                else:
                    raise RouteFactMismatchError("公交 strategy 参数必须保持整数或字符串类型")
            if fact.requested_strategy != expected_strategy:
                raise RouteFactMismatchError("RouteFact requested_strategy 与实际参数不匹配")

            if key.requested_departure_at is None:
                if fact.status != "error" and (
                    "date" in params
                    or "time" in params
                    or fact.requested_departure_at is not None
                    or fact.time_verification != "not_requested"
                ):
                    raise RouteFactMismatchError("未指定公交时刻的请求不能返回声称已绑定时刻的事实")
            elif fact.status != "error":
                if fact.requested_departure_at is None:
                    raise RouteFactMismatchError("公交响应缺少请求出发时刻，不能作为该时刻的结果")
                zone = ZoneInfo(key.timezone_name)
                expected_local = key.requested_departure_at.astimezone(zone)
                actual_local = fact.requested_departure_at.astimezone(zone)
                expected_date_time = (expected_local.date().isoformat(), expected_local.strftime("%H:%M"))
                actual_date_time = (actual_local.date().isoformat(), actual_local.strftime("%H:%M"))
                sent_date_time = (params.get("date"), params.get("time"))
                if (
                    actual_date_time != expected_date_time
                    or actual_date_time != sent_date_time
                    or fact.time_verification not in {"sent_not_echoed", "provider_echoed"}
                ):
                    raise RouteFactMismatchError("RouteFact 出发时刻未与实际发送参数一致")
        elif fact.status != "error" and (
            fact.requested_departure_at is not None
            or fact.requested_strategy is not None
            or fact.time_verification != "not_requested"
        ):
            raise RouteFactMismatchError("步行和骑行 RouteFact 不能携带公交请求条件")

        return fact.model_copy(deep=True)

    @staticmethod
    def _consume_unobserved_exception(task: asyncio.Task[RouteFact]) -> None:
        """即使全部 waiter 已取消，也读取 loader 异常以避免未取回告警。"""
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        """关闭 plan 作用域，取消其未完成 loader 并释放缓存条目。"""
        self._check_loop(allow_closed=True)
        if self._closed:
            return
        self._closed = True
        self._entries.clear()
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
