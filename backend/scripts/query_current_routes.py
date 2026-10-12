"""只对输入快照中指定的一个相邻路段执行 F08 真实路线查询。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from app.models.planning_v4 import FactStore, PlanDraft, PlanningInput
from app.services.current_route_service import CurrentRouteError, query_current_routes
from app.services.endpoint_verification_service import EndpointVerificationExecution


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 JSON 输入快照读取 PlanningInput、PlanDraft 与 FactStore，只查询用户指定的一个当前路段。"
        ),
    )
    parser.add_argument("--input-file", required=True, type=Path, help="含规划输入、草稿和事实的 UTF-8 JSON 文件")
    parser.add_argument("--day-id", required=True, help="目标日期的 day_id")
    parser.add_argument("--from-ref", required=True, help="RouteLeg 起点 reference_id")
    parser.add_argument("--to-ref", required=True, help="RouteLeg 终点 reference_id")
    parser.add_argument("--live", action="store_true", help="明确启用真实地图工具调用")
    return parser


def _result_report(result) -> dict:
    route_facts = {fact.fact_id: fact for fact in result.fact_store.routes}
    return {
        "plan_id": result.plan_id,
        "request_id": result.request_id,
        "based_on_revision": result.based_on_revision,
        "status": result.status,
        "leg_results": [item.model_dump(mode="json") for item in result.leg_results],
        "legs": [
            {
                "leg_id": leg.leg_id,
                "plan_revision": leg.plan_revision,
                "day_id": leg.day_id,
                "from_endpoint": leg.from_endpoint.model_dump(mode="json"),
                "to_endpoint": leg.to_endpoint.model_dump(mode="json"),
                "requested_departure_at": (
                    leg.requested_departure_at.isoformat() if leg.requested_departure_at else None
                ),
                "time_basis": leg.time_basis,
                "selected_mode": leg.selected_mode,
                "selected_option_id": leg.selected_option_id,
                "selection_source": leg.selection_source,
                "mode_results": [item.model_dump(mode="json") for item in leg.mode_results],
            }
            for leg in result.draft.legs
        ],
        "routes": [
            {
                "fact_id": fact.fact_id,
                "mode": fact.mode,
                "status": fact.status,
                "requested_departure_at": (
                    fact.requested_departure_at.isoformat() if fact.requested_departure_at else None
                ),
                "time_verification": fact.time_verification,
                "effective_params": fact.source.effective_params,
                "options": [
                    {
                        "option_id": option.option_id,
                        "duration_s": option.duration_s,
                        "distance_m": option.distance_m,
                        "geometry_status": option.geometry_status,
                    }
                    for option in fact.options
                ],
                "error_category": fact.error_category,
            }
            for fact in route_facts.values()
        ],
    }


async def _run(payload: dict, args: argparse.Namespace) -> dict:
    planning_input = PlanningInput.model_validate(payload["planning_input"])
    draft = PlanDraft.model_validate(payload["draft"])
    facts = FactStore.model_validate(payload["facts"])
    endpoint_raw = payload.get("endpoint_execution")
    endpoint_execution = (
        EndpointVerificationExecution.model_validate(endpoint_raw)
        if endpoint_raw is not None else None
    )
    result = await query_current_routes(
        planning_input,
        draft,
        facts,
        endpoint_execution=endpoint_execution,
        verified_departure_fact_id=payload.get("verified_departure_fact_id"),
        current_context_revision=payload.get("current_context_revision"),
        only_edge=(args.day_id, args.from_ref, args.to_ref),
    )
    return _result_report(result)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.live:
        print("请同时传入 --live；此命令只用于显式真实路线查询。", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.input_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be an object")
        report = asyncio.run(_run(payload, args))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, CurrentRouteError) else "input_or_route_contract_invalid"
        print(json.dumps({"error": code}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
