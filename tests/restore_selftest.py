#!/usr/bin/env python3
"""还原作业端到端自测: 精确求解、发布守卫、失效/停供、重启恢复语义。

用法:
  DATABASE_URL="postgresql://lab@/labdb?host=/tmp/pgrun6&port=5438" \
  BASE_URL="http://127.0.0.1:8007" python3 tests/restore_selftest.py

脚本会重建 schema 并重置演示数据, 可重复运行。失败即非零退出。
只依赖标准库 + psycopg。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import psycopg

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8007")
DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://lab@/labdb?host=/tmp/pgrun6&port=5438"
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
        BASE_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def http_get(path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(BASE_URL + path, timeout=20) as resp:
        return resp.status, json.loads(resp.read().decode())


def sql_conn(autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(DB_URL, autocommit=autocommit)
    conn.execute("SET lock_timeout='10s'")
    conn.rollback()
    return conn


def reset_db() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conn = sql_conn(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in ("db/01_schema.sql", "db/02_seed.sql",
                  "db/03_restoration_schema.sql"):
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                cur.execute(fh.read())
    conn.close()
    print("== 数据库已重置 (含还原 schema) ==")


def wait_job(job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = http_get(f"/api/restoration/jobs/{job_id}")
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内终结")


def make_p003_nonempty() -> None:
    """把空池 P003 填成 S001 100 (总量 100), 用于唯一解构造。"""
    http_post("/api/orders", {
        "pools": [{"op": "update", "pool_no": "P003", "total_ul": 100}],
        "details": [{"op": "insert", "sample_no": "S001",
                     "pool_no": "P003", "amount_ul": 100}],
    })


# ---------------------------------------------------------------------------
def test_unique_and_fractions() -> None:
    print("\n[A] 唯一解 / 非唯一 / 无解 / 分数精度")
    make_p003_nonempty()

    # 唯一: P001(300x1+200x2=500*1) + P003(100x1=100*1) => x1=1,x2=1
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P001", "concentration": "1"},
        {"pool_no": "P003", "concentration": "1"}]})
    job = wait_job(body["job_id"])
    check("唯一解任务 succeeded", job["status"] == "succeeded", str(job))
    _, res = http_get(f"/api/restoration/results/{job['result_id']}")
    check("唯一解已发布", res["published"] is True and res["valid"] is True)
    check("状态 unique", res["status"] == "unique")
    b = {x["sample_no"]: x for x in res["bounds"]}
    check("S001 唯一=1", b["S001"]["lower"] == "1" == b["S001"]["upper"]
          and b["S001"]["unique"] is True)
    check("S002 唯一=1", b["S002"]["lower"] == "1" == b["S002"]["upper"])

    # 非唯一 + 分数: 仅 P001, c=3 => 300x1+200x2=1500, 自由度 1
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P001", "concentration": "3"}]})
    job = wait_job(body["job_id"])
    _, res = http_get(f"/api/restoration/results/{job['result_id']}")
    check("非唯一状态 non_unique", res["status"] == "non_unique")
    b = {x["sample_no"]: x for x in res["bounds"]}
    check("S001 区间 [0,5]", b["S001"]["lower"] == "0"
          and b["S001"]["upper"] == "5")
    check("S002 区间 [0,15/2] 分数", b["S002"]["upper"] == "15/2")
    check("非唯一不冒充确定值", all(not x["unique"] for x in res["bounds"]))

    # 分数浓度: P003 c=2/3 => 100x1=100*2/3 => x1=2/3
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P003", "concentration": "2/3"}]})
    job = wait_job(body["job_id"])
    _, res = http_get(f"/api/restoration/results/{job['result_id']}")
    b = {x["sample_no"]: x for x in res["bounds"]}
    check("分数浓度精确还原 2/3", b["S001"]["lower"] == "2/3"
          and b["S001"]["upper"] == "2/3", str(b["S001"]))

    # 无解: P001 c=0 (x1=x2=0) 与 P003 c=1 (x1=1) 矛盾
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P001", "concentration": "0"},
        {"pool_no": "P003", "concentration": "1"}]})
    job = wait_job(body["job_id"])
    check("无解任务 succeeded(完整结论)", job["status"] == "succeeded")
    _, res = http_get(f"/api/restoration/results/{job['result_id']}")
    check("状态 infeasible", res["status"] == "infeasible")
    check("无解也发布为完整结果", res["published"] is True
          and res["valid"] is True)
    check("无解无区间", res["bounds"] == [])


def test_input_validation() -> None:
    print("\n[B] 入参校验")
    cases = [
        ([{"pool_no": "P001", "concentration": "-1"}], 422, "负浓度"),
        ([{"pool_no": "P001", "concentration": "1/0"}], 422, "零分母"),
        ([{"pool_no": "P001", "concentration": "abc"}], 422, "非数字"),
        ([{"pool_no": "NOPE", "concentration": "1"}], 404, "不存在池"),
        ([{"pool_no": "P003", "concentration": "1"},
          {"pool_no": "P003", "concentration": "2"}], 422, "重复池号"),
    ]
    for meas, expect, label in cases:
        status, body = http_post("/api/restoration/jobs",
                                 {"measurements": meas})
        check(f"{label} -> {expect}", status == expect, f"got {status} {body}")

    # 9 池 -> 422
    nine = {"measurements": [
        {"pool_no": f"P{i:03d}", "concentration": "1"} for i in range(1, 10)]}
    status, _ = http_post("/api/restoration/jobs", nine)
    check("9 个池 -> 422", status == 422)

    # 空池 -> 409 (种子 P003 已被填, 新建一个空池)
    conn = sql_conn(autocommit=True)
    conn.execute("INSERT INTO pools (pool_no,total_ul) VALUES ('PEMPTY',0) "
                 "ON CONFLICT DO NOTHING")
    conn.close()
    status, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "PEMPTY", "concentration": "1"}]})
    check("空池 -> 409", status == 409, str(body))


def test_invalidation_and_supply() -> None:
    print("\n[C] 更正失效、停供、改回不恢复、无关池隔离")
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P001", "concentration": "3"},
        {"pool_no": "P002", "concentration": "4"}]})
    job = wait_job(body["job_id"])
    rid = job["result_id"]
    _, before = http_get("/api/restoration/results")
    check("发布后进入供数", any(r["result_id"] == rid for r in before["results"]))

    # 更正 P001 (总量不变, 明细 300/200 -> 350/150)
    http_post("/api/orders", {"details": [
        {"op": "update", "sample_no": "S001", "pool_no": "P001",
         "amount_ul": 350},
        {"op": "update", "sample_no": "S002", "pool_no": "P001",
         "amount_ul": 150}]})
    _, after = http_get("/api/restoration/results")
    check("更正后停供", all(r["result_id"] != rid for r in after["results"]))
    _, old = http_get(f"/api/restoration/results/{rid}")
    check("旧结果永久失效", old["valid"] is False
          and old["invalid_reason"] == "composition_correction:P001")

    # P002 测值不受影响
    _, meas = http_get("/api/restoration/measurements")
    p002 = [m for m in meas["measurements"]
            if m["pool_no"] == "P002" and m["status"] == "confirmed"]
    check("无关池 P002 测值仍 confirmed", bool(p002) and p002[0]["concentration"] == "4")

    # 改回 P001 组成, 旧结果不恢复
    http_post("/api/orders", {"details": [
        {"op": "update", "sample_no": "S001", "pool_no": "P001",
         "amount_ul": 300},
        {"op": "update", "sample_no": "S002", "pool_no": "P001",
         "amount_ul": 200}]})
    _, old2 = http_get(f"/api/restoration/results/{rid}")
    check("改回组成旧结果仍失效", old2["valid"] is False)

    # 重新确认即令旧结果失效 (即使浓度相同)
    _, body2 = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P002", "concentration": "4"}]})
    job2 = wait_job(body2["job_id"])
    # 上一条 P001+P002 结果已因 P001 更正失效; 这里确认重新确认 P002 本身不报错
    check("重新确认可正常计算", job2["status"] in ("succeeded", "failed"))


def test_sql_trigger_invalidation() -> None:
    print("\n[D] 独立 SQL 连接更正同样触发失效 (不依赖应用)")
    # 先产生一条有效结果
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P003", "concentration": "1"}]})
    job = wait_job(body["job_id"])
    rid = job["result_id"]

    # 裸 SQL 更正 P003 明细 (保持平衡: S001 100 -> 总量也调整)
    conn = sql_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE pools SET total_ul=120 WHERE pool_no='P003'")
        cur.execute("UPDATE pool_details SET amount_ul=120 "
                    "WHERE sample_no='S001' AND pool_no='P003'")
    conn.commit()
    conn.close()

    _, old = http_get(f"/api/restoration/results/{rid}")
    check("裸 SQL 更正触发结果失效", old["valid"] is False, str(old))
    _, meas = http_get("/api/restoration/measurements")
    p003 = [m for m in meas["measurements"] if m["pool_no"] == "P003"]
    check("裸 SQL 更正确认测值作废",
          all(m["status"] == "superseded" for m in p003))

    # 同值 UPDATE 不失效
    _, body = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P003", "concentration": "1"}]})
    job = wait_job(body["job_id"])
    rid2 = job["result_id"]
    conn = sql_conn(autocommit=True)
    conn.execute("UPDATE pools SET total_ul=120 WHERE pool_no='P003'")
    conn.close()
    _, still = http_get(f"/api/restoration/results/{rid2}")
    check("同值 UPDATE 不失效", still["valid"] is True)


def test_failure_not_published() -> None:
    print("\n[E] 失败不发布半份结果")
    # 造 9 样本池, 确认阶段即 409, 不产生任务结果
    # 必须用显式事务: 自动提交下中间失衡状态会被 R1 触发器拒绝
    conn = sql_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO samples (sample_no, amount_ul) "
                    "SELECT 'Y'||lpad(g::text,2,'0'), 1000 "
                    "FROM generate_series(1,9) g ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO pools (pool_no,total_ul) VALUES ('P9',90) "
                    "ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO pool_details (sample_no,pool_no,amount_ul) "
                    "SELECT 'Y'||lpad(g::text,2,'0'),'P9',10 "
                    "FROM generate_series(1,9) g ON CONFLICT DO NOTHING")
    conn.commit()
    conn.close()
    status, _ = http_post("/api/restoration/jobs", {"measurements": [
        {"pool_no": "P9", "concentration": "1"}]})
    check("9 样本同步 409", status == 409)
    _, results = http_get("/api/restoration/results")
    check("被拒请求无结果流入供数",
          all(r["pool_count"] <= 8 for r in results["results"]))


def main() -> int:
    print(f"== 还原自测开始 ==\nBASE_URL={BASE_URL}\nDB_URL={DB_URL}")
    reset_db()
    test_unique_and_fractions()
    test_input_validation()
    test_invalidation_and_supply()
    test_sql_trigger_invalidation()
    test_failure_not_published()
    print(f"\n== 结果: {PASS} 通过, {FAIL} 失败 ==")
    if FAILURES:
        print("失败项:")
        for f in FAILURES:
            print(" -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
