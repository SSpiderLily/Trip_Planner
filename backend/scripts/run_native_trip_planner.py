"""第四步真实规划与按日修订；输出脱敏JSONL，默认使用公开地点的合成请求。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryFile

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.native_trip_planner import NativeTripPlanner
from app.config import get_settings
from app.models.planning_v4.input import TripRequest
from app.services.llm_service import get_agent_llm
from app.services.native_chain_trace import NativeChainTrace, TracedNativeModel
from app.services.native_tool_loop import NativeToolLoopError


def sample_request():
    return TripRequest(
        request_id="native-planning-shanghai-2026-10-12", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-21T15:00:00+08:00"),
        preferences=("城市漫步", "历史建筑", "本帮菜"),
        remarks="必须去外滩；景点偏好城市漫步和历史建筑，餐饮偏好本帮菜。请为两个日期的可用时段安排真实景点和餐馆，并在活动区域附近推荐一家酒店。不要无理由跨天重复同一景点，价格或开放时间未知请保留未知。",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="运行真实主模型、子模型和高德MCP")
    parser.add_argument("--request-file", type=Path, help="可选TripRequest JSON；日志会包含脱敏需求字段")
    args = parser.parse_args()
    settings = get_settings()
    output = sys.stdout
    trace = NativeChainTrace(
        sink=lambda row: print(json.dumps(row, ensure_ascii=False), file=output, flush=True),
        secrets=(settings.llm_api_key, settings.openai_api_key, settings.amap_api_key),
    )
    try:
        request = TripRequest.model_validate_json(args.request_file.read_text()) if args.request_file else sample_request()
        # 第三方初始化及协议诊断不直接输出；只将白名单阶段内容写入验收日志。
        # MCP的stdio传输需要stderr.fileno()；StringIO会导致子进程连接失败。
        with TemporaryFile(mode="w+", encoding="utf-8") as errors, \
                redirect_stdout(io.StringIO()), redirect_stderr(errors):
            master = TracedNativeModel(get_agent_llm("master"), "master", trace)
            children = {role: TracedNativeModel(get_agent_llm(role), role, trace)
                        for role in ("attractions", "dining", "lodging")}
            result = NativeTripPlanner(master_llm=master, attraction_llm=children["attractions"],
                dining_llm=children["dining"], lodging_llm=children["lodging"], emit=trace.emit).run(request)
        places = {place.fact_id: place for place in result.fact_store.places}
        trace.emit("planner.result", {
            "status": result.repair.status, "run_id": result.collection.run_id,
            "master_model_calls": master.requests,
            "child_model_calls": {role: model.requests for role, model in children.items()},
            "initial_revision": result.initial_draft.revision, "revision": result.draft.revision,
            "days": [{"date": day.day_date.isoformat(), "theme": day.theme,
                "activities": [{"kind": activity.kind, "slot_id": activity.slot_id,
                    "poi_id": activity.poi_id, "name": places[activity.place_fact_id].name if activity.place_fact_id else None,
                    "reason": activity.recommendation_reason} for activity in day.activities],
                "unfilled_slot_ids": list(day.unfilled_slot_ids),
                "lodging": places[day.lodging.place_fact_id].name if day.lodging and day.lodging.place_fact_id else None}
                for day in result.draft.days],
            "issues": [issue.model_dump(mode="json") for issue in result.report.issues],
            "repair_states": [state.model_dump(mode="json") for state in result.repair.repair_states],
            "fact_count": len(result.fact_store.places), "source_count": len(result.fact_store.sources),
            "routes_status": "not_queried", "storage_status": "not_saved",
        })
        return 0 if result.repair.status == "complete" else 2
    except Exception as exc:
        trace.emit("collection.failed", {
            "code": exc.code if isinstance(exc, NativeToolLoopError) else "COLLECTION_FAILED",
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
