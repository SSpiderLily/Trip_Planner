"""旅行规划API路由"""

import asyncio
from fastapi import APIRouter, HTTPException, Request, Response, Query, Header
from urllib.parse import urlsplit
from ...services.task_service import TaskError
from ...models.planning_v4 import TripRequest as PlanningV4TripRequest
from ...models.planning_v4_preview import (
    DestinationSelectionRequest,
    PreviewEditJobResponse,
    PreviewEditRequest,
    PreviewEditResponse,
    PreviewEnvelope,
    PreviewFinalRestoreRequest,
    PreviewFinalSaveRequest,
    PreviewFinalSaveResponse,
    PreviewFinalState,
    PreviewRestoreResponse,
    PreviewTaskAccepted,
    PreviewTaskStatus,
)
from ...models.planning_v4.destination_search import DestinationSearchRequest, DestinationSearchResponse
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


def _v4_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "PREVIEW_ACCESS_REQUIRED", "message": "需要预览访问标识"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail={"code": "PREVIEW_ACCESS_REQUIRED", "message": "需要预览访问标识"})
    return token


@router.post("/v4/tasks", status_code=202, response_model=PreviewTaskAccepted)
async def create_v4_preview(body: PlanningV4TripRequest, request: Request):
    try:
        return await request.app.state.preview_task_service.submit(body)
    except TaskError as exc:
        raise task_http(exc)


@router.post("/v4/tasks/{task_id}/recover-local-access")
async def recover_v4_local_access(task_id: str, request: Request, response: Response):
    # Host + 客户端地址阻止远程/代理访问；自定义头及 Origin 阻止跨站表单和 DNS rebinding。
    loopback = {"127.0.0.1", "::1", "localhost"}
    origin = request.headers.get("origin")
    parsed = urlsplit(origin) if origin else None
    allowed_origin = origin is None or (
        parsed.scheme == "http" and parsed.hostname in loopback
        and parsed.netloc in {f"{host}:{port}" for host in ("localhost", "127.0.0.1", "[::1]") for port in (5173, 3000, 8000)}
        and not parsed.path and not parsed.query and not parsed.fragment
    )
    if (request.client is None or request.client.host not in loopback
        or request.url.hostname not in loopback or not allowed_origin
        or request.headers.get("x-trip-local-recovery") != "1"):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_RECOVERY_ONLY", "message": "仅允许本机恢复预览访问"})
    try:
        recovered = await asyncio.to_thread(
            request.app.state.preview_task_service.repository.recover_local_access, task_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "PREVIEW_STORAGE_UNAVAILABLE", "message": "暂时无法恢复预览访问"}) from exc
    if recovered is None:
        raise HTTPException(status_code=409, detail={"code": "PREVIEW_NOT_RECOVERABLE", "message": "任务没有已完成的预览，或仍在更新，请稍后重试"})
    response.headers["Cache-Control"] = "no-store"
    return recovered


@router.get("/v4/tasks/{task_id}", response_model=PreviewTaskStatus)
async def v4_preview_status(task_id: str, request: Request, authorization: str | None = Header(default=None)):
    token = _v4_bearer(authorization)
    try:
        record = await request.app.state.preview_task_service.status(task_id, token)
    except TaskError as exc:
        raise task_http(exc)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在"})
    return record


@router.get("/v4/tasks/{task_id}/preview", response_model=PreviewEnvelope)
async def v4_preview(task_id: str, request: Request, authorization: str | None = Header(default=None)):
    token = _v4_bearer(authorization)
    try:
        record = await request.app.state.preview_task_service.status(task_id, token)
    except TaskError as exc:
        raise task_http(exc)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在"})
    if not record["preview_ready"]:
        raise HTTPException(status_code=409, detail={"code": "PREVIEW_NOT_READY", "message": "预览尚未就绪"})
    try:
        payload = await request.app.state.preview_task_service.preview(task_id, token)
    except TaskError as exc:
        raise task_http(exc)
    if payload is None:
        raise HTTPException(status_code=503, detail={"code": "PREVIEW_UNAVAILABLE", "message": "预览暂不可用"})
    return payload


@router.post(
    "/v4/tasks/{task_id}/destinations/search",
    response_model=DestinationSearchResponse,
)
async def v4_destination_search(
    task_id: str,
    body: DestinationSearchRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    searcher = getattr(request.app.state, "destination_search_service", None)
    if searcher is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "DESTINATION_SEARCH_UNAVAILABLE", "message": "目的地搜索暂不可用"},
        )
    try:
        envelope = await request.app.state.preview_task_service.preview(task_id, token)
    except TaskError as exc:
        raise task_http(exc)
    if envelope is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "PREVIEW_NOT_READY", "message": "预览尚未就绪"},
        )
    try:
        return await searcher.search_async(
            task_id=task_id,
            plan_id=envelope.plan_id,
            access_token=token,
            city=envelope.preview.planning_input.request.city,
            request=body,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "DESTINATION_SEARCH_INVALID", "message": str(exc)},
        ) from exc


@router.post(
    "/v4/tasks/{task_id}/destinations/select",
    status_code=202,
    response_model=PreviewEditResponse,
)
async def v4_destination_select(
    task_id: str,
    body: DestinationSelectionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    editor = getattr(request.app.state, "preview_edit_service", None)
    if editor is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "DESTINATION_SELECT_UNAVAILABLE", "message": "目的地选择暂不可用"},
        )
    try:
        return await editor.add_destination_candidate(task_id, token, body)
    except TaskError as exc:
        raise task_http(exc)


@router.post(
    "/v4/tasks/{task_id}/edits",
    status_code=202,
    response_model=PreviewEditResponse,
)
async def v4_edit_preview(
    task_id: str,
    body: PreviewEditRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    editor = getattr(request.app.state, "preview_edit_service", None)
    if editor is None:
        raise HTTPException(status_code=503, detail={"code": "EDIT_UNAVAILABLE", "message": "预览编辑暂不可用"})
    try:
        return await editor.edit(
            task_id,
            token,
            body.command,
            expected_store_version=body.expected_store_version,
        )
    except TaskError as exc:
        raise task_http(exc)


@router.get(
    "/v4/tasks/{task_id}/edit-jobs/{calculation_id}",
    response_model=PreviewEditJobResponse,
)
async def v4_edit_job(
    task_id: str,
    calculation_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    editor = getattr(request.app.state, "preview_edit_service", None)
    if editor is None:
        raise HTTPException(status_code=503, detail={"code": "EDIT_UNAVAILABLE", "message": "预览编辑暂不可用"})
    try:
        return await editor.get_job(task_id, token, calculation_id)
    except TaskError as exc:
        raise task_http(exc)


@router.get("/v4/tasks/{task_id}/final", response_model=PreviewFinalState)
async def v4_final_state(task_id: str, request: Request, authorization: str | None = Header(default=None)):
    token = _v4_bearer(authorization)
    try:
        return await request.app.state.preview_task_service.final_state(task_id, token)
    except TaskError as exc:
        raise task_http(exc)


@router.post("/v4/tasks/{task_id}/final/save", response_model=PreviewFinalSaveResponse)
async def v4_save_final(
    task_id: str,
    body: PreviewFinalSaveRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    try:
        return await request.app.state.preview_task_service.save_final(
            task_id,
            token,
            save_id=body.save_id,
            source_store_version=body.source_store_version,
        )
    except TaskError as exc:
        raise task_http(exc)


@router.post("/v4/tasks/{task_id}/final/restore", response_model=PreviewRestoreResponse)
async def v4_restore_final(
    task_id: str,
    body: PreviewFinalRestoreRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = _v4_bearer(authorization)
    try:
        return await request.app.state.preview_task_service.restore_final(
            task_id,
            token,
            restore_id=body.restore_id,
            expected_store_version=body.expected_store_version,
            expected_plan_revision=body.expected_plan_revision,
            expected_save_id=body.expected_save_id,
        )
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
