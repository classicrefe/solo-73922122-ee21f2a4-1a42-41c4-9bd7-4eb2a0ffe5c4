#!/usr/bin/env python3
"""端到端自测: FastAPI 订单接口 + 独立 READ COMMITTED SQL 连接 + 并发冲突。

用法 (本地):
  DATABASE_URL="postgresql://lab@/labdb?host=/tmp/pgrun&port=5433" \
  BASE_URL="http://127.0.0.1:8000" python3 tests/selftest.py

Docker 环境:
  DATABASE_URL="postgresql://lab:lab@localhost:5432/labdb" \
  BASE_URL="http://localhost:8000" python3 tests/selftest.py

脚本只依赖标准库 + psycopg。失败即非零退出。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime

import psycopg

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://lab@/labdb?host=/tmp/pgrun&port=5433"
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def http_post(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def http_get(path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(BASE_URL + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def sql_conn(autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(DB_URL, autocommit=autocommit)
    # 显式确认隔离级别为 READ COMMITTED (PostgreSQL 默认);
    # 锁等待上限 10s, 超时按 SQLSTATE 55P03 失败, 避免测试无限挂起
    conn.execute("SET lock_timeout = '10s'")
    conn.rollback()  # 结束 SET 隐式开启的事务, 参数为会话级仍然保留
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    return conn


def reset_db() -> None:
    """重建 schema 并灌入演示数据, 保证测试可重复。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    conn = sql_conn(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        with open(os.path.join(root, "db", "01_schema.sql"), encoding="utf-8") as f:
            cur.execute(f.read())
        with open(os.path.join(root, "db", "02_seed.sql"), encoding="utf-8") as f:
            cur.execute(f.read())
    conn.close()
    print("== 数据库已重置为演示数据 ==")


# ---------------------------------------------------------------------------
def test_api_basic() -> None:
    print("\n[A] API 基础功能")
    status, body = http_get("/health")
    check("健康检查 200", status == 200 and body["status"] == "ok")

    status, body = http_get("/api/composition")
    check("取数 200", status == 200)
    check(
        "取数含 4 样本 3 混池",
        len(body["samples"]) == 4 and len(body["pools"]) == 3,
        str(body),
    )
    p1 = next(p for p in body["pools"] if p["pool_no"] == "P001")
    check("快照内 P001 平衡", p1["total_ul"] == p1["detail_sum"] == 500)

    # 合法订单: 向空池 P003 录入 S004 100
    order = {
        "samples": [],
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 100}],
        "details": [
            {"op": "insert", "sample_no": "S004", "pool_no": "P003", "amount_ul": 100}
        ],
    }
    status, body = http_post("/api/orders", order)
    check("合法订单提交 200", status == 200 and body["committed"] is True, str(body))
    if status == 200:
        p3 = next(p for p in body["composition"]["pools"] if p["pool_no"] == "P003")
        check("返回组成中 P003 平衡", p3["total_ul"] == p3["detail_sum"] == 100)

    # 更正: 明细 100 -> 80, 池总量同步改为 80
    order = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 80}],
        "details": [
            {"op": "update", "sample_no": "S004", "pool_no": "P003", "amount_ul": 80}
        ],
    }
    status, body = http_post("/api/orders", order)
    check("更正订单提交 200", status == 200, str(body))


