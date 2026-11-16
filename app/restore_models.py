"""还原作业的请求/响应入参模型。

业务边界:
  * 一次还原选择 1..8 个非空混池, 涉及至多 8 个样本;
  * 每个混池录入一个非负有理数浓度 c (允许整数/小数/分数字符串);
  * 浓度解析与非负校验在服务层用精确有理数完成 (Pydantic 只做形状校验),
    避免浮点引入误差。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_POOLS = 8
MAX_SAMPLES = 8


class MeasurementInput(BaseModel):
    """单个混池的浓度录入。"""

    pool_no: str
    # 非负有理数的原始字符串, 例如 "3" / "0.25" / "2/3"; 服务层精确解析
    concentration: str

    @field_validator("pool_no")
    @classmethod
    def _v_pool(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError("混池号不能为空")
        return str(v).strip()

    @field_validator("concentration")
    @classmethod
    def _v_conc(cls, v: str) -> str:
        if v is None or not str(v).strip():
            raise ValueError("浓度不能为空")
        return str(v).strip()


class RestoreRequest(BaseModel):
    """一次还原请求: 选 1..8 个非空混池并录入各自浓度。"""

    measurements: list[MeasurementInput] = Field(min_length=1, max_length=MAX_POOLS)

    @field_validator("measurements")
    @classmethod
    def _v_unique_pools(cls, v: list[MeasurementInput]) -> list[MeasurementInput]:
        nos = [m.pool_no for m in v]
        if len(set(nos)) != len(nos):
            raise ValueError("同一请求内混池号不可重复")
        return v
