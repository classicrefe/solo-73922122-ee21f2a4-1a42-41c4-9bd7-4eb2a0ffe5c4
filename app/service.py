"""订单事务执行与组成取数。

一个订单 = 一个数据库事务:
  1. 按相位执行全部增删改 (样本 -> 混池 -> 明细 -> 混池总量调整)
  2. 全量预检查, 一次返回所有违规对象 (不合法则回滚)
  3. SET CONSTRAINTS ALL IMMEDIATE 触发 deferred 约束触发器做数据库终审
  4. COMMIT; dry_run 模式在第 3 步后直接 ROLLBACK

任何步骤失败都整体回滚。死锁/序列化失败按整单重试,
其他冲突 (规则违规、唯一键、外键) 直接返回, 不做部分提交。
"""
from __future__ import annotations

import time
from typing import Any

import psycopg

from .config import settings
from .database import (
    ConflictError,
    NotFoundError,
    RuleViolation,
    pool,
    run_precheck,
    translate_db_error,
)
from .models import OrderRequest

# ---------------------------------------------------------------------------
# 语句模板
# ---------------------------------------------------------------------------

SAMPLE_INSERT = "INSERT INTO samples (sample_no, amount_ul) VALUES (%s, %s)"
SAMPLE_UPDATE = "UPDATE samples SET amount_ul = %s WHERE sample_no = %s"
SAMPLE_DELETE = "DELETE FROM samples WHERE sample_no = %s"
SAMPLE_EXISTS = "SELECT 1 FROM samples WHERE sample_no = %s"

POOL_INSERT = "INSERT INTO pools (pool_no, total_ul) VALUES (%s, %s)"
POOL_UPDATE = "UPDATE pools SET total_ul = %s WHERE pool_no = %s"
POOL_DELETE = "DELETE FROM pools WHERE pool_no = %s"
POOL_EXISTS = "SELECT 1 FROM pools WHERE pool_no = %s"

DETAIL_INSERT = (
    "INSERT INTO pool_details (sample_no, pool_no, amount_ul) VALUES (%s, %s, %s)"
)
DETAIL_UPDATE = (
    "UPDATE pool_details SET amount_ul = %s WHERE sample_no = %s AND pool_no = %s"
)
DETAIL_DELETE = "DELETE FROM pool_details WHERE sample_no = %s AND pool_no = %s"
DETAIL_EXISTS = (
    "SELECT 1 FROM pool_details WHERE sample_no = %s AND pool_no = %s"
)


def _check_rowcount(cur: psycopg.Cursor, kind: str, key: str) -> None:
    if cur.rowcount == 0:
        raise NotFoundError(f"{kind} {key} 不存在, 无法更新或删除")


# 预锁: 在任何写操作 (含外键自动行锁) 之前, 按全局确定顺序一次性锁定
# 本单涉及的全部混池行与样本行。这样重叠事务在同一行上排队而非互相等待,
# 从根本上消除 "外键 KEY SHARE 锁 + FOR UPDATE 锁" 造成的死锁环;
# 不相交事务锁定不同的行, 仍然并行。
def _pre_lock_parent_rows(
    cur: psycopg.Cursor, order: OrderRequest
) -> None:
    pool_nos = sorted(
        {p.pool_no for p in order.pools} | {d.pool_no for d in order.details}
    )
    sample_nos = sorted(
        {s.sample_no for s in order.samples}
        | {d.sample_no for d in order.details}
    )
    if pool_nos:
        cur.execute(
            "SELECT pool_no FROM pools WHERE pool_no = ANY(%s) ORDER BY pool_no FOR UPDATE",
            (list(pool_nos),),
        )
    if sample_nos:
        cur.execute(
            "SELECT sample_no FROM samples WHERE sample_no = ANY(%s) "
            "ORDER BY sample_no FOR UPDATE",
            (list(sample_nos),),
        )


