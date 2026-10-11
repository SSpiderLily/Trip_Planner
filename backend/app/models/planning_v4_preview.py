"""Persisted preview envelope returned by the read-only v4 API."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, PositiveInt, StrictInt, model_validator

from .planning_v4.base import ContractModel
from .planning_v4.cost import ReferenceCostSummary
from .planning_v4.plan import ItineraryPreview
from ..services.planning_v4_timing import PlanTimingResult


class PreviewSnapshot(ContractModel):
    """One consistent draft plus the typed calculations derived from it."""

    preview: ItineraryPreview
    timing: PlanTimingResult
    cost: ReferenceCostSummary

    @model_validator(mode="after")
    def validate_bindings(self):
        plan_id = self.preview.draft.plan_id
        revision = self.preview.draft.revision
        request_id = self.preview.draft.request_id
        if (self.timing.plan_id, self.timing.based_on_revision) != (plan_id, revision):
            raise ValueError("活动时间必须引用当前计划版本")
        if (self.cost.plan_id, self.cost.request_id, self.cost.based_on_revision) != (
            plan_id, request_id, revision
        ):
            raise ValueError("消费汇总必须引用当前计划版本")
        expected_days = {day.day_id: day.day_date for day in self.preview.draft.days}
        timing_ids = tuple(day.day_id for day in self.timing.days)
        timing_summary_ids = tuple(day.summary.day_id for day in self.timing.days)
        cost_days = tuple((day.day_id, day.day_date) for day in self.cost.days)
        if len(expected_days) != len(self.preview.draft.days):
            raise ValueError("预览日期集合不能重复")
        if (
            len(timing_ids) != len(expected_days)
            or set(timing_ids) != set(expected_days)
            or timing_summary_ids != timing_ids
        ):
            raise ValueError("活动时间日期集合必须与预览一致")
        expected_cost_days = set(expected_days.items())
        if len(cost_days) != len(expected_days) or set(cost_days) != expected_cost_days:
            raise ValueError("消费汇总日期集合必须与预览一致")
        return self


class PreviewEnvelope(PreviewSnapshot):
    """任务读取接口的持久化快照及 CAS 版本。"""

    task_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    store_version: PositiveInt
    plan_revision: PositiveInt

    @model_validator(mode="after")
    def validate_envelope(self):
        if self.plan_id != self.preview.draft.plan_id:
            raise ValueError("preview envelope 的 plan_id 不匹配")
        if self.plan_revision != self.preview.draft.revision:
            raise ValueError("preview envelope 的 plan_revision 不匹配")
        return self


class PreviewTaskAccepted(ContractModel):
    task_id: str
    plan_id: str
    access_token: str
    status: Literal["accepted"]


class PreviewFinalMetadata(ContractModel):
    """最近一次成功最终保存的元数据；不代表历史列表。"""

    save_id: str
    saved_at: str
    source_store_version: PositiveInt
    plan_revision: PositiveInt


class PreviewFinalState(ContractModel):
    task_id: str
    plan_id: str
    restorable: bool
    final: PreviewFinalMetadata | None = None
    unavailable_reason: Literal["snapshot_incomplete"] | None = None

    @model_validator(mode="after")
    def validate_final_state(self):
        if self.restorable != (self.final is not None and self.unavailable_reason is None):
            raise ValueError("restorable 必须与最终结果状态一致")
        if self.unavailable_reason is not None and self.final is None:
            raise ValueError("最终结果不可用原因必须对应已保存记录")
        return self


class PreviewFinalSaveRequest(ContractModel):
    save_id: str = Field(min_length=1, max_length=256)
    source_store_version: StrictInt = Field(gt=0)


class PreviewFinalSaveResponse(ContractModel):
    task_id: str
    plan_id: str
    status: Literal["saved", "already_saved", "stale"]
    replayed: bool
    current_store_version: PositiveInt
    receipt_store_version: PositiveInt
    restorable: bool
    final: PreviewFinalMetadata | None = None
    unavailable_reason: Literal["snapshot_incomplete"] | None = None

    @model_validator(mode="after")
    def validate_final_state(self):
        if self.restorable != (self.final is not None and self.unavailable_reason is None):
            raise ValueError("restorable 必须与最终结果状态一致")
        if self.unavailable_reason is not None and self.final is None:
            raise ValueError("最终结果不可用原因必须对应已保存记录")
        return self


class PreviewFinalRestoreRequest(ContractModel):
    restore_id: str = Field(min_length=1, max_length=256)
    expected_store_version: StrictInt = Field(gt=0)
    expected_plan_revision: StrictInt = Field(gt=0)
    expected_save_id: str = Field(min_length=1, max_length=256)


class PreviewRestoreResponse(ContractModel):
    envelope: PreviewEnvelope
    restored_from: PreviewFinalMetadata
    replayed: bool
    receipt_store_version: PositiveInt


class BackfillTaskProgress(ContractModel):
    status: Literal["not_started", "running", "completed", "failed", "interrupted"]
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


class PreviewTaskStatus(ContractModel):
    task_id: str
    plan_id: str
    status: Literal["accepted", "running", "ready", "failed", "interrupted"]
    stage: Literal[
        "accepted", "planning_input", "dispatch", "proposal", "current_routes",
        "timing", "validation", "repair", "cost", "ready", "failed", "interrupted",
    ]
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    message: str | None = None
    backfill: BackfillTaskProgress
    preview_ready: bool
    store_version: PositiveInt | None = None
    error_code: str | None = None
    updated_at: str
