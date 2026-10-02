"""数据模型定义"""

from typing import List, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    city: str = Field(..., description="目的地城市", example="北京")
    start_date: Optional[str] = Field(default=None, description="由到达时间推导的开始日期 YYYY-MM-DD", example="2025-06-01")
    end_date: Optional[str] = Field(default=None, description="由离开时间推导的结束日期 YYYY-MM-DD", example="2025-06-03")
    arrival_at: Optional[datetime] = Field(default=None, description="到达目的地的完整日期时间，Asia/Shanghai")
    departure_at: Optional[datetime] = Field(default=None, description="离开目的地的完整日期时间，Asia/Shanghai")
    arrival_place_id: Optional[str] = Field(default=None, max_length=100, description="可选到达地点高德POI ID")
    departure_place_id: Optional[str] = Field(default=None, max_length=100, description="可选离开地点高德POI ID")
    budget_per_person: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False, description="人均人民币参考预算，不含交通")
    travel_days: Optional[int] = Field(default=None, description="按日期自动计算；兼容旧客户端", ge=1, le=30)
    transportation: str = Field(default="公共交通", description="旧客户端交通选项")
    accommodation: str = Field(default="", description="旧客户端住宿偏好")
    lodging: str = Field(default="", max_length=300, description="已定住处，可留空")
    preferences: List[str] = Field(default=[], description="旅行偏好标签", example=["历史文化", "美食"])
    free_text_input: Optional[str] = Field(default="", description="额外要求", example="希望多安排一些博物馆")

    @model_validator(mode="before")
    @classmethod
    def derive_dates_from_times(cls, values: Any):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        destination_tz = ZoneInfo("Asia/Shanghai")
        for time_field, date_field in (("arrival_at", "start_date"), ("departure_at", "end_date")):
            if values.get(date_field) is not None or values.get(time_field) is None:
                continue
            try:
                raw = values[time_field]
                parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                local = parsed.replace(tzinfo=destination_tz) if parsed.tzinfo is None else parsed.astimezone(destination_tz)
                values[date_field] = local.date().isoformat()
            except (TypeError, ValueError):
                # Let pydantic report the malformed datetime through the field itself.
                pass
        return values
    
    @model_validator(mode="after")
    def validate_dates(self):
        destination_tz = ZoneInfo("Asia/Shanghai")
        for field_name in ("arrival_at", "departure_at"):
            value = getattr(self, field_name)
            if value is not None:
                value = value.replace(tzinfo=destination_tz) if value.tzinfo is None else value.astimezone(destination_tz)
                setattr(self, field_name, value)
        if self.start_date is None or self.end_date is None:
            raise ValueError("请提供到达和离开时间，系统据此确定旅行日期")
        start, end = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        if start.isoformat() != self.start_date or end.isoformat() != self.end_date:
            raise ValueError("日期必须采用 YYYY-MM-DD")
        days = (end - start).days + 1
        if not 1 <= days <= 30:
            raise ValueError("旅行天数须在1至30天之间")
        if not self.city.strip():
            raise ValueError("目的地不能为空")
        if (self.arrival_at is None) != (self.departure_at is None):
            raise ValueError("到达和离开时间必须同时填写")
        if self.arrival_at is not None:
            # 无时区值按目的地本地时区处理；带时区值保留并在规划时换算。
            if self.arrival_at.date().isoformat() != self.start_date or self.departure_at.date().isoformat() != self.end_date:
                raise ValueError("到达和离开时间的日期必须与旅行起止日期一致")
            if self.departure_at <= self.arrival_at:
                raise ValueError("离开时间必须晚于到达时间")
        if self.travel_days is None:
            self.travel_days = days
        if (end - start).days + 1 != self.travel_days:
            raise ValueError("旅行天数必须与起止日期范围（含首尾）一致")
        return self

    class Config:
        json_schema_extra = {
            "example": {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆"
            }
        }


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", example="故宫")
    city: str = Field(..., description="城市", example="北京")
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", example="北京市朝阳区阜通东大街6号")
    destination_address: str = Field(..., description="终点地址", example="北京市海淀区上地十街10号")
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


class DayEditOperation(BaseModel):
    type: str = Field(..., pattern="^(add_poi|delete_activity|move_activity|select_leg_mode)$")
    poi_id: Optional[str] = Field(default=None, max_length=100)
    client_activity_id: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern="^[A-Za-z0-9_-]+$")
    activity_id: Optional[str] = Field(default=None, max_length=100)
    confirmed: Optional[bool] = None
    direction: Optional[str] = Field(default=None, pattern="^(up|down)$")
    from_activity_id: Optional[str] = Field(default=None, max_length=100)
    to_activity_id: Optional[str] = Field(default=None, max_length=100)
    mode: Optional[str] = Field(default=None, pattern="^(walking|bicycling|transit|driving)$")

    @model_validator(mode="after")
    def validate_operation(self):
        if self.type == "add_poi" and (not self.poi_id or not self.client_activity_id):
            raise ValueError("新增地点必须包含poi_id和client_activity_id")
        if self.type == "add_poi" and self.client_activity_id in {"lodging", "arrival", "departure"}:
            raise ValueError("新增活动不能使用行程保留标识")
        if self.type == "delete_activity" and (not self.activity_id or self.confirmed is not True):
            raise ValueError("删除景点必须指定activity_id并确认")
        if self.type == "move_activity" and (not self.activity_id or not self.direction):
            raise ValueError("调整顺序必须指定activity_id和方向")
        if self.type == "select_leg_mode" and not all((self.from_activity_id, self.to_activity_id, self.mode)):
            raise ValueError("切换交通方式必须指定路段和方式")
        return self


class RecalculateDayRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=120)
    date: str = Field(..., pattern="^\\d{4}-\\d{2}-\\d{2}$")
    edit_token: str = Field(..., min_length=20, max_length=100000)
    client_revision: int = Field(..., ge=0)
    request_id: str = Field(..., min_length=1, max_length=128)
    operations: List[DayEditOperation] = Field(..., min_length=1, max_length=20)


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票价格(元)")


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, description="预估费用(元)")


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, description="预估费用(元/晚)")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default=[], description="景点列表")
    meals: List[Meal] = Field(default=[], description="餐饮列表")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Optional[int] = Field(default=None, description="白天温度")
    night_temp: Optional[int] = Field(default=None, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return None
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default=[], description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")
    photos: List[str] = Field(default_factory=list, description="查询到的图片地址")
    opening_hours: Optional[str] = Field(default=None, description="查询到的开放时间")
    reference_cost: Optional[float] = Field(default=None, ge=0, description="地图查询到的参考消费")
    cost_basis: Optional[str] = Field(default=None, description="参考消费单位或适用说明")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")
