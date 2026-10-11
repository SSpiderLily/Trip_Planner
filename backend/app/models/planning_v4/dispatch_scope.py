"""把定向收集边界作为 F05/F09 之间的独立数据契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .base import ContractModel
from .facts import FactStore
from .input import PlanningInput
from .tasks import CandidateRef, Subtask, TaskResult


class DispatchTarget(ContractModel):
    """单个可补查缺项的角色、日期、时段和硬性要求范围。"""

    target_id: str = Field(min_length=1)
    issue_code: str = Field(min_length=1)
    issue_ids: tuple[str, ...] = ()
    issue_message: str | None = Field(default=None, max_length=500)
    role: Literal["attractions", "dining", "lodging"]
    day_id: str = Field(min_length=1)
    slot_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    area_hint_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_references(self):
        for values in (
            self.issue_ids, self.slot_ids, self.requirement_ids,
            self.candidate_ids, self.area_hint_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("dispatch target references must be unique")
        return self


class DispatchActivitySummary(ContractModel):
    """供定向收集参考的当前日期活动摘要；引用仍由后端核实。"""

    day_id: str = Field(min_length=1)
    activity_id: str = Field(min_length=1)
    kind: Literal["sightseeing", "meal", "free_time"]
    slot_id: str = Field(min_length=1)
    poi_id: str | None = None
    place_fact_id: str | None = None
    place_name: str | None = None


class DispatchAnchorSummary(ContractModel):
    """由同城带坐标地点事实支持的收集锚点。"""

    day_id: str = Field(min_length=1)
    poi_id: str = Field(min_length=1)
    place_fact_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    area_hint_id: str | None = None


class DispatchScope(ContractModel):
    """限定一次 F05 收集只处理指定缺项，不触及天气或到离端点查询。"""

    targets: tuple[DispatchTarget, ...] = Field(min_length=1)
    existing_activities: tuple[DispatchActivitySummary, ...] = ()
    anchors: tuple[DispatchAnchorSummary, ...] = ()

    @model_validator(mode="after")
    def validate_unique_targets(self):
        target_ids = [item.target_id for item in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("dispatch target IDs must be unique")
        activity_ids = [item.activity_id for item in self.existing_activities]
        if len(activity_ids) != len(set(activity_ids)):
            raise ValueError("dispatch activity IDs must be unique")
        anchor_keys = [(item.day_id, item.place_fact_id) for item in self.anchors]
        if len(anchor_keys) != len(set(anchor_keys)):
            raise ValueError("dispatch anchor facts must be unique per day")
        return self


def derived_dispatch_anchors(
    scope: DispatchScope,
    planning_input: PlanningInput,
    fact_store: FactStore,
    candidates: tuple[CandidateRef, ...],
    tasks: tuple[Subtask, ...],
    task_results: tuple[TaskResult, ...],
    *,
    run_id: str,
    context_revision: int,
    base_revision: int | None,
) -> tuple[DispatchAnchorSummary, ...]:
    """仅把当前定向景点任务成功核实的目标日地点提升为后续区域锚点。"""
    target_days = {
        target.day_id for target in scope.targets if target.role == "attractions"
    }
    if not target_days:
        return ()
    target_slots: dict[str, set[str]] = {}
    for target in scope.targets:
        if target.role == "attractions":
            target_slots.setdefault(target.day_id, set()).update(target.slot_ids)

    candidate_by_id = {item.candidate_id: item for item in candidates}
    place_by_id = {item.fact_id: item for item in fact_store.places}
    task_by_id = {item.task_id: item for item in tasks}
    result_by_id = {item.task_id: item for item in task_results}
    anchors: dict[tuple[str, str], DispatchAnchorSummary] = {}
    requested_city = _city_key(planning_input.request.city)

    for task_id, task in task_by_id.items():
        if task.role != "attractions" or task.run_id != run_id:
            continue
        if (task.context_revision != context_revision or task.base_revision != base_revision):
            continue
        result = result_by_id.get(task_id)
        if (result is None or result.run_id != run_id or result.context_revision != context_revision
                or result.base_revision != base_revision
                or result.request_id != planning_input.request.request_id):
            continue
        for candidate_ref in result.candidates:
            candidate = candidate_by_id.get(candidate_ref.candidate_id)
            place = place_by_id.get(candidate_ref.place_fact_id)
            if (candidate != candidate_ref or candidate is None
                    or candidate.category != "sightseeing"
                    or place is None or place.poi_id != candidate.poi_id
                    or place.location is None or not place.city
                    or _city_key(place.city) != requested_city):
                continue
            candidate_slot_days = {
                slot.day_id
                for day in planning_input.days
                for slot in day.slots
                if slot.slot_id in candidate.slot_ids
            }
            for day_id in target_days & set(task.scope.day_ids):
                allowed_slots = target_slots.get(day_id, set())
                task_slots = set(task.scope.slot_ids) & allowed_slots
                if task.scope.slot_ids and not task_slots:
                    continue
                if candidate.slot_ids and not set(candidate.slot_ids) & task_slots:
                    continue
                if candidate_slot_days and day_id not in candidate_slot_days:
                    continue
                anchors[(day_id, place.fact_id)] = DispatchAnchorSummary(
                    day_id=day_id,
                    poi_id=place.poi_id,
                    place_fact_id=place.fact_id,
                    place_name=place.name,
                )
    return tuple(anchors[key] for key in sorted(anchors))


def dispatch_scope_supports_area(
    scope: DispatchScope,
    anchors: tuple[DispatchAnchorSummary, ...],
    basis_fact_ids: tuple[str, ...],
    anchor_poi_ids: tuple[str, ...],
) -> bool:
    """要求一个定向区域的全部来源和锚点能由同一目标日证明。"""
    if not basis_fact_ids or not anchor_poi_ids:
        return False
    for day_id in {target.day_id for target in scope.targets}:
        day_anchors = tuple(item for item in anchors if item.day_id == day_id)
        fact_ids = {item.place_fact_id for item in day_anchors}
        poi_ids = {item.poi_id for item in day_anchors}
        if set(basis_fact_ids).issubset(fact_ids) and set(anchor_poi_ids).issubset(poi_ids):
            return True
    return False


def _city_key(value: str) -> str:
    normalized = "".join(value.split()).casefold()
    for suffix in ("特别行政区", "自治州", "地区", "市"):
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
    return normalized
