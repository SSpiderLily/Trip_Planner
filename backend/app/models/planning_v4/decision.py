"""主智能体单次派发的模型输入输出契约。"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from .base import ContractModel
from .plan import AreaHint
from .tasks import Subtask


class DecisionDependency(ContractModel):
    """模型给出的逻辑依赖；local_ref 由后端解析成真实标识。"""

    kind: Literal["fact", "area_hint", "task"]
    reference: str = Field(min_length=1)


class AreaSpec(ContractModel):
    local_ref: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=120)
    anchor_poi_ids: tuple[str, ...] = ()
    basis_fact_ids: tuple[str, ...] = Field(min_length=1)


class MealAreaSpec(ContractModel):
    slot_id: str = Field(min_length=1)
    area_ref: str = Field(min_length=1)
    anchor_poi_id: str = Field(min_length=1)


class NamedRestaurantSpec(ContractModel):
    """把用户原文中的指定餐馆名绑定到一个 must_include 要求。"""

    requirement_id: str = Field(min_length=1)
    restaurant_name: str = Field(min_length=1, max_length=120)


class TaskSpec(ContractModel):
    """不含 task/run/request/version ID 的模型派发草案。"""

    local_ref: str = Field(min_length=1)
    role: Literal["attractions", "dining", "lodging"]
    objective: str = Field(min_length=1, max_length=1000)
    day_ids: tuple[str, ...] = Field(min_length=1)
    slot_ids: tuple[str, ...] = ()
    area_refs: tuple[str, ...] = ()
    dependencies: tuple[DecisionDependency, ...] = ()
    known_fact_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    excluded_poi_ids: tuple[str, ...] = ()
    candidate_target: int = Field(ge=1, le=20)
    preference_refs: tuple[str, ...] = ()
    meal_areas: tuple[MealAreaSpec, ...] = ()
    must_eat_restaurants: tuple[NamedRestaurantSpec, ...] = ()


class DelegatePayload(ContractModel):
    action: Literal["delegate"]
    reason: str = Field(min_length=1, max_length=500)
    areas: tuple[AreaSpec, ...] = ()
    tasks: tuple[TaskSpec, ...] = ()
    weather_dates: tuple[date, ...] = ()
    verify_arrival_place: bool = False
    verify_departure_place: bool = False


class PlanPayload(ContractModel):
    """仅表示收集阶段结束，F06 另行生成每日计划。"""

    action: Literal["plan"]
    reason: str = Field(min_length=1, max_length=500)
    missing_information: tuple[str, ...] = ()


class ResolvedMealArea(ContractModel):
    slot_id: str
    area_hint_id: str
    anchor_poi_id: str


class ResolvedNamedRestaurant(ContractModel):
    """可直接转换为 F04a DiningRestaurantRequirement 的来源化名称。"""

    requirement_id: str
    restaurant_name: str


class ResolvedTask(ContractModel):
    local_ref: str
    subtask: Subtask
    meal_areas: tuple[ResolvedMealArea, ...] = ()
    must_eat_restaurants: tuple[ResolvedNamedRestaurant, ...] = ()


class MasterDecisionOutcome(ContractModel):
    """后端定稿结果；所有身份与版本字段均由服务端注入。"""

    action: Literal["delegate", "plan"]
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    context_revision: int = Field(ge=1)
    base_revision: int | None = Field(default=None, ge=1)
    rule_version: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    missing_information: tuple[str, ...] = ()
    areas: tuple[AreaHint, ...] = ()
    tasks: tuple[ResolvedTask, ...] = ()
    weather_dates: tuple[date, ...] = ()
    verify_arrival_place: bool = False
    verify_departure_place: bool = False
