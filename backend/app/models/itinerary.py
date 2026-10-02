"""新版行程契约：模型提出安排，程序核实地点并填充路段与汇总。"""
from typing import Literal, Any
from pydantic import BaseModel, Field, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class Place(Model):
    source: Literal['amap'] = 'amap'
    source_id: str = Field(min_length=1)
    name: str = ''
    address: str = ''
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    is_area_reference: bool = False
    tel: str | None = None
    photos: list[str] = Field(default_factory=list)
    opening_hours: str | None = None


class Cost(Model):
    amount: float | None = Field(default=None, ge=0)
    basis: str = '未知'


class ReferenceCost(Model):
    amount: float | None = Field(default=None, ge=0)
    currency: Literal['CNY'] = 'CNY'
    unit: str | None = None
    source: str | None = None
    status: Literal['available', 'missing'] = 'missing'


class Requirement(Model):
    requirement_id: str
    text: str = Field(description='用户明确必去的具体地点原意；一般偏好、饮食类型、排除项不能放入此列表')


class Conditions(Model):
    city: str = ''
    start_date: str = ''
    end_date: str = ''
    travel_days: int = 1
    preferences: list[str] = Field(default_factory=list)
    transportation: Literal['transit', 'walking', 'bicycling', 'driving'] = 'transit'
    daily_time_budget_minutes: int = 600
    must_visit_requests: list[Requirement] = Field(default_factory=list)
    budget_per_adult: float | None = Field(default=None, ge=0)
    remarks: str = ''
    interpretation_notes: list[str] = Field(default_factory=list)
    reminder_only_requests: list[str] = Field(default_factory=list)
    arrival_at: str | None = None
    departure_at: str | None = None
    arrival_place_id: str | None = None
    departure_place_id: str | None = None
    arrival_place: Place | None = None
    departure_place: Place | None = None
    budget_per_person: float | None = Field(default=None, ge=0)


class Search(Model):
    keywords: str = Field(min_length=1, max_length=200)
    category: Literal['sightseeing', 'meal', 'lodging']


class Collection(Model):
    conditions: Conditions | None = None
    searches: list[Search] = Field(default_factory=list, max_length=6)


class Lodging(Model):
    source: Literal['user', 'recommended']
    user_input: str | None = None
    area_name: str | None = None
    recommendation_reason: str | None = None
    place: Place | None = None
    reference_cost: ReferenceCost = Field(default_factory=ReferenceCost)


class Activity(Model):
    activity_id: str
    type: Literal['sightseeing', 'meal', 'free_time']
    period: Literal['morning', 'lunch', 'afternoon', 'dinner', 'evening']
    title: str
    description: str = ''
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    place: Place | None = None
    estimated_cost: Cost = Field(default_factory=Cost)
    requirement_ids: list[str] = Field(default_factory=list)
    opening_hours: str | None = None
    photos: list[str] = Field(default_factory=list)
    reference_cost: ReferenceCost | None = None


class Day(Model):
    date: str
    description: str = ''
    activities: list[Activity] = Field(default_factory=list, max_length=12)


class Draft(Model):
    lodging_base: Lodging
    days: list[Day] = Field(min_length=1, max_length=30)


class Itinerary(Model):
    schema_version: Literal[3] = 3
    planning_conditions: Conditions
    lodging_base: Lodging
    days: list[dict[str, Any]]
    cost_summary: dict
    issues: list[dict]
