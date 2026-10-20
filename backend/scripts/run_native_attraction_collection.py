"""第二步真实小闭环；输出脱敏JSONL，默认使用公开地点的合成请求。"""
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

from app.agents.native_attraction_collection import NativeAttractionCollection
from app.config import get_settings
from app.models.planning_v4.input import TripRequest
from app.services.llm_service import get_agent_llm
from app.services.native_chain_trace import NativeChainTrace, TracedNativeModel
from app.services.native_tool_loop import NativeToolLoopError


def sample_request():
    return TripRequest(
        request_id="native-attractions-shanghai-2026-10-12", city="上海",
        arrival_at=datetime.fromisoformat("2026-10-20T08:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-21T20:00:00+08:00"),
        preferences=("城市漫步", "公园或历史建筑"),
        remarks="请为这段旅行找2个景点候选。必须去外滩；另一个偏好公园或历史建筑。不要安排餐饮、住宿和每日行程。",
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
            child = TracedNativeModel(get_agent_llm("attractions"), "attractions", trace)
            result = NativeAttractionCollection(master_llm=master, attraction_llm=child,
                                                emit=trace.emit).run(request)
        trace.emit("collection.result", {
            # 脚本验证“候选链路是否返回”，保留每次派发的状态，不把一次已恢复失败当作最终失败。
            # 用户需求是否全面满足仍需检查TaskResult覆盖和主模型总结。
            "status": "candidates_returned" if result.candidates else "no_candidates",
            "child_statuses": [item.task_result.status for item in result.children],
            "run_id": result.run_id, "candidate_count": len(result.candidates),
            "master_model_calls": result.model_calls, "child_model_calls": child.requests,
            "candidates": [candidate.model_dump(mode="json") for candidate in result.candidates],
            "fact_count": len(result.fact_store.places), "source_count": len(result.fact_store.sources),
        })
        return 0 if result.candidates else 2
    except Exception as exc:
        trace.emit("collection.failed", {
            "code": exc.code if isinstance(exc, NativeToolLoopError) else "COLLECTION_FAILED",
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
