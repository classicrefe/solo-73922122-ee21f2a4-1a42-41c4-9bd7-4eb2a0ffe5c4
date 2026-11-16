"""请求/响应 Pydantic 模型与入参校验。

约束:
  * 每单至多 20 项操作 (三类合计)
  * 体积为非负整数微升; 明细用量须为正整数
  * 编号非空
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ITEMS_PER_ORDER = 20

SampleOp = Literal["insert", "update", "delete"]
PoolOp = Literal["insert", "update", "delete"]
DetailOp = Literal["insert", "update", "delete"]


def _check_code(v: str) -> str:
    if v is None or not v.strip():
        raise ValueError("编号不能为空")
    return v.strip()


class SampleItem(BaseModel):
    op: SampleOp
    sample_no: str
    amount_ul: int | None = Field(default=None, ge=0)

    @field_validator("sample_no")
    @classmethod
    def _v_no(cls, v: str) -> str:
        return _check_code(v)

    @model_validator(mode="after")
    def _v_amount(self) -> "SampleItem":
        if self.op in ("insert", "update") and self.amount_ul is None:
            raise ValueError("样本 insert/update 必须提供非负整数 amount_ul")
        return self


class PoolItem(BaseModel):
    op: PoolOp
    pool_no: str
    total_ul: int | None = Field(default=None, ge=0)

    @field_validator("pool_no")
    @classmethod
    def _v_no(cls, v: str) -> str:
        return _check_code(v)

    @model_validator(mode="after")
    def _v_total(self) -> "PoolItem":
        if self.op in ("insert", "update") and self.total_ul is None:
            raise ValueError("混池 insert/update 必须提供非负整数 total_ul")
        return self


class DetailItem(BaseModel):
    op: DetailOp
    sample_no: str
    pool_no: str
    amount_ul: int | None = Field(default=None, ge=1)  # 明细用量须为正

    @field_validator("sample_no", "pool_no")
    @classmethod
    def _v_no(cls, v: str) -> str:
        return _check_code(v)

    @model_validator(mode="after")
    def _v_amount(self) -> "DetailItem":
        if self.op in ("insert", "update") and self.amount_ul is None:
            raise ValueError("明细 insert/update 必须提供正整数 amount_ul")
        return self


class OrderRequest(BaseModel):
    samples: list[SampleItem] = Field(default_factory=list)
    pools: list[PoolItem] = Field(default_factory=list)
    details: list[DetailItem] = Field(default_factory=list)
    dry_run: bool = False

    @model_validator(mode="after")
    def _v_size_and_dupes(self) -> "OrderRequest":
        total = len(self.samples) + len(self.pools) + len(self.details)
        if total == 0:
            raise ValueError("订单至少包含一项操作")
        if total > MAX_ITEMS_PER_ORDER:
            raise ValueError(f"每单至多 {MAX_ITEMS_PER_ORDER} 项, 当前 {total} 项")

        sample_keys = [s.sample_no for s in self.samples]
        pool_keys = [p.pool_no for p in self.pools]
        detail_keys = [(d.sample_no, d.pool_no) for d in self.details]
        if len(set(sample_keys)) != len(sample_keys):
            raise ValueError("同一订单内样本号重复操作")
        if len(set(pool_keys)) != len(pool_keys):
            raise ValueError("同一订单内混池号重复操作")
        if len(set(detail_keys)) != len(detail_keys):
            raise ValueError("同一订单内 (样本号, 混池号) 重复操作")
        return self
