"""派发草案的局部引用修复范围；不放宽事实、任务范围或依赖图校验。"""
from dataclasses import dataclass

from app.models.planning_v4.decision import DelegatePayload


RECOVERABLE_REFERENCE_CODES = frozenset({"MASTER_TASK_AREA_UNKNOWN", "MASTER_TASK_PREFERENCE_INVALID"})


@dataclass
class ReferenceRepair:
    original: DelegatePayload
    errors: list[dict]
    area_tasks: set[int]
    preference_tasks: set[int]
    allowed_areas: tuple[str, ...]
    allowed_preferences: tuple[str, ...]

    def feedback(self):
        return {
            "type": "repair_task_references",
            "instruction": "只修改下列错误涉及的任务引用，返回完整 delegate JSON。其他任务、目标、日期、时段、事实和区域定义保持原样；不得通过删除任务或改成 plan 绕过错误。区域必须来自 allowed_area_refs，偏好必须来自 allowed_preference_refs。同步修正受影响的区域依赖和餐次区域引用，不得更换餐次锚点。",
            "errors": self.errors,
            "allowed_area_refs": self.allowed_areas,
            "allowed_preference_refs": self.allowed_preferences,
            "original_decision": self.original.model_dump(mode="json", exclude_unset=True),
        }

    def merge_references(self, repaired) -> DelegatePayload | None:
        """只合并报告过的引用字段，其他内容从原草案保留，避免再次整份替换。"""
        if not isinstance(repaired, DelegatePayload):
            return None
        tasks = list(self.original.tasks)
        for index in self.area_tasks | self.preference_tasks:
            original = self.original.tasks[index]
            matches = [task for task in repaired.tasks if task.local_ref == original.local_ref]
            if len(matches) != 1:
                return None
            changed = matches[0]
            updates = {}
            if index in self.preference_tasks:
                updates["preference_refs"] = changed.preference_refs
            if index in self.area_tasks:
                updates["area_refs"] = changed.area_refs
                updates["dependencies"] = (
                    *(d for d in original.dependencies if d.kind != "area_hint"),
                    *(d for d in changed.dependencies if d.kind == "area_hint"),
                )
                meals = list(original.meal_areas)
                for meal_index, old in enumerate(meals):
                    if old.area_ref in self.allowed_areas:
                        continue
                    meal_matches = [m for m in changed.meal_areas if m.slot_id == old.slot_id]
                    if len(meal_matches) != 1:
                        return None
                    # 只改别名，不允许模型借此更换餐次或真实锚点。
                    meals[meal_index] = old.model_copy(update={"area_ref": meal_matches[0].area_ref})
                updates["meal_areas"] = tuple(meals)
            tasks[index] = original.model_copy(update=updates)
        return self.original.model_copy(update={"tasks": tuple(tasks)})


def prepare_reference_repair(context, payload) -> ReferenceRepair | None:
    if not isinstance(payload, DelegatePayload):
        return None
    areas = tuple(dict.fromkeys([a.area_hint_id for a in context.area_hints] + [a.local_ref for a in payload.areas]))
    preferences = tuple(dict.fromkeys((*context.planning_input.request.preferences,
                                     *context.planning_input.requirements.preferences)))
    errors, area_tasks, preference_tasks = [], set(), set()
    for index, task in enumerate(payload.tasks):
        if set(task.area_refs) - set(areas):
            errors.append({"code": "unknown_area_reference", "path": f"tasks[{index}].area_refs",
                           "guidance": "区域引用必须已在输入或本批 areas 中声明；不能把名称当作引用。"})
            area_tasks.add(index)
        if any(d.kind == "area_hint" and d.reference not in task.area_refs for d in task.dependencies):
            errors.append({"code": "area_dependency_outside_scope", "path": f"tasks[{index}].dependencies",
                           "guidance": "区域依赖必须同时列入该任务的 area_refs，并引用已声明区域。"})
            area_tasks.add(index)
        if len(task.preference_refs) != len(set(task.preference_refs)):
            errors.append({"code": "duplicate_preference_reference", "path": f"tasks[{index}].preference_refs",
                           "guidance": "去除重复偏好引用，保留原偏好。"})
            preference_tasks.add(index)
        if set(task.preference_refs) - set(preferences):
            errors.append({"code": "preference_reference_not_in_input", "path": f"tasks[{index}].preference_refs",
                           "guidance": "从原需求或整理结果的偏好中选择，不要在引用字段中改写偏好。"})
            preference_tasks.add(index)
    return ReferenceRepair(payload, errors, area_tasks, preference_tasks, areas, preferences) if errors else None