def test_api_validation() -> None:
    print("\n[B] API 校验与违规对象展示")
    # R1 池失衡
    order = {
        "pools": [{"op": "update", "pool_no": "P001", "total_ul": 499}],
        "details": [],
    }
    status, body = http_post("/api/orders", order)
    check("R1 失衡返回 409", status == 409, str(body))
    check(
        "R1 违规对象为混池 P001",
        any(v.get("object") == "P001" and v.get("type") == "pool_mismatch"
            for v in body.get("violations", [])),
        str(body.get("violations")),
    )

    # R2 样本超量: S003 原量 500 已用 150, 再加 400 -> 550
    order = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 480}],
        "details": [
            {"op": "insert", "sample_no": "S003", "pool_no": "P003", "amount_ul": 400}
        ],
    }
    status, body = http_post("/api/orders", order)
    check("R2 超量返回 409", status == 409, str(body))
    check(
        "R2 违规对象为样本 S003",
        any(v.get("object") == "S003" and v.get("type") == "sample_overuse"
            for v in body.get("violations", [])),
        str(body.get("violations")),
    )

    # 一次订单含多个违规对象, 应一次全部返回
    order = {
        "samples": [{"op": "update", "sample_no": "S002", "amount_ul": 10}],
        "pools": [{"op": "update", "pool_no": "P001", "total_ul": 1}],
    }
    status, body = http_post("/api/orders", order)
    objs = {v.get("object") for v in body.get("violations", [])}
    check("多违规返回 409", status == 409, str(body))
    check("多违规对象齐全 (S002+P001)", {"S002", "P001"}.issubset(objs), str(objs))

    # 试运行: 合法但不提交
    before = http_get("/api/composition")[1]
    order = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 50}],
        "details": [
            {"op": "update", "sample_no": "S004", "pool_no": "P003", "amount_ul": 50}
        ],
        "dry_run": True,
    }
    status, body = http_post("/api/orders", order)
    check("试运行合法返回 200 且未提交", status == 200 and body["committed"] is False)
    after = http_get("/api/composition")[1]
    p3_before = next(p for p in before["pools"] if p["pool_no"] == "P003")
    p3_after = next(p for p in after["pools"] if p["pool_no"] == "P003")
    check("试运行后数据不变", p3_before["total_ul"] == p3_after["total_ul"] == 80)

    # 试运行非法也应报错且不提交
    order = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 999}],
        "dry_run": True,
    }
    status, body = http_post("/api/orders", order)
    check("试运行非法返回 409", status == 409)

    # 入参: 21 项 / 负体积 / 明细 0 用量
    big = {"samples": [{"op": "insert", "sample_no": f"X{i}", "amount_ul": 1}
                       for i in range(21)]}
    status, body = http_post("/api/orders", big)
    check("超过 20 项返回 422", status == 422, str(body))

    status, body = http_post("/api/orders", {
        "samples": [{"op": "insert", "sample_no": "NEG", "amount_ul": -1}]})
    check("负体积返回 422", status == 422)

    status, body = http_post("/api/orders", {
        "details": [{"op": "insert", "sample_no": "S001",
                     "pool_no": "P003", "amount_ul": 0}]})
    check("明细 0 用量返回 422", status == 422)

    # 更新不存在对象 -> 404, 且无副作用
    status, body = http_post("/api/orders", {
        "pools": [{"op": "update", "pool_no": "NOPE", "total_ul": 0}]})
    check("更新不存在对象 404", status == 404, str(body))


