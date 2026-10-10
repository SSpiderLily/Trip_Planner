"""v4 地图事实、来源与 MCP 结果类型。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, Field, PositiveInt, model_validator

from .base import ContractModel, CoordinateSystem, ParamScalar, Provider


class FactSource(ContractModel):
    tool_call_id: str = Field(min_length=1)
    provider: Provider = "amap"
    tool_name: str = Field(min_length=1)
    api_version: Literal["v3", "v4"]
    queried_at: AwareDatetime
    effective_params: dict[str, ParamScalar]

    @model_validator(mode="after")
    def reject_credentials(self):
        forbidden = {"key", "apikey", "amapapikey", "token", "authorization", "secret"}
        normalized = ("".join(character for character in key.lower() if character.isalnum())
                      for key in self.effective_params)
        if any(key in forbidden for key in normalized):
            raise ValueError("effective_params 不得包含凭据")
        return self


class MissingField(ContractModel):
    field: str = Field(min_length=1)
    reason: Literal["not_returned", "not_supported", "unparseable", "out_of_coverage"]


class MapCoordinate(ContractModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    coordinate_system: CoordinateSystem = "GCJ-02"


class ReferenceCost(ContractModel):
    raw: str | None = None
    value: Decimal | None = Field(default=None, ge=0)
    unit: Literal["per_person_reference"] = "per_person_reference"


class PlaceFact(ContractModel):
    fact_id: str = Field(min_length=1)
    poi_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    location: MapCoordinate | None = None
    address: str | None = None
    city: str | None = None
    adcode: str | None = None
    type_name: str | None = None
    typecode: str | None = None
    business_area: str | None = None
    cost: ReferenceCost = Field(default_factory=ReferenceCost)
    opening_hours_raw: str | None = None
    photos: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    field_sources: dict[str, str] = Field(default_factory=dict)
    missing_fields: tuple[MissingField, ...] = ()


class PlaceSearchData(ContractModel):
    places: tuple[PlaceFact, ...] = ()
    page: PositiveInt | None = None
    offset: PositiveInt | None = None
    provider_count: int | None = Field(default=None, ge=0)


class WeatherForecast(ContractModel):
    forecast_date: date
    day_weather: str | None = None
    night_weather: str | None = None
    day_temp_c: float | None = None
    night_temp_c: float | None = None
    day_wind: str | None = None
    night_wind: str | None = None
    day_power: str | None = None
    night_power: str | None = None


class WeatherFact(ContractModel):
    fact_id: str = Field(min_length=1)
    city: str = Field(min_length=1)
    adcode: str | None = None
    report_time: AwareDatetime | None = None
    forecasts: tuple[WeatherForecast, ...] = ()
    source: FactSource
    missing_dates: tuple[date, ...] = ()


class TransitStop(ContractModel):
    name: str | None = None
    stop_id: str | None = None
    location: MapCoordinate | None = None


class TransitLine(ContractModel):
    mode: Literal["bus", "subway", "railway"]
    line_name: str | None = None
    line_type: str | None = None
    distance_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    polyline: tuple[MapCoordinate, ...] = ()
    departure_stop: TransitStop | None = None
    arrival_stop: TransitStop | None = None
    via_stops: tuple[TransitStop, ...] = ()
    instructions: tuple[str, ...] = ()
    start_time: str | None = None
    end_time: str | None = None
    station_start_time: str | None = None
    station_end_time: str | None = None
    via_stop_count: int | None = Field(default=None, ge=0)


class RouteSegment(ContractModel):
    sequence: int = Field(ge=0)
    mode: Literal["walking", "bicycling", "bus", "subway", "railway"]
    distance_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    polyline: tuple[MapCoordinate, ...] = ()
    line_name: str | None = None
    line_type: str | None = None
    departure_stop: TransitStop | None = None
    arrival_stop: TransitStop | None = None
    via_stops: tuple[TransitStop, ...] = ()
    instructions: tuple[str, ...] = ()
    transit_alternatives: tuple[TransitLine, ...] = ()
    entrance: TransitStop | None = None
    exit: TransitStop | None = None
    provider_attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RouteOption(ContractModel):
    option_id: str = Field(min_length=1)
    duration_s: float | None = Field(default=None, ge=0)
    distance_m: float | None = Field(default=None, ge=0)
    segments: tuple[RouteSegment, ...] = ()
    geometry_status: Literal["complete", "partial", "missing"]
    provider_attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def geometry_matches_segments(self):
        sequences = [segment.sequence for segment in self.segments]
        if sequences != list(range(len(sequences))):
            raise ValueError("路线分段 sequence 必须从0开始连续排列")
        segment_any_geometry = [
            bool(segment.polyline) or any(line.polyline for line in segment.transit_alternatives)
            for segment in self.segments
        ]
        segment_complete = [
            bool(segment.polyline) or (
                bool(segment.transit_alternatives)
                and all(line.polyline for line in segment.transit_alternatives)
            )
            for segment in self.segments
        ]
        has_geometry = any(segment_any_geometry)
        has_missing = any(not complete for complete in segment_complete)
        if self.geometry_status == "complete" and (not self.segments or not has_geometry or has_missing):
            raise ValueError("complete 路径必须包含所有分段的几何坐标")
        if self.geometry_status == "missing" and has_geometry:
            raise ValueError("missing 路径不能包含几何坐标")
        if self.geometry_status == "partial" and not (has_geometry and has_missing):
            raise ValueError("partial 路径必须同时包含和缺少分段几何")
        for segment in self.segments:
            if segment.transit_alternatives and segment.mode not in {line.mode for line in segment.transit_alternatives}:
                raise ValueError("路线段的交通方式必须与其备选线路之一相符")
        return self


class RouteFact(ContractModel):
    fact_id: str = Field(min_length=1)
    origin_poi_id: str = Field(min_length=1)
    destination_poi_id: str = Field(min_length=1)
    origin: MapCoordinate
    destination: MapCoordinate
    mode: Literal["walking", "bicycling", "transit"]
    status: Literal["available", "empty", "error"]
    requested_departure_at: AwareDatetime | None = None
    requested_strategy: int | None = None
    provider_departure_at: AwareDatetime | None = None
    time_verification: Literal["not_requested", "not_sent", "sent_not_echoed", "provider_echoed"] = "not_requested"
    options: tuple[RouteOption, ...] = ()
    source: FactSource
    missing_fields: tuple[MissingField, ...] = ()
    error_category: Literal[
        "invalid_request",
        "auth",
        "rate_limit",
        "timeout",
        "network",
        "provider_error",
        "invalid_response",
    ] | None = None

    @model_validator(mode="after")
    def validate_status_and_time_evidence(self):
        if self.status == "available" and not self.options:
            raise ValueError("available 路线必须含至少一个方案")
        if self.status != "available" and self.options:
            raise ValueError("empty/error 路线不能包含有效方案")
        if self.mode == "transit" and self.requested_departure_at is not None:
            if self.time_verification == "not_requested":
                raise ValueError("公共交通指定出发时间必须标记是否已发送")
            if self.time_verification == "provider_echoed" and self.provider_departure_at != self.requested_departure_at:
                raise ValueError("供应商回显时间必须与请求时间一致")
        elif self.time_verification != "not_requested" or self.provider_departure_at is not None:
            raise ValueError("未指定出发时间时不能声称时间已发送或已回显")
        if self.mode != "transit" and (self.time_verification != "not_requested" or self.provider_departure_at is not None):
            raise ValueError("非公共交通路线不应设置公交时刻核验状态")
        return self


class ToolError(ContractModel):
    category: Literal[
        "invalid_request",
        "auth",
        "rate_limit",
        "timeout",
        "network",
        "provider_error",
        "invalid_response",
    ]
    provider_code: str | None = None
    message: str = "地图查询失败"
    retryable: bool


T = TypeVar("T", bound=ContractModel)


class ToolResult(ContractModel, Generic[T]):
    tool_call_id: str = Field(min_length=1)
    status: Literal["ok", "empty", "error"]
    data: T | None = None
    missing_fields: tuple[MissingField, ...] = ()
    source: FactSource
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.status == "error" and self.error is None:
            raise ValueError("error 结果必须包含分类错误")
        if self.status != "error" and self.error is not None:
            raise ValueError("成功结果不能携带工具错误")
        if self.status == "ok" and self.data is None:
            raise ValueError("ok 结果必须包含类型化 data")
        return self


class FactStore(ContractModel):
    sources: tuple[FactSource, ...] = ()
    places: tuple[PlaceFact, ...] = ()
    weather: tuple[WeatherFact, ...] = ()
    routes: tuple[RouteFact, ...] = ()

    @model_validator(mode="after")
    def validate_unique_fact_ids(self):
        ids = [fact.fact_id for fact in (*self.places, *self.weather, *self.routes)]
        if len(ids) != len(set(ids)):
            raise ValueError("FactStore 中 fact_id 必须唯一")
        source_ids = [source.tool_call_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("FactStore 中 tool_call_id 必须唯一")
        return self
