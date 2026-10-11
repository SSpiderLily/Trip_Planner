"""手动进行一次真实 F04a 餐饮子任务验收；必须显式传入 --live。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.dining_subtask import (  # noqa: E402
    DiningExecutionContext,
    DiningMealAreaBinding,
    DiningSubtaskRunner,
)
from app.agents.execution import PlanningError  # noqa: E402
from app.models.planning_v4 import FactStore, HardRequirement, RequirementSummary, RuleSet  # noqa: E402
from app.models.planning_v4.input import RequirementSource  # noqa: E402
from app.models.planning_v4.plan import AreaHint  # noqa: E402
from app.models.planning_v4.tasks import Subtask, TaskDependency, TaskScope  # noqa: E402
from app.services.llm_service import get_llm  # noqa: E402
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input  # noqa: E402
from scripts.run_requirement_interpreter import shanghai_request  # noqa: E402


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
MUST_VISIT = "必须去外滩"


def build_context(anchor_file: str | Path) -> tuple[Subtask, DiningExecutionContext]:
    """读取脱敏 F03 FactStore/AreaHint，并建立一个首日晚餐的验收子任务。"""
    data = json.loads(Path(anchor_file).read_text(encoding="utf-8"))
    facts = FactStore.model_validate(data["fact_store"])
    hints = tuple(AreaHint.model_validate(item) for item in data["area_hints"])
    hint = next((item for item in hints if item.anchor_poi_ids and item.basis_fact_ids), None)
    if hint is None:
        raise ValueError("anchor file must contain a source-backed area hint")
    anchor = next((place for place in facts.places
                   if place.poi_id in hint.anchor_poi_ids
                   and place.fact_id in hint.basis_fact_ids
                   and place.location is not None), None)
    if anchor is None:
        raise ValueError("area hint must reference a source-backed anchor place")

    request = shanghai_request()  # Retains the original fixed three-day request unchanged.
    requirements = RequirementSummary(
        requirements_id="f04a-live-requirements",
        request_id=request.request_id,
        core_goals=("上海代表性景点", "城市漫步与美食", "尽量少折返"),
        hard_requirements=(HardRequirement(
            requirement_id="M1", kind="must_visit", text=MUST_VISIT,
            source=RequirementSource(field="remarks", quote=MUST_VISIT),
        ),),
        preferences=request.preferences,
    )
    rules = RuleSet(
        rule_version="f04a-live-f02-rules-v1",
        windows=WINDOWS,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    planning = build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=requirements,
        rules=rules,
    ))
    dinner = next(
        slot for day in planning.days for slot in day.slots
        if slot.period == "dinner" and slot.eligibility != "skipped"
    )
    task = Subtask(
        task_id="task-dining-f04a-live",
        run_id="run-f04a-live",
        context_revision=1,
        base_revision=None,
        role="dining",
        objective=(
            "验收范围示例：为首日晚餐在指定的来源化外滩区域锚点附近查找餐馆。"
            "这个子任务范围不是原始旅行请求新增的硬性条件。"
        ),
        scope=TaskScope(
            city=request.city,
            day_ids=(dinner.day_id,),
            slot_ids=(dinner.slot_id,),
            area_hint_ids=(hint.area_hint_id,),
        ),
        dependencies=(
            TaskDependency(kind="area_hint", reference_id=hint.area_hint_id),
            TaskDependency(kind="fact", reference_id=anchor.fact_id),
        ),
        known_fact_ids=(anchor.fact_id,),
        candidate_target=1,
        required_fields=("poi_id", "name", "location"),
        preferences=request.preferences,
    )
    context = DiningExecutionContext(
        request_id=request.request_id,
        run_id=task.run_id,
        context_revision=task.context_revision,
        base_revision=task.base_revision,
        planning_input=planning,
        fact_store=facts,
        area_hints=hints,
        ready_task_ids=frozenset(),
        meal_areas=(DiningMealAreaBinding(
            slot_id=dinner.slot_id,
            area_hint_id=hint.area_hint_id,
            anchor_poi_id=anchor.poi_id,
        ),),
    )
    return task, context


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", required=True,
        help="确认将调用当前配置的真实 LLM 和高德 MCP 服务",
    )
    parser.add_argument(
        "--anchor-file", required=True,
        help="脱敏 JSON 文件，顶层包含 fact_store 与 area_hints（F03 已核实输出）",
    )
    args = parser.parse_args(argv)

    try:
        task, context = build_context(args.anchor_file)
        with redirect_stdout(io.StringIO()):
            llm = get_llm()
        runner = DiningSubtaskRunner(llm=llm)
        with redirect_stdout(io.StringIO()):
            execution = runner.run(task, context)
        places = {place.fact_id: place for place in execution.fact_store.places}
        print(json.dumps({
            "status": execution.task_result.status,
            "stop_reason": execution.task_result.stop_reason,
            "coverage": [item.model_dump(mode="json") for item in execution.task_result.coverage],
            "candidates": [
                {
                    "poi_id": candidate.poi_id,
                    "place": places[candidate.place_fact_id].model_dump(mode="json")
                    if candidate.place_fact_id in places else None,
                    "slot_ids": list(candidate.slot_ids),
                    "requirement_ids": list(candidate.requirement_ids),
                    "recommendation_reason": candidate.recommendation_reason,
                }
                for candidate in execution.task_result.candidates
            ],
            "tool_calls": [item.__dict__ for item in execution.tool_calls],
            "model_calls": execution.model_calls,
            "elapsed_seconds": execution.elapsed_seconds,
        }, ensure_ascii=False, indent=2))
        return 0 if execution.task_result.status == "complete" else 1
    except (PlanningError, ValueError, KeyError, TypeError):
        print(json.dumps({
            "status": "failed",
            "error_code": "DINING_ACCEPTANCE_FAILED",
            "message": "F04a 餐饮子任务验收未完成，请查看脱敏观测记录",
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception:
        print(json.dumps({
            "status": "failed",
            "error_code": "DINING_ACCEPTANCE_FAILED",
            "message": "F04a 餐饮子任务验收未完成，请查看脱敏观测记录",
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
