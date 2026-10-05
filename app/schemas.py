"""API 请求/响应模式。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _finite(v: float, name: str) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{name} 必须为数值")
    if not math.isfinite(float(v)):
        raise ValueError(f"{name} 必须为有限数值")
    return float(v)


class Point(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _chk(cls, v: float) -> float:
        return _finite(v, "坐标")


class ZoneCreate(BaseModel):
    """登记新禁区。zone_no 省略时自动分配。"""
    zone_no: int | None = Field(default=None, ge=1)
    polygon: list[tuple[float, float]] = Field(min_length=3)
    z_min: float
    z_max: float
    description: str | None = Field(default=None, max_length=500)

    @field_validator("z_min", "z_max")
    @classmethod
    def _z(cls, v: float) -> float:
        return _finite(v, "Z 高度")


class ZoneVersionCreate(BaseModel):
    """登记某禁区的新版本（几何不可变修改 => 新版本）。"""
    polygon: list[tuple[float, float]] = Field(min_length=3)
    z_min: float
    z_max: float
    description: str | None = Field(default=None, max_length=500)

    @field_validator("z_min", "z_max")
    @classmethod
    def _z(cls, v: float) -> float:
        return _finite(v, "Z 高度")


class ZoneVersionOut(BaseModel):
    version_id: int
    version: int
    polygon: list[tuple[float, float]]
    z_min: float
    z_max: float
    created_at: datetime


class ZoneOut(BaseModel):
    zone_id: int
    zone_no: int
    description: str | None
    current_version: int
    created_at: datetime


class ZoneDetailOut(ZoneOut):
    versions: list[ZoneVersionOut]


class ZoneRef(BaseModel):
    """审查引用：禁区编号 + 明确版本号。"""
    zone_no: int = Field(ge=1)
    version: int = Field(ge=1)


class ReviewRequest(BaseModel):
    zones: list[ZoneRef] = Field(min_length=1)
    start: tuple[float, float, float]
    tool_radius: float = Field(ge=0.0)
    gcode: str = Field(min_length=1)
    # 可选：启用后计算全程最小水平净间隙并随报告固化
    include_min_clearance: bool = False

    @field_validator("include_min_clearance", mode="before")
    @classmethod
    def _strict_bool(cls, v):
        # 仅接受真正的布尔值；拒绝 "true"/1 等隐式转换，属非法输入（422）
        if not isinstance(v, bool):
            raise ValueError("include_min_clearance 必须为布尔值 true/false")
        return v

    @field_validator("tool_radius")
    @classmethod
    def _radius(cls, v: float) -> float:
        return _finite(v, "刀具半径")

    @field_validator("start")
    @classmethod
    def _start(cls, v: tuple[float, float]) -> tuple[float, float, float]:
        if len(v) != 3:
            raise ValueError("初始刀尖坐标必须为 [x, y, z]")
        for c in v:
            _finite(c, "初始刀尖坐标")
        return tuple(float(c) for c in v)  # type: ignore[return-value]


class MinClearanceOut(BaseModel):
    """全程最小水平净间隙结果（毫米；净间隙可为负）。"""
    min_clearance_mm: float
    line_no: int | None
    point_mm: tuple[float, float, float]
    zone_no: int
    progress: float = Field(description="取得最小值处的段内进度（直线 t / 圆弧 s）")


class ReviewResponse(BaseModel):
    report_id: int
    used_zone_versions: dict[str, int]
    input_summary: dict[str, Any]
    has_collision: bool
    collision_line_no: int | None
    collision_zone_no: int | None
    conclusion: Literal["collision", "no_collision"]
    message: str
    # 仅当请求 include_min_clearance=true 时非空；全程不在任何禁区高度内时为 null
    min_clearance: MinClearanceOut | None = None
    min_clearance_enabled: bool = False
