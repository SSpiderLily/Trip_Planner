"""显式核实用户填写的端点；无 query 时零地图工具请求。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import sys
from uuid import uuid4

from app.agents.execution import PlanningError
from app.models.planning_v4.input import (
    ReferenceWindow,
    RequirementSummary,
    RuleSet,
    TripBudget,
    TripRequest,
)
from app.services.endpoint_verification_service import (
    EndpointVerificationContext,
    EndpointVerificationService,
)
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input


WINDOWS = (
    ReferenceWindow(period="breakfast", start_minute=480, end_minute=540),
    ReferenceWindow(period="morning", start_minute=540, end_minute=720),
    ReferenceWindow(period="lunch", start_minute=720, end_minute=840),
    ReferenceWindow(period="afternoon", start_minute=840, end_minute=1020),
    ReferenceWindow(period="dinner", start_minute=1020, end_minute=1140),
    ReferenceWindow(period="evening", start_minute=1140, end_minute=1320),
)


def shanghai_endpoint_context(
    *, arrival_query: str | None = None, departure_query: str | None = None,
) -> EndpointVerificationContext:
    """构造固定上海三日样例；端点仅来自显式传入的 query。"""
    request = TripRequest(
        request_id="DEMO_REQ",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        preferences=("城市漫步", "美食"),
        transport_preferences=("transit", "walking"),
        arrival_place_query=arrival_query,
        departure_place_query=departure_query,
        lodging_query=None,
        budget=TripBudget(amount=None, currency="CNY", scope="whole_trip", party_basis="unspecified"),
        remarks="想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中；安排顺路午餐和晚餐。",
    )
    planning_input = build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=RequirementSummary(
            requirements_id="F04D_CLI",
            request_id=request.request_id,
        ),
        rules=RuleSet(rule_version="f04d-cli-v1", windows=WINDOWS),
    ))
    return EndpointVerificationContext.from_planning_input(
        planning_input,
        run_id=f"endpoint-live-{uuid4().hex}",
        context_revision=1,
        base_revision=None,
    )


def _source_summary(result):
    return {
        "tool_call_id": result.source.tool_call_id,
        "tool_name": result.source.tool_name,
        "api_version": result.source.api_version,
        "queried_at": result.source.queried_at.isoformat(),
    }


def _endpoint_report(endpoint):
    results = []
    if endpoint.search_result is not None:
        results.append({
            "kind": "search",
            "status": endpoint.search_result.status,
            "error_category": endpoint.search_result.error.category if endpoint.search_result.error else None,
            "source": _source_summary(endpoint.search_result),
        })
    results.extend({
        "kind": "detail",
        "status": item.status,
        "error_category": item.error.category if item.error else None,
        "source": _source_summary(item),
    } for item in endpoint.detail_results)
    return {
        "kind": endpoint.kind,
        "user_query": endpoint.user_query,
        "status": endpoint.status,
        "reason": endpoint.reason,
        "fact_id": endpoint.fact_id,
        "candidates": [
            {
                "poi_id": item.poi_id,
                "name": item.name,
                "location": item.location.model_dump(mode="json") if item.location else None,
                "city": item.city,
                "adcode": item.adcode,
            }
            for item in endpoint.candidates
        ],
        "tool_results": results,
    }


def _report(result, *, supplemental: bool) -> dict:
    calls = {
        endpoint.search_result.tool_call_id
        for endpoint in (result.arrival, result.departure)
        if endpoint.search_result is not None
    }
    calls.update(
        item.tool_call_id
        for endpoint in (result.arrival, result.departure)
        for item in endpoint.detail_results
    )
    return {
        "scenario": "supplemental_explicit_endpoint_queries" if supplemental else "original_shanghai_three_day_request",
        "scope_notice": (
            "CLI提供的补充端点核验，不代表原始上海三日请求。"
            if supplemental else "原始上海三日请求的到离端点均为空；本次不发起地图工具请求。"
        ),
        "request_id": result.request_id,
        "run_id": result.run_id,
        "context_revision": result.context_revision,
        "base_revision": result.base_revision,
        "city": result.city,
        "tool_call_count": len(calls),
        "arrival": _endpoint_report(result.arrival),
        "departure": _endpoint_report(result.departure),
        "fact_store": {
            "place_fact_ids": [fact.fact_id for fact in result.fact_store.places],
            "sources": [_source_summary_for_fact(source) for source in result.fact_store.sources],
        },
    }


def _source_summary_for_fact(source):
    return {
        "tool_call_id": source.tool_call_id,
        "tool_name": source.tool_name,
        "api_version": source.api_version,
        "queried_at": source.queried_at.isoformat(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="独立核实上海三日请求中用户填写的到离端点")
    parser.add_argument(
        "--live",
        action="store_true",
        help="确认本次可以执行显式高德端点 MCP 查询；仅 CLI query 非空时产生地图调用",
    )
    parser.add_argument("--arrival-query", help="补充到达地点核验 query；仅用于本次显式 CLI 场景")
    parser.add_argument("--departure-query", help="补充离开地点核验 query；仅用于本次显式 CLI 场景")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"error": {"code": "LIVE_CONFIRMATION_REQUIRED"}}, ensure_ascii=False), file=sys.stderr)
        return 2

    supplemental = bool(args.arrival_query and args.arrival_query.strip()) or bool(
        args.departure_query and args.departure_query.strip()
    )
    try:
        context = shanghai_endpoint_context(
            arrival_query=args.arrival_query,
            departure_query=args.departure_query,
        )
        # MCP 初始化提示不能混入机器可读输出，也不回显环境变量或原始工具响应。
        with redirect_stdout(io.StringIO()):
            result = EndpointVerificationService().run(context)
        print(json.dumps(_report(result, supplemental=supplemental), ensure_ascii=False, indent=2))
        return 0
    except PlanningError as exc:
        print(json.dumps({"error": {"code": exc.code, "step": exc.step}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "ENDPOINT_VERIFICATION_FAILED", "step": "endpoint_verification"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
