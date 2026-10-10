"""用设计页中的固定上海需求单独核验 F01。"""
import argparse
from contextlib import redirect_stdout
import io
import json
import sys
from datetime import datetime
from time import monotonic

from app.agents.requirement_interpreter import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    RequirementInterpreter,
)
from app.models.planning_v4.input import TripBudget, TripRequest
from app.agents.execution import PlanningError


def shanghai_request():
    """与 docs/planning-design.html#data-step-1 中的固定请求一致。"""
    return TripRequest(
        request_id="DEMO_REQ",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        preferences=("城市漫步", "美食"),
        transport_preferences=("transit", "walking"),
        arrival_place_query=None,
        departure_place_query=None,
        lodging_query=None,
        budget=TripBudget(amount=None, currency="CNY", scope="whole_trip", party_basis="unspecified"),
        remarks="想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中，尽量少折返；安排顺路午餐和晚餐。",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="单独运行 F01 上海三日需求整理")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"每次模型HTTP调用超时，范围为(0, {MAX_TIMEOUT_SECONDS:g}]秒，默认{DEFAULT_TIMEOUT_SECONDS:g}秒",
    )
    args = parser.parse_args(argv)
    started_at = monotonic()

    try:
        # get_llm() 输出已有的初始化横幅；CLI 的 stdout 必须只包含机器可读 JSON。
        with redirect_stdout(io.StringIO()):
            interpreter = RequirementInterpreter(timeout_seconds=args.timeout_seconds)
            summary = interpreter.interpret(shanghai_request())
        print(json.dumps({
            "requirements": summary.model_dump(mode="json"),
            "usage": interpreter.last_usage,
            "model_calls": interpreter.model_calls,
            "elapsed_seconds": round(monotonic() - started_at, 3),
        }, ensure_ascii=False, indent=2))
        return 0
    except PlanningError as exc:
        print(json.dumps({"error": {"code": exc.code, "step": exc.step}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except ValueError:
        print(json.dumps({"error": {"code": "INVALID_TIMEOUT", "step": "cli_arguments"}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "REQUIREMENT_RUN_FAILED", "step": "requirement_interpretation"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
