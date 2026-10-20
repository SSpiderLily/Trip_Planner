"""规划运行身份和进度契约；不依赖任务存储、API或具体规划实现。"""
from __future__ import annotations

from typing import Awaitable, Callable, Literal

from pydantic import Field, StrictStr, field_validator

from app.models.planning_v4.base import ContractModel


class PlanningRunIdentity(ContractModel):
    task_id: StrictStr = Field(min_length=1, max_length=128)
    plan_id: StrictStr = Field(min_length=1, max_length=128)

    @field_validator("task_id", "plan_id")
    @classmethod
    def require_exact_identifier(cls, value):
        if value != value.strip():
            raise ValueError("运行标识不得为空或包含首尾空白")
        return value


class PreviewProgress(ContractModel):
    # 沿用正式预览的阶段和值，原生地点规划不自行报告cost/ready/current_routes。
    stage: Literal[
        "planning_input", "dispatch", "proposal", "current_routes",
        "validation", "repair", "cost", "ready",
    ]
    completed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    message: str | None = None


ProgressCallback = Callable[[PreviewProgress], Awaitable[None]]
