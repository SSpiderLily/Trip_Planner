"""v4 规划契约共用的严格基础类型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


ParamScalar = str | int | float | bool | None
Provider = Literal["amap"]
CoordinateSystem = Literal["GCJ-02"]
