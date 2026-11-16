"""数据库连接池、错误映射与规则预检查。

规则的最终防线在数据库 (约束触发器 + CHECK + 外键), 本模块的预检查
只用于在提交前一次性返回全部违规对象, 改善 API 与网页的错误展示。
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

def _configure(conn: psycopg.Connection) -> None:
    # 锁等待上限: 冲突事务快速失败 (SQLSTATE 55P03) 并由上层整单重试,
    # 避免请求线程无限挂起; READ COMMITTED 保持 PostgreSQL 默认隔离级别。
    # SET 是会话级参数, 这里的回滚仅结束它隐式开启的事务, 不撤销参数。
    conn.execute("SET lock_timeout = '10s'")
    conn.rollback()


pool = ConnectionPool(
    conninfo=settings.db_url,
    min_size=settings.pool_min_size,
    max_size=settings.pool_max_size,
    kwargs={"row_factory": dict_row},
    configure=_configure,
    open=False,
)


def open_pool() -> None:
    pool.open()


def close_pool() -> None:
    pool.close()


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------

class RuleViolation(Exception):
    """业务规则违规 (409), 携带违规对象列表。"""

    def __init__(self, violations: list[dict[str, Any]], message: str = "混样组成规则校验未通过"):
        self.violations = violations
        self.message = message
        super().__init__(message)


class NotFoundError(Exception):
    """更新/删除目标不存在 (404)。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ConflictError(Exception):
    """唯一键/外键等并发或约束冲突 (409)。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _extract_violations(exc: psycopg.Error) -> list[dict[str, Any]]:
    """从触发器 RAISE 的 DETAIL 中解析违规对象; 解析失败时降级为单条消息。"""
    detail = getattr(exc, "diag", None)
    raw = detail.message_detail if detail else None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("violations"), list):
                return parsed["violations"]
            if isinstance(parsed, dict):
                return [parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    return [{"type": "rule_violation", "message": str(exc)}]


def translate_db_error(exc: psycopg.Error) -> Exception:
    """把 psycopg 异常翻译成应用层异常。"""
    sqlstate = exc.sqlstate or ""
    # 23514 = check_violation; 我们的触发器显式使用该 SQLSTATE
    if sqlstate == "23514":
        return RuleViolation(_extract_violations(exc))
    if sqlstate == "23505":  # unique_violation
        return ConflictError(f"记录已存在: {exc.diag.message_detail or exc}")
    if sqlstate == "23503":  # foreign_key_violation
        return ConflictError(f"外键约束失败: {exc.diag.message_detail or exc}")
    if sqlstate in ("40P01", "40001"):  # deadlock_detected / serialization_failure
        return exc  # 交由上层整单重试
    return exc


# ---------------------------------------------------------------------------
# 提交前全量预检查: 在订单事务内、所有语句执行完毕后调用
# 返回违规对象列表 (空列表代表合法)。与数据库触发器使用相同的规则定义。
# ---------------------------------------------------------------------------

PRECHECK_SQL = """
WITH pool_balance AS (
    SELECT p.pool_no AS object, p.total_ul AS pool_total,
           COALESCE(d.detail_sum, 0) AS detail_sum
      FROM pools p
      LEFT JOIN (SELECT pool_no, sum(amount_ul) AS detail_sum
                   FROM pool_details GROUP BY pool_no) d
        ON d.pool_no = p.pool_no
),
sample_usage AS (
    SELECT s.sample_no AS object, s.amount_ul AS sample_amount,
           COALESCE(d.used, 0) AS used
      FROM samples s
      LEFT JOIN (SELECT sample_no, sum(amount_ul) AS used
                   FROM pool_details GROUP BY sample_no) d
        ON d.sample_no = s.sample_no
)
SELECT jsonb_agg(v) AS violations
  FROM (
    SELECT jsonb_build_object(
             'type', 'pool_mismatch', 'object_kind', 'pool', 'object', object,
             'pool_total', pool_total, 'detail_sum', detail_sum,
             'message', format('混池 %s: 总量 %s μL 与明细合计 %s μL 不一致',
                               object, pool_total, detail_sum)
           ) AS v
      FROM pool_balance
     WHERE pool_total IS DISTINCT FROM detail_sum
    UNION ALL
    SELECT jsonb_build_object(
             'type', 'sample_overuse', 'object_kind', 'sample', 'object', object,
             'sample_amount', sample_amount, 'used', used,
             'message', format('样本 %s: 累计用量 %s μL 超过原量 %s μL',
                               object, used, sample_amount)
           ) AS v
      FROM sample_usage
     WHERE used > sample_amount
) q
"""


def run_precheck(conn: psycopg.Connection) -> list[dict[str, Any]]:
    row = conn.execute(PRECHECK_SQL).fetchone()
    return row["violations"] or []