def _execute_order(conn: psycopg.Connection, order: OrderRequest) -> None:
    # 相位 0: 预锁全部相关父行 (必须早于明细 INSERT 的外键行锁)
    with conn.cursor() as cur:
        _pre_lock_parent_rows(cur, order)

    # 相位 1: 样本增删改
    with conn.cursor() as cur:
        for item in order.samples:
            if item.op == "insert":
                cur.execute(SAMPLE_INSERT, (item.sample_no, item.amount_ul))
            elif item.op == "update":
                cur.execute(SAMPLE_EXISTS, (item.sample_no,))
                if cur.fetchone() is None:
                    raise NotFoundError(f"样本 {item.sample_no} 不存在, 无法更新")
                cur.execute(SAMPLE_UPDATE, (item.amount_ul, item.sample_no))
            else:
                cur.execute(SAMPLE_DELETE, (item.sample_no,))
                _check_rowcount(cur, "样本", item.sample_no)

    # 相位 2: 混池增删改
    with conn.cursor() as cur:
        for item in order.pools:
            if item.op == "insert":
                cur.execute(POOL_INSERT, (item.pool_no, item.total_ul))
            elif item.op == "update":
                cur.execute(POOL_EXISTS, (item.pool_no,))
                if cur.fetchone() is None:
                    raise NotFoundError(f"混池 {item.pool_no} 不存在, 无法更新")
                cur.execute(POOL_UPDATE, (item.total_ul, item.pool_no))
            else:
                cur.execute(POOL_DELETE, (item.pool_no,))
                _check_rowcount(cur, "混池", item.pool_no)

    # 相位 3: 明细增删改 (锁触发器在此按确定顺序锁定相关父行)
    with conn.cursor() as cur:
        for item in order.details:
            if item.op == "insert":
                cur.execute(
                    DETAIL_INSERT,
                    (item.sample_no, item.pool_no, item.amount_ul),
                )
            elif item.op == "update":
                cur.execute(DETAIL_EXISTS, (item.sample_no, item.pool_no))
                if cur.fetchone() is None:
                    raise NotFoundError(
                        f"明细 {item.sample_no}/{item.pool_no} 不存在, 无法更新"
                    )
                cur.execute(
                    DETAIL_UPDATE,
                    (item.amount_ul, item.sample_no, item.pool_no),
                )
            else:
                cur.execute(DETAIL_DELETE, (item.sample_no, item.pool_no))
                _check_rowcount(
                    cur, "明细", f"{item.sample_no}/{item.pool_no}"
                )

    # 相位 4: 应用层全量预检查 (一次性列出全部违规对象)
    violations = run_precheck(conn)
    if violations:
        raise RuleViolation(violations)

    # 相位 5: 数据库 deferred 约束触发器终审 (硬保证, 独立 SQL 连接同样生效)
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def submit_order(order: OrderRequest) -> dict[str, Any]:
    """提交订单, 返回结果状态。带死锁/序列化冲突的整单重试。"""
    attempts = settings.max_retries
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        with pool.connection() as conn:
            try:
                # psycopg 连接默认即开事务; READ COMMITTED 是 PG 默认隔离级别
                _execute_order(conn, order)
                if order.dry_run:
                    conn.rollback()
                    return {
                        "status": "dry_run_ok",
                        "committed": False,
                        "violations": [],
                        "message": "校验通过 (试运行, 未提交)",
                    }
                conn.commit()
                return {
                    "status": "committed",
                    "committed": True,
                    "violations": [],
                    "composition": fetch_composition(conn),
                }
            except RuleViolation:
                conn.rollback()
                raise
            except NotFoundError:
                conn.rollback()
                raise
            except ConflictError:
                conn.rollback()
                raise
            except psycopg.Error as exc:
                conn.rollback()
                translated = translate_db_error(exc)
                if isinstance(translated, RuleViolation):
                    raise translated
                if isinstance(translated, (ConflictError, NotFoundError)):
                    raise translated
                if exc.sqlstate in ("40P01", "40001", "55P03") and attempt < attempts:
                    last_exc = exc
                    time.sleep(0.05 * attempt)
                    continue
                raise exc

    # 重试耗尽 (死锁/序列化): 整单拒绝
    raise ConflictError(f"并发冲突, 整单已拒绝 (重试 {attempts} 次): {last_exc}")


# ---------------------------------------------------------------------------
# 组成取数: 单一事务、单一快照, 返回同一已提交状态的完整组成
# ---------------------------------------------------------------------------

COMPOSITION_SQL = """
SELECT
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'sample_no', sample_no,
        'amount_ul', amount_ul,
        'used_ul', COALESCE((SELECT sum(amount_ul) FROM pool_details d
                              WHERE d.sample_no = s.sample_no), 0)
      ) ORDER BY sample_no), '[]'::jsonb)
       FROM samples s) AS samples,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'pool_no', pool_no,
        'total_ul', total_ul,
        'detail_sum', COALESCE((SELECT sum(amount_ul) FROM pool_details d
                                 WHERE d.pool_no = p.pool_no), 0),
        'details', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'sample_no', d2.sample_no,
                'amount_ul', d2.amount_ul
              ) ORDER BY d2.sample_no)
              FROM pool_details d2 WHERE d2.pool_no = p.pool_no), '[]'::jsonb)
      ) ORDER BY pool_no), '[]'::jsonb)
       FROM pools p) AS pools
"""


def fetch_composition(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute(COMPOSITION_SQL).fetchone()
    return {"samples": row["samples"], "pools": row["pools"]}


def get_composition() -> dict[str, Any]:
    with pool.connection() as conn:
        # 只读、单事务单快照, 保证三类记录来自同一已提交状态
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return fetch_composition(conn)
