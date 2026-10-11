"""用户搜索目的地候选的只读响应模型；选择时只回传服务端 token。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, PositiveInt

from .base import ContractModel, Provider
from .facts import MapCoordinate, MissingField, ReferenceCost, ToolError


DestinationCategory = Literal["sightseeing", "meal"]
DestinationBlockReason = Literal[
    "location_unavailable", "city_mismatch", "category_unverified", "source_unclosed"
]


class DestinationSearchRequest(ContractModel):
    keywords: str = Field(min_length=1, max_length=120)
    category: DestinationCategory
    page: PositiveInt = 1

    @staticmethod
    def normalized_keywords(value: str) -> str:
        return " ".join(value.split())


class DestinationSourceSummary(ContractModel):
    """不包含工具调用 ID 或凭据的可展示来源信息。"""

    provider: Provider
    tool_name: str = Field(min_length=1)
    api_version: Literal["v3", "v4"]
    queried_at: datetime
    effective_params: dict[str, str | int | float | bool | None]


class DestinationSearchCandidate(ContractModel):
    # 搜索列表仅在内存中保存；为恢复已提交请求，可缓存pending请求中的token。
    # 不向用户展示此字段，也不由客户端回传事实或价格。
    candidate_token: str | None = Field(default=None, min_length=32, max_length=128)
    name: str = Field(min_length=1)
    address: str | None = None
    city: str | None = None
    location: MapCoordinate | None = None
    type_name: str | None = None
    cost: ReferenceCost
    opening_hours_raw: str | None = None
    photos: tuple[str, ...] = ()
    missing_fields: tuple[MissingField, ...] = ()
    source: DestinationSourceSummary
    selectable: bool
    selection_block_reason: DestinationBlockReason | None = None


class DestinationSearchResponse(ContractModel):
    search_id: str = Field(min_length=1)
    city: str = Field(min_length=1)
    keywords: str = Field(min_length=1)
    category: DestinationCategory
    page: PositiveInt
    status: Literal["ok", "empty", "error"]
    provider_count: int | None = Field(default=None, ge=0)
    candidates: tuple[DestinationSearchCandidate, ...] = ()
    omitted_count: int = Field(default=0, ge=0)
    error: ToolError | None = None

