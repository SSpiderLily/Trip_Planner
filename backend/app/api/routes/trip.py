"""旅行规划API路由"""

import asyncio
from fastapi import APIRouter, HTTPException, Request, Query
from ...services.task_service import TaskError
from starlette.concurrency import run_in_threadpool
from ...models.schemas import (
    TripRequest, RecalculateDayRequest
)
from ...agents.trip_planner_agent import get_trip_planner_agent
from ...services.day_edit_service import DayEditError
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/trip", tags=["旅行规划"])


def service(request: Request):
    return request.app.state.task_service


def task_http(exc):
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc), **exc.details})


@router.post("/tasks", status_code=202)
async def create_task(body: TripRequest, request: Request):
    if body.arrival_at is None or body.departure_at is None:
        raise HTTPException(status_code=422, detail={"code": "TRAVEL_TIMES_REQUIRED", "message": "请选择到达和离开时间"})
    try:
        return await service(request).submit(body)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/tasks")
async def list_tasks(request: Request, status: str | None = Query(None, pattern="^(accepted|running|succeeded|failed|interrupted)$"),
                     limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    try:
        return await service(request).list_tasks(status, limit, offset)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/tasks/{task_id}")
async def task_status(task_id: str, request: Request):
    try:
        return await service(request).status(task_id)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/tasks/{task_id}/result")
async def task_result(task_id: str, request: Request):
    try:
        result = await service(request).result(task_id)
        editor = getattr(request.app.state, 'day_edit_service', None)
        if editor is not None:
            result = editor.attach_tokens(task_id, result)
        return {"success": True, "data": result}
    except TaskError as exc:
        raise task_http(exc)


@router.post("/recalculate-day")
async def recalculate_day(body: RecalculateDayRequest, request: Request):
    editor = getattr(request.app.state, 'day_edit_service', None)
    if editor is None:
        raise HTTPException(status_code=503, detail={"code": "EDIT_UNAVAILABLE", "message": "当天行程编辑暂不可用"})
    try:
        return await run_in_threadpool(editor.recalculate, body)
    except DayEditError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/tasks/{task_id}/metrics")
async def task_metrics(task_id: str, request: Request):
    try:
        return await service(request).metrics(task_id)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/tasks/{task_id}/spans")
async def task_spans(task_id: str, request: Request):
    try:
        await service(request).status(task_id)
        return await asyncio.to_thread(service(request).repository.spans, task_id)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/tasks/{task_id}/spans/{span_id}")
async def span_detail(task_id: str, span_id: str, request: Request):
    row = await asyncio.to_thread(service(request).repository.span, task_id, span_id)
    if row is None:
        raise HTTPException(404, detail={"code":"SPAN_NOT_FOUND", "message":"步骤记录不存在"})
    return row


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        # 规划器首次初始化会同步创建 LLM 和 MCP 工具，放入线程池避免阻塞事件循环。
        planner = await run_in_threadpool(get_trip_planner_agent)

        return {
            "status": "healthy",
            "service": "trip-planner",
            "agent_name": "多智能体旅行规划系统",
            "tools_count": len(planner.amap_tools),
            "agents": [agent.name for agent in planner.agents],
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )
