"""FastAPI主应用"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from ..services.task_repository import TaskRepository
from ..services.task_service import TaskService
from ..services.observation_service import ObservationService
from ..services.day_edit_service import DayEditService
from ..services.preview_repository import PreviewRepository
from ..services.preview_task_service import PreviewTaskService
from ..services.planning_v4_pipeline import PlanningV4Pipeline
from ..services.destination_candidate_repository import DestinationCandidateRepository
from ..services.destination_search_service import DestinationSearchService

from ..services.planning_v4_preview_edit_service import PlanningV4PreviewEditService
from ..services.current_route_service import CurrentRouteService
from ..agents.trip_planner_agent import get_trip_planner_agent
from fastapi.middleware.cors import CORSMiddleware
from ..config import get_settings, validate_config, print_config
from .routes import trip, poi, map as map_routes

# 获取配置
settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """统一管理任务服务的初始化和退出等待。"""
    await startup_event(application)
    try:
        yield
    finally:
        await shutdown_event(application)


# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于HelloAgents框架的智能旅行规划助手API",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")


async def startup_event(application: FastAPI):
    """应用启动事件"""
    print("\n" + "="*60)
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print("="*60)
    
    # 打印配置信息
    print_config()
    
    # 验证配置
    try:
        validate_config()
        print("\n✅ 配置验证通过")
    except ValueError as e:
        print(f"\n❌ 配置验证失败:\n{e}")
        print("\n请检查.env文件并确保所有必要的配置项都已设置")
        raise
    
    if settings.observation_retention_days < 1 or settings.observation_content_limit < 256 or settings.observation_max_bytes < 4096:
        raise ValueError("观测存储配置必须为有效正数（内容上限至少256字节）")
    task_db = Path(settings.task_db_path).expanduser().resolve()
    preview_db = Path(settings.preview_db_path).expanduser().resolve()
    destination_db = Path(settings.destination_candidate_db_path).expanduser().resolve()
    if task_db == preview_db:
        raise ValueError("preview_db_path 必须与 task_db_path 分开，以隔离预览体积与观测清理")
    if destination_db in {task_db, preview_db}:
        raise ValueError("destination_candidate_db_path 必须与任务观测和预览数据库分开")
    repository = TaskRepository(settings.task_db_path, settings.observation_retention_days, settings.observation_max_bytes)
    await asyncio.to_thread(repository.initialize)
    observation = ObservationService(repository, settings.observation_content_limit)
    application.state.task_service = TaskService(repository, observation, get_trip_planner_agent)
    application.state.day_edit_service = DayEditService(repository)
    destination_repository = DestinationCandidateRepository(
        settings.destination_candidate_db_path,
        ttl_seconds=settings.destination_candidate_ttl_seconds,
    )
    await asyncio.to_thread(destination_repository.initialize)
    await asyncio.to_thread(destination_repository.cleanup_expired)
    application.state.destination_candidate_repository = destination_repository
    destination_search_service = DestinationSearchService(
        destination_repository,
        observation_service=observation,
        tool_timeout_seconds=settings.planner_tool_timeout_seconds,
    )
    application.state.destination_search_service = destination_search_service
    preview_repository = PreviewRepository(settings.preview_db_path)
    await asyncio.to_thread(preview_repository.initialize)
    await asyncio.to_thread(preview_repository.recover_jobs)
    application.state.task_service.preview_completion_resolver = preview_repository.ready_task_completion
    application.state.task_service.preview_completion_batch_resolver = preview_repository.ready_task_completions
    application.state.preview_repository = preview_repository
    pipeline = getattr(application.state, "planning_v4_pipeline", None)
    if pipeline is None:
        pipeline = PlanningV4Pipeline(
            total_timeout_seconds=settings.planner_v4_total_timeout_seconds,
            initial_collection_timeout_seconds=settings.planner_v4_initial_collection_timeout_seconds,
            current_revision_lookup=preview_repository.current_plan_revision,
        )
    application.state.planning_v4_pipeline = pipeline
    application.state.preview_task_service = PreviewTaskService(
        application.state.task_service,
        preview_repository,
        pipeline,
    )
    application.state.preview_edit_service = PlanningV4PreviewEditService(
        application.state.preview_task_service,
        preview_repository,
        route_service=CurrentRouteService(),
        route_deadline_seconds=settings.planner_edit_route_deadline_seconds,
        destination_search_service=destination_search_service,
    )
    application.state.preview_task_service.edit_coordinator = application.state.preview_edit_service
    await application.state.preview_edit_service.recover_pending_jobs()

    print("\n" + "="*60)
    print("📚 API文档: http://localhost:8000/docs")
    print("📖 ReDoc文档: http://localhost:8000/redoc")
    print("="*60 + "\n")


async def shutdown_event(application: FastAPI):
    """应用关闭事件"""
    print("\n" + "="*60)
    print("👋 应用正在关闭...")
    if hasattr(application.state, "preview_edit_service"):
        await application.state.preview_edit_service.close()
    if hasattr(application.state, "preview_task_service"):
        await application.state.preview_task_service.close()
    if hasattr(application.state, "task_service"):
        await application.state.task_service.close()
    print("="*60 + "\n")


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )
