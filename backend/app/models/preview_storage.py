"""独立预览仓库的读写结果与冲突类型。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .planning_v4.plan import ItineraryPreview


@dataclass(frozen=True)
class StoredPreview:
    preview: ItineraryPreview
    store_version: int
    plan_revision: int
    updated_at: str


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
class SavedPreview:
    preview: ItineraryPreview
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


class IdempotencyConflict(PreviewRepositoryError):
    """同一幂等 ID 被用于不同的请求内容。"""