def test_sql_connection_rules() -> None:
    print("\n[C] 独立 READ COMMITTED SQL 连接同样受规则约束")
    # 自动提交单条非法语句 -> 提交点被触发器拒绝
    conn = sql_conn(autocommit=True)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("UPDATE pools SET total_ul = 1 WHERE pool_no = 'P001'")
                check("SQL 单条池失衡被拒", False, "未抛异常")
            except psycopg.Error as e:
                check("SQL 单条池失衡被拒", e.sqlstate == "23514", str(e))

            try:
                cur.execute(
                    "INSERT INTO pool_details VALUES ('S003','P003', 1000)"
                )
                check("SQL 明细正值 CHECK 之外的规则触发(超量)", False, "未抛异常")
            except psycopg.Error as e:
                # 既可能是 R2 触发器 (23514), 也可能先经其他检查; 关键是被拒绝
                check("SQL 样本超量被拒", e.sqlstate == "23514", str(e))

            try:
                cur.execute("INSERT INTO samples VALUES ('Z', -5)")
                check("SQL 负原量 CHECK 被拒", False)
            except psycopg.Error as e:
                check("SQL 负原量 CHECK 被拒", e.sqlstate == "23514", str(e))
    finally:
        conn.close()

    # 显式事务内暂时失衡 -> 最终合法可提交
    conn = sql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pools SET total_ul = 0 WHERE pool_no = 'P003'")
            cur.execute("DELETE FROM pool_details WHERE pool_no = 'P003'")
            cur.execute(
                "INSERT INTO pool_details VALUES ('S004','P003', 30),"
                "('S002','P003', 20)"
            )
            cur.execute("UPDATE pools SET total_ul = 50 WHERE pool_no = 'P003'")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_ul, (SELECT sum(amount_ul) FROM pool_details "
                "WHERE pool_no='P003') FROM pools WHERE pool_no='P003'"
            )
            total, used = cur.fetchone()
        check("SQL 暂时失衡最终合法可提交", total == used == 50, f"{total}/{used}")
    finally:
        conn.close()

    # 显式事务最终非法 -> 整单回滚
    conn = sql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pools SET total_ul = 51 WHERE pool_no = 'P003'")
        try:
            conn.commit()
            check("SQL 非法提交被拒", False)
        except psycopg.Error as e:
            check("SQL 非法提交被拒", e.sqlstate == "23514", str(e))
    finally:
        conn.rollback()
        conn.close()

    # 回滚后数据保持 50
    conn = sql_conn(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT total_ul FROM pools WHERE pool_no='P003'")
        check("回滚后 P003 仍为 50", cur.fetchone()[0] == 50)
    conn.close()


def test_concurrency() -> None:
    print("\n[D] 并发冲突: 不能各自通过却共同提交失衡数据")

    # --- D1: API 路径 (应用在事务开头预锁, 应无死锁, 一胜一拒) ---
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def api_worker(name: str, order: dict) -> None:
        try:
            barrier.wait(timeout=10)
            status, body = http_post("/api/orders", order)
            results[name] = "committed" if status == 200 else f"rejected:{status}"
        except Exception as e:  # noqa: BLE001
            results[name] = f"error:{e}"

    # 两事务都基于 P003 当前 50 (S004 30 + S002 20) 做互斥调整:
    # T1: 追加 S001 10, 池总量 -> 60 (单独合法)
    # T2: 追加 S003 20, 池总量 -> 70 (单独合法)
    # 共同成功将失衡, 必须恰好一个提交、另一个整单拒绝
    order1 = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 60}],
        "details": [
            {"op": "insert", "sample_no": "S001", "pool_no": "P003", "amount_ul": 10}
        ],
    }
    order2 = {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 70}],
        "details": [
            {"op": "insert", "sample_no": "S003", "pool_no": "P003", "amount_ul": 20}
        ],
    }
    th = [
        threading.Thread(target=api_worker, args=("T1", order1)),
        threading.Thread(target=api_worker, args=("T2", order2)),
    ]
    for t in th:
        t.start()
    for t in th:
        t.join()
    print(f"    API 并发结果: {results}")
    committed = [k for k, v in results.items() if v == "committed"]
    rejected = [k for k, v in results.items() if v != "committed"]
    check("API 并发恰好一个事务提交", len(committed) == 1, str(results))
    check("API 并发另一个被整单拒绝(409)", rejected and results[rejected[0]] == "rejected:409",
          str(results))

    # 最终状态必须仍然合法
    conn = sql_conn(autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.total_ul, COALESCE((SELECT sum(amount_ul) FROM pool_details "
            "WHERE pool_no='P003'),0) FROM pools p WHERE pool_no='P003'"
        )
        total, used = cur.fetchone()
    conn.close()
    check("API 并发后 P003 仍然平衡", total == used, f"total={total} used={used}")

    # --- D2: 裸 SQL 路径 (无应用预锁, 可能死锁; 无论死锁还是规则拒绝,
    #     都必须整单回滚, 绝不共同提交失衡数据) ---
    barrier2 = threading.Barrier(2)
    results2: dict[str, str] = {}

    def sql_worker(name: str, sqls: list[str]) -> None:
        conn = sql_conn()
        ok = False
        try:
            with conn.cursor() as cur:
                for sql in sqls:
                    cur.execute(sql)
                barrier2.wait(timeout=10)
                conn.commit()
            ok = True
            results2[name] = "committed"
        except Exception as e:  # noqa: BLE001
            results2[name] = f"rejected:{getattr(e, 'sqlstate', e)}"
        finally:
            if not ok:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # 先重置 P003 到 50 的已知状态
    conn = sql_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_details WHERE pool_no='P003'")
        cur.execute(
            "INSERT INTO pool_details VALUES ('S004','P003',30),('S002','P003',20)"
        )
        cur.execute("UPDATE pools SET total_ul=50 WHERE pool_no='P003'")
    conn.commit()
    conn.close()

    t1_sql = [
        "INSERT INTO pool_details VALUES ('S001','P003', 10)",
        "UPDATE pools SET total_ul = 60 WHERE pool_no = 'P003'",
    ]
    t2_sql = [
        "INSERT INTO pool_details VALUES ('S003','P003', 20)",
        "UPDATE pools SET total_ul = 70 WHERE pool_no = 'P003'",
    ]
    th = [
        threading.Thread(target=sql_worker, args=("Q1", t1_sql)),
        threading.Thread(target=sql_worker, args=("Q2", t2_sql)),
    ]
    for t in th:
        t.start()
    for t in th:
        t.join()
    print(f"    裸 SQL 并发结果: {results2}")
    sql_committed = [k for k, v in results2.items() if v == "committed"]
    check("裸 SQL 不会双方共同提交", len(sql_committed) <= 1, str(results2))
    check(
        "裸 SQL 至少一方被整单拒绝",
        any(v != "committed" for v in results2.values()),
        str(results2),
    )
    conn = sql_conn(autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.total_ul, COALESCE((SELECT sum(amount_ul) FROM pool_details "
            "WHERE pool_no='P003'),0) FROM pools p WHERE pool_no='P003'"
        )
        total, used = cur.fetchone()
    conn.close()
    check("裸 SQL 并发后 P003 仍然平衡", total == used, f"total={total} used={used}")

    # --- D3: 无关样本/混池的并发事务应都能提交 ---
    barrier3 = threading.Barrier(2)
    results3: dict[str, str] = {}

    def independent_worker(name: str, sqls: list[str]) -> None:
        conn = sql_conn()
        ok = False
        try:
            with conn.cursor() as cur:
                for sql in sqls:
                    cur.execute(sql)
                barrier3.wait(timeout=10)
                conn.commit()
            ok = True
            results3[name] = "committed"
        except Exception as e:  # noqa: BLE001
            results3[name] = f"rejected:{getattr(e, 'sqlstate', e)}"
        finally:
            if not ok:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    w1 = [
        "INSERT INTO samples VALUES ('IA1', 100)",
        "INSERT INTO pools VALUES ('IPA1', 0)",
        "INSERT INTO pool_details VALUES ('IA1','IPA1', 40)",
        "UPDATE pools SET total_ul = 40 WHERE pool_no = 'IPA1'",
    ]
    w2 = [
        "INSERT INTO samples VALUES ('IB1', 200)",
        "INSERT INTO pools VALUES ('IPB1', 0)",
        "INSERT INTO pool_details VALUES ('IB1','IPB1', 70)",
        "UPDATE pools SET total_ul = 70 WHERE pool_no = 'IPB1'",
    ]
    th = [
        threading.Thread(target=independent_worker, args=("I1", w1)),
        threading.Thread(target=independent_worker, args=("I2", w2)),
    ]
    for t in th:
        t.start()
    for t in th:
        t.join()
    check("无关事务并行均提交", all(v == "committed" for v in results3.values()),
          str(results3))


def test_snapshot_consistency() -> None:
    print("\n[E] 取数快照一致性")
    status, body = http_get("/api/composition")
    check("取数 200", status == 200)
    ok_pools = all(p["total_ul"] == p["detail_sum"] for p in body["pools"])
    check("快照内所有混池平衡", ok_pools)
    ok_samples = all(
        s["used_ul"] <= s["amount_ul"] for s in body["samples"]
    )
    check("快照内所有样本未超量", ok_samples)
    # 明细求和与池明细列表一致
    detail_consistent = all(
        sum(d["amount_ul"] for d in p["details"]) == p["detail_sum"]
        for p in body["pools"]
    )
    check("明细列表与合计一致", detail_consistent)


def main() -> int:
    print(f"== 自测开始 {datetime.now().isoformat(timespec='seconds')} ==")
    print(f"BASE_URL={BASE_URL}\nDB_URL={DB_URL}")
    reset_db()
    test_api_basic()
    test_api_validation()
    test_sql_connection_rules()
    test_concurrency()
    test_snapshot_consistency()

    print(f"\n== 结果: {PASS} 通过, {FAIL} 失败 ==")
    if FAILURES:
        print("失败项:")
        for f in FAILURES:
            print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
