"""手动进行一次真实 F04b 住宿子任务验收；必须显式传入 --live。"""
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

from app.agents.execution import PlanningError  # noqa: E402
from app.agents.lodging_subtask import LodgingExecutionContext, LodgingSubtaskRunner  # noqa: E402
from app.models.planning_v4 import AreaHint, FactStore, RequirementSummary, RuleSet  # noqa: E402
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


def build_context(anchor_file: str | Path) -> tuple[Subtask, LodgingExecutionContext]:
    """读取 F03 脱敏 FactStore/AreaHint；上海 fixture 本身不指定住处。"""
    data = json.loads(Path(anchor_file).read_text(encoding="utf-8"))
    facts = FactStore.model_validate(data["fact_store"])
    hints = tuple(AreaHint.model_validate(item) for item in data["area_hints"])
    usable_hints = []
    dependencies = []
    known_fact_ids = set()
    for hint in hints:
        anchors = [
            place for place in facts.places
            if place.poi_id in hint.anchor_poi_ids and place.fact_id in hint.basis_fact_ids
            and place.location is not None and place.city
            and place.city.removesuffix("市") == "上海"
        ]
        if not anchors:
            continue
        usable_hints.append(hint)
        dependencies.append(TaskDependency(kind="area_hint", reference_id=hint.area_hint_id))
        basis_fact_ids = set(hint.basis_fact_ids)
        dependencies.extend(TaskDependency(kind="fact", reference_id=item) for item in sorted(basis_fact_ids))
        known_fact_ids.update(basis_fact_ids)
    if not usable_hints:
        raise ValueError("anchor file must contain a source-backed Shanghai AreaHint and real POI anchor")

    request = shanghai_request()
    requirements = RequirementSummary(
        requirements_id="f04b-live-requirements",
        request_id=request.request_id,
        core_goals=("上海代表性景点", "城市漫步与美食", "尽量少折返"),
        preferences=request.preferences,
    )
    rules = RuleSet(
        rule_version="f04b-live-f02-rules-v1",
        windows=WINDOWS,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    planning = build_planning_input(PlanningInputBuildRequest(
        request=request, requirements=requirements, rules=rules,
    ))
    task = Subtask(
        task_id="task-lodging-f04b-live",
        run_id="run-f04b-live",
        context_revision=1,
        base_revision=None,
        role="lodging",
        objective="为上海三日行程在已来源化的整趟活动区域中推荐一家酒店和对应区域。",
        scope=TaskScope(
            city=request.city,
            day_ids=tuple(day.day_id for day in planning.days),
            area_hint_ids=tuple(item.area_hint_id for item in usable_hints),
        ),
        dependencies=tuple(dependencies),
        known_fact_ids=tuple(sorted(known_fact_ids)),
        candidate_target=1,
        required_fields=("poi_id", "name", "location"),
        preferences=request.preferences,
    )
    context = LodgingExecutionContext(
        request_id=request.request_id,
        run_id=task.run_id,
        context_revision=task.context_revision,
        base_revision=task.base_revision,
        planning_input=planning,
        fact_store=facts,
        area_hints=tuple(usable_hints),
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
        runner = LodgingSubtaskRunner(llm=llm)
        with redirect_stdout(io.StringIO()):
            execution = runner.run(task, context)
        places = {place.fact_id: place for place in execution.fact_store.places}
        print(json.dumps({
            "request_lodging_query": context.planning_input.request.lodging_query,
            "status": execution.task_result.status,
            "stop_reason": execution.task_result.stop_reason,
            "coverage": [item.model_dump(mode="json") for item in execution.task_result.coverage],
            "candidates": [
                {
                    "poi_id": candidate.poi_id,
                    "place": places[candidate.place_fact_id].model_dump(mode="json")
                    if candidate.place_fact_id in places else None,
                    "area_hint_id": candidate.area_hint_id,
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
            "error_code": "LODGING_ACCEPTANCE_FAILED",
            "message": "F04b 住宿子任务验收未完成，请查看脱敏观测记录",
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception:
        print(json.dumps({
            "status": "failed",
            "error_code": "LODGING_ACCEPTANCE_FAILED",
            "message": "F04b 住宿子任务验收未完成，请查看脱敏观测记录",
        }, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
