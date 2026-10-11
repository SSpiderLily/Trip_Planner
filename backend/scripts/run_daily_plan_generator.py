"""手动验收 F06 每日安排生成；必须显式传入 --live 和事实输入文件。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.daily_plan_generator import DailyPlanGenerator  # noqa: E402
from app.agents.execution import PlanningError  # noqa: E402
from app.models.planning_v4 import AreaHint, CandidateRef, FactStore, PlanningInput, TaskResult  # noqa: E402


def _load_input(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return (
            PlanningInput.model_validate(raw["planning_input"]),
            tuple(CandidateRef.model_validate(item) for item in raw["candidates"]),
            FactStore.model_validate(raw["fact_store"]),
            tuple(AreaHint.model_validate(item) for item in raw["area_hints"]),
            tuple(TaskResult.model_validate(item) for item in raw["task_results"]),
            raw["source_run_id"],
        )
    except Exception:
        raise ValueError("F06 输入文件结构无效") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="确认将调用当前配置的真实 LLM",
    )
    parser.add_argument("--input-file", required=True, type=Path, help="PlanningInput、已核实候选、事实与区域的 JSON 文件")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    args = parser.parse_args()

    try:
        inputs = _load_input(args.input_file)
    except ValueError:
        print(json.dumps({
            "status": "failed",
            "error_code": "INPUT_INVALID",
            "message": "F06 事实输入文件无效",
        }, ensure_ascii=False, indent=2))
        return 2

    planning_input, candidates, facts, area_hints, task_results, source_run_id = inputs
    try:
        execution = DailyPlanGenerator(timeout_seconds=args.timeout_seconds).generate(
            planning_input,
            candidates,
            facts,
            area_hints,
            task_results,
            source_run_id=source_run_id,
        )
    except PlanningError as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": exc.code,
            "step": exc.step,
            "message": str(exc),
            "reference_issue_codes": list(getattr(exc, "reference_issue_codes", ())),
            "model_calls": getattr(exc, "model_calls", None),
            "format_repairs": getattr(exc, "format_repairs", None),
            "usage": getattr(exc, "usage", None),
        }, ensure_ascii=False, indent=2))
        return 1
    except Exception:
        print(json.dumps({
            "status": "failed",
            "error_code": "EXECUTION_FAILED",
            "message": "F06 每日安排验收未完成",
            "model_calls": getattr(exc, "model_calls", None),
            "format_repairs": getattr(exc, "format_repairs", None),
            "usage": getattr(exc, "usage", None),
        }, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({
        "status": "complete",
        "proposal": execution.proposal.model_dump(mode="json"),
        "draft": execution.draft.model_dump(mode="json"),
        "usage": execution.usage,
        "model_calls": execution.model_calls,
        "format_repairs": execution.format_repairs,
        "elapsed_seconds": execution.elapsed_seconds,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
