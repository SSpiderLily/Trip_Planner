"""独立预览仓库的读写结果与冲突类型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .planning_v4.plan import ItineraryPreview
from .planning_v4.cost import ReferenceCostSummary
from .planning_v4_preview import PreviewSnapshot
from ..services.planning_v4_timing import PlanTimingResult


@dataclass(frozen=True)
class StoredPreview:
    preview: ItineraryPreview
    store_version: int
    plan_revision: int
    updated_at: str
    timing: PlanTimingResult | None = None
    cost: ReferenceCostSummary | None = None

    @property
    def snapshot(self) -> PreviewSnapshot | None:
        if self.timing is None or self.cost is None:
            return None
        return PreviewSnapshot(preview=self.preview, timing=self.timing, cost=self.cost)


@dataclass(frozen=True)
class CreatedPreview:
    access_token: str
    record: StoredPreview


@dataclass(frozen=True)
class DraftWriteResult:
    # 重放时 record 是当前快照；receipt_store_version 指向原命令完成时的版本。
    record: StoredPreview
    receipt_store_version: int
    replayed: bool


@dataclass(frozen=True)
class PreviewEditWriteResult:
    record: StoredPreview
    receipt_store_version: int
    replayed: bool
    calculation_id: str
    changed_activity_ids: tuple[str, ...]
    changed_leg_ids: tuple[str, ...]
    route_job: PreviewRouteJob | None = None


@dataclass(frozen=True)
class PreviewRouteJob:
    task_id: str
    plan_id: str
    calculation_id: str
    status: Literal["pending", "running", "completed", "failed", "superseded"]
    source_store_version: int
    source_plan_revision: int
    affected_day_ids: tuple[str, ...]
    result_store_version: int | None
    error_code: str | None
    updated_at: str


@dataclass(frozen=True)
class ClaimedPreviewRouteJob:
    job: PreviewRouteJob
    record: StoredPreview


@dataclass(frozen=True)
class SavedPreview:
    preview: ItineraryPreview
    plan_revision: int
    source_store_version: int
    save_id: str
    saved_at: str
    timing: PlanTimingResult | None = None
    cost: ReferenceCostSummary | None = None

    @property
    def snapshot(self) -> PreviewSnapshot | None:
        if self.timing is None or self.cost is None:
            return None
        return PreviewSnapshot(preview=self.preview, timing=self.timing, cost=self.cost)


@dataclass(frozen=True)
class SavedPreviewMetadata:
    plan_revision: int
    source_store_version: int
    save_id: str
    saved_at: str


@dataclass(frozen=True)
class FinalSaveResult:
    status: Literal["saved", "already_saved", "stale"]
    # stale 或重放时仍返回当前最近成功保存的结果（如果存在）。
    final: SavedPreview | None
    current_store_version: int
    receipt_store_version: int
    replayed: bool


@dataclass(frozen=True)
class PreviewRestoreResult:
    record: StoredPreview
    restored_from: SavedPreviewMetadata
    receipt_store_version: int
    replayed: bool


class PreviewRepositoryError(Exception):
    """预览仓库操作错误基类。"""


class PreviewAccessError(PreviewRepositoryError):
    """访问标识无效，或其对应的计划与写入快照不匹配。"""


class PreviewConflict(PreviewRepositoryError):
    """当前草稿版本与调用方的比较并交换前置条件不匹配。"""

    def __init__(self, message: str, *, current_store_version: int, current_plan_revision: int):
        super().__init__(message)
        self.current_store_version = current_store_version
        self.current_plan_revision = current_plan_revision


class PreviewIdentityConflict(PreviewRepositoryError):
    """快照试图改变不可变的计划、需求、运行或规则身份。"""


class PreviewFinalUnavailable(PreviewRepositoryError):
    """最近一次成功保存不存在或缺少可完整还原的计算快照。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PreviewFinalChanged(PreviewRepositoryError):
    """用户读取最终结果后，该最终结果已被另一份导出替换。"""

    def __init__(self, latest: SavedPreviewMetadata):
        super().__init__("最近一次成功保存已变化")
        self.latest = latest


class IdempotencyConflict(PreviewRepositoryError):
    """同一幂等 ID 被用于不同的请求内容。"""
