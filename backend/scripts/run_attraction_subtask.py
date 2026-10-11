"""手动进行一次真实 F03 单景点任务验收；必须显式传入 --live。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.attraction_subtask import AttractionExecutionContext, AttractionSubtaskRunner  # noqa: E402
from app.agents.execution import PlanningError  # noqa: E402
from app.models.planning_v4 import HardRequirement, RequirementSummary, RuleSet  # noqa: E402
from app.models.planning_v4.input import RequirementSource  # noqa: E402
from app.models.planning_v4.tasks import Subtask, TaskScope  # noqa: E402
from app.services.llm_service import get_llm  # noqa: E402
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input  # noqa: E402
from run_requirement_interpreter import shanghai_request  # noqa: E402


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
MUST_VISIT = "必须去外滩"


def build_context() -> tuple[Subtask, AttractionExecutionContext]:
    # Reuse the complete F01 fixture. Constructing it is pure local setup; this
    # script does not invoke F01 or make another model call for requirements.
    request = shanghai_request()
    requirements = RequirementSummary(
        requirements_id="f03-live-requirements",
        request_id=request.request_id,
        core_goals=("上海代表性景点", "城市漫步与美食", "尽量少折返"),
        hard_requirements=(HardRequirement(
            requirement_id="M1",
            kind="must_visit",
            text=MUST_VISIT,
            source=RequirementSource(field="remarks", quote=MUST_VISIT),
        ),),
        preferences=("城市漫步", "上海代表性景点"),
    )
    rules = RuleSet(
        rule_version="f03-live-f02-rules-v1",
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
    task = Subtask(
        task_id="task-attractions-f03-live",
        run_id="run-f03-live",
        context_revision=1,
        base_revision=None,
        role="attractions",
        objective="只核实上海外滩这个必去景点，并将其分配到首日下午时段。只做一轮关键词搜索，随后结束。",
        scope=TaskScope(
            city="上海",
            day_ids=("day-2026-10-10",),
            slot_ids=("slot-2026-10-10-afternoon",),
        ),
        requirement_ids=("M1",),
        preferences=("城市漫步", "上海代表性景点"),
        candidate_target=1,
        required_fields=("poi_id", "name", "location"),
    )
    context = AttractionExecutionContext(
        request_id=request.request_id,
        run_id=task.run_id,
        context_revision=task.context_revision,
        base_revision=task.base_revision,
        planning_input=planning,
    )
    return task, context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="确认将调用当前配置的真实 LLM 和高德 MCP 服务",
    )
    parser.parse_args()

    try:
        task, context = build_context()
        # Existing service constructors print status lines. Keep the acceptance
        # output machine-readable and never echo configuration or provider text.
        with redirect_stdout(io.StringIO()):
            llm = get_llm()
        runner = AttractionSubtaskRunner(llm=llm)
        with redirect_stdout(io.StringIO()):
            execution = runner.run(task, context)
    except PlanningError as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": exc.code,
            "step": exc.step,
            "message": "F03 景点子任务验收未完成",
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception:
        print(json.dumps({
            "status": "failed",
            "error_code": "EXECUTION_FAILED",
            "message": "F03 景点子任务验收未完成，请查看脱敏观测记录",
        }, ensure_ascii=False, indent=2))
        return 1

    report = {
        "status": execution.task_result.status,
        "input_scope": {
            "request_id": context.request_id,
            "city": context.planning_input.request.city,
            "arrival_at": context.planning_input.request.arrival_at.isoformat(),
            "departure_at": context.planning_input.request.departure_at.isoformat(),
            "day_ids": list(task.scope.day_ids),
            "slot_ids": list(task.scope.slot_ids),
            "requirement_ids": list(task.requirement_ids),
        },
        "task_result": execution.task_result.model_dump(mode="json"),
        "places": [
            {
                "fact_id": place.fact_id,
                "poi_id": place.poi_id,
                "name": place.name,
                "location": place.location.model_dump(mode="json") if place.location else None,
                "city": place.city,
                "type_name": place.type_name,
                "typecode": place.typecode,
                "missing_fields": [item.model_dump(mode="json") for item in place.missing_fields],
                "source_refs": list(place.source_refs),
            }
            for place in execution.fact_store.places
        ],
        "sources": [source.model_dump(mode="json") for source in execution.fact_store.sources],
        "tool_calls": [call.__dict__ for call in execution.tool_calls],
        "usage": execution.usage,
        "model_calls": execution.model_calls,
        "format_repairs": execution.format_repairs,
        "elapsed_seconds": execution.elapsed_seconds,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if execution.task_result.status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
