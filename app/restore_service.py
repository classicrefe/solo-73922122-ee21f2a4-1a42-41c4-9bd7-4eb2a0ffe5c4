"""还原作业: 测值确认、异步精确计算、发布守卫与供数。

事务/并发设计 (与录入平台共用同一连接池与触发器体系):

1. 测值确认 (同步事务)
   * 按 pool_no 全局排序后 SELECT ... FOR UPDATE 锁定相关混池;
   * 校验混池存在且非空, 精确解析非负有理数浓度;
   * 取/建该混池当前组成的版本指纹 composition_versions, 测值绑定 version_id;
   * 旧 confirmed 测值置 superseded, 并显式作废引用这些混池的旧结果
     (重新确认本身即令旧结果永久失效, 即使浓度相同、即使日后改回组成);
   * 插入新的 confirmed 测值后提交, 立即创建计算任务。

2. 计算 (后台线程, 独立事务)
   * 任务行 restoration_jobs 持久化, running/succeeded/failed 全程落库;
   * 计算开始时重新锁定同一批混池, 比对"当前组成指纹"与"测值绑定指纹",
     并确认测值仍是绑定该版本的 confirmed 行 —— 任一不成立即为过期,
     只登记 failed 未发布结果, 绝不发布过期/半份结果;
   * 守卫通过后构造线性系统, 用精确单纯形求每样本可行上下界,
     结论 (unique/non_unique/infeasible) 与组成/测值/区间快照在同一事务内
     落库并 published=true; 计算异常只落 failed 行, 不发布。

3. 供数
   * /results 只返回 valid=true AND published=true 的完整结果;
   * 失效结果 (valid=false) 永久保留、可按 id 回查但不进入供数列表,
     且没有任何路径把 valid 改回 true (改回组成也不恢复)。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from typing import Any

import psycopg

from .config import settings
from .database import ConflictError, NotFoundError, pool
from .restore_models import MAX_SAMPLES, RestoreRequest
from .solver import RationalError, format_fraction, parse_nonneg_rational, solve_feasible_bounds


# ---------------------------------------------------------------------------
# 组成指纹
# ---------------------------------------------------------------------------

def _pool_fingerprint(cur: psycopg.Cursor, pool_no: str) -> dict[str, Any]:
    cur.execute("SELECT pool_composition_fingerprint(%s)", (pool_no,))
    row = cur.fetchone()
    return row["pool_composition_fingerprint"]


def _get_or_create_version(cur: psycopg.Cursor, fp: dict[str, Any]) -> int:
    cur.execute(
        "SELECT version_id FROM composition_versions "
        "WHERE pool_no = %s AND detail_sig = %s",
        (fp["pool_no"], fp["detail_sig"]),
    )
    row = cur.fetchone()
    if row:
        return row["version_id"]
    cur.execute(
        "INSERT INTO composition_versions (pool_no, total_ul, detail_json, detail_sig) "
        "VALUES (%s, %s, %s, %s) RETURNING version_id",
        (fp["pool_no"], fp["total_ul"], psycopg.types.json.Json(fp["details"]),
         fp["detail_sig"]),
    )
    return cur.fetchone()["version_id"]


# ---------------------------------------------------------------------------
# 测值确认 (同步事务)
# ---------------------------------------------------------------------------

def confirm_measurements(request: RestoreRequest) -> dict[str, Any]:
    """确认一批混池的浓度测值, 绑定当前组成版本。返回事务后状态。"""
    # 先在事务外做一次纯函数解析, 非法浓度直接 422, 不占用写事务
    parsed: dict[str, Fraction] = {}
    for m in request.measurements:
        parsed[m.pool_no] = parse_nonneg_rational(m.concentration)

    pool_nos = sorted(parsed)
    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                # 全局确定顺序加锁, 与录入订单的预锁顺序一致, 重叠事务只排队不死锁
                cur.execute(
                    "SELECT pool_no, total_ul FROM pools "
                    "WHERE pool_no = ANY(%s) ORDER BY pool_no FOR UPDATE",
                    (pool_nos,),
                )
                rows = cur.fetchall()
                found = {r["pool_no"]: r["total_ul"] for r in rows}
                missing = [p for p in pool_nos if p not in found]
                if missing:
                    raise NotFoundError(f"混池不存在: {', '.join(missing)}")
                empty = [p for p in pool_nos if found[p] <= 0]
                if empty:
                    raise ConflictError(
                        "只能选择非空混池, 以下混池总量为 0 (空池): "
                        + ", ".join(empty)
                    )

                # 样本数上限在确认阶段即拒绝 (同步 409), 不创建任务、不留半份结果
                cur.execute(
                    "SELECT count(DISTINCT sample_no) AS n "
                    "FROM pool_details WHERE pool_no = ANY(%s)",
                    (pool_nos,),
                )
                sample_n = cur.fetchone()["n"]
                if sample_n > MAX_SAMPLES:
                    raise ConflictError(
                        f"所选混池涉及 {sample_n} 个样本, 超过上限 {MAX_SAMPLES} 个"
                    )

                confirmed: list[dict[str, Any]] = []
                for pno in pool_nos:
                    fp = _pool_fingerprint(cur, pno)
                    if fp["total_ul"] <= 0 or not fp["details"]:
                        raise ConflictError(f"混池 {pno} 为空池, 无法录入测值")
                    version_id = _get_or_create_version(cur, fp)
                    frac = parsed[pno]

                    # 重新确认: 旧测值作废, 引用该池的旧结果永久失效
                    cur.execute(
                        "UPDATE pool_measurements SET status='superseded', "
                        "superseded_at=now() WHERE pool_no=%s AND status='confirmed'",
                        (pno,),
                    )
                    cur.execute(
                        "SELECT invalidate_results_of(%s, %s)",
                        (pno, "measurement_reconfirmed"),
                    )
                    cur.execute(
                        "INSERT INTO pool_measurements "
                        "(pool_no, conc_num, conc_den, version_id, status) "
                        "VALUES (%s, %s, %s, %s, 'confirmed') "
                        "RETURNING measurement_id",
                        (pno, frac.numerator, frac.denominator, version_id),
                    )
                    measurement_id = cur.fetchone()["measurement_id"]
                    confirmed.append({
                        "measurement_id": measurement_id,
                        "pool_no": pno,
                        "concentration": format_fraction(frac),
                        "conc_num": frac.numerator,
                        "conc_den": frac.denominator,
                        "version_id": version_id,
                        "total_ul": fp["total_ul"],
                    })
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {"pool_nos": pool_nos, "confirmed": confirmed}


# ---------------------------------------------------------------------------
# 计算任务: 创建 + 后台执行
# ---------------------------------------------------------------------------

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=settings.restore_workers,
                    thread_name_prefix="restore",
                )
    return _executor


def recover_interrupted_jobs() -> None:
    """启动恢复: 上次未完成的任务一律置 failed, 绝不补发过期结果。

    已 succeeded/failed 的结论原样保留 (重启保留结论)。
    """
    with pool.connection() as conn:
        conn.execute(
            "UPDATE restoration_jobs SET status='failed', finished_at=now(), "
            "error_message=COALESCE(error_message, 'restart_interrupted') "
            "WHERE status IN ('pending','running')"
        )
        conn.commit()


def create_job(request: RestoreRequest) -> dict[str, Any]:
    """确认测值并创建计算任务, 后台线程执行。"""
    confirmation = confirm_measurements(request)
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO restoration_jobs (status, request_json) "
            "VALUES ('pending', %s) RETURNING job_id, created_at",
            (psycopg.types.json.Json({
                "measurements": [m.model_dump() for m in request.measurements],
            }),),
        ).fetchone()
        conn.commit()
        job_id = row["job_id"]

    _get_executor().submit(_run_job_safely, job_id, confirmation["pool_nos"])
    return {"job_id": job_id, "status": "pending", "confirmation": confirmation}


def _run_job_safely(job_id: int, pool_nos: list[str]) -> None:
    """后台入口: 捕获一切异常, 保证失败只落 failed、绝不发布半份结果。"""
    try:
        _run_job(job_id, pool_nos)
    except Exception as exc:  # noqa: BLE001 - 后台任务必须兜底
        try:
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE restoration_jobs SET status='failed', finished_at=now(), "
                    "error_message=%s WHERE job_id=%s AND status <> 'succeeded'",
                    (f"compute_error: {exc!r}"[:500], job_id),
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            pass


def _run_job(job_id: int, pool_nos: list[str]) -> None:
    ordered = sorted(pool_nos)
    with pool.connection() as conn:
        try:
            # 1. 占用任务行并置 running
            cur = conn.execute(
                "SELECT job_id, status FROM restoration_jobs "
                "WHERE job_id=%s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if cur is None or cur["status"] != "pending":
                conn.rollback()
                return
            conn.execute(
                "UPDATE restoration_jobs SET status='running', started_at=now() "
                "WHERE job_id=%s",
                (job_id,),
            )

            # 2. 锁定本任务涉及的全部混池 (与录入/确认事务同序, 排队而非死锁)
            with conn.cursor() as c:
                c.execute(
                    "SELECT pool_no FROM pools WHERE pool_no = ANY(%s) "
                    "ORDER BY pool_no FOR UPDATE",
                    (ordered,),
                )
                locked = {r["pool_no"] for r in c.fetchall()}
                if locked != set(ordered):
                    raise _StaleResultError("任务涉及的混池已被删除或变更")

                # 3. 读取绑定当前组成的 confirmed 测值
                c.execute(
                    "SELECT m.measurement_id, m.pool_no, m.conc_num, m.conc_den, "
                    "       m.version_id, v.detail_sig "
                    "FROM pool_measurements m "
                    "JOIN composition_versions v ON v.version_id = m.version_id "
                    "WHERE m.pool_no = ANY(%s) AND m.status='confirmed' "
                    "ORDER BY m.pool_no",
                    (ordered,),
                )
                meas_rows = c.fetchall()
                if {r["pool_no"] for r in meas_rows} != set(ordered):
                    raise _StaleResultError(
                        "存在混池的测值已失效或需重新确认, 本次计算作废"
                    )

                # 4. 发布守卫: 实时组成指纹必须与测值绑定版本完全一致
                snapshots = []
                for r in meas_rows:
                    fp = _pool_fingerprint(c, r["pool_no"])
                    if fp["detail_sig"] != r["detail_sig"]:
                        raise _StaleResultError(
                            f"混池 {r['pool_no']} 的总量或明细在计算期间被更正, "
                            "测值绑定版本已过期, 不发布结果"
                        )
                    snapshots.append((r, fp))

                # 5. 构造精确线性系统
                sample_set = sorted({
                    d["sample_no"]
                    for _, fp in snapshots for d in fp["details"]
                })
                if len(sample_set) > MAX_SAMPLES:
                    raise ConflictError(
                        f"涉及样本 {len(sample_set)} 个, 超过上限 {MAX_SAMPLES}"
                    )
                idx = {s: j for j, s in enumerate(sample_set)}
                n = len(sample_set)
                A: list[list[Fraction]] = []
                b: list[Fraction] = []
                pool_snapshot = []
                for r, fp in snapshots:
                    conc = Fraction(r["conc_num"], r["conc_den"])
                    row = [Fraction(0) for _ in range(n)]
                    for d in fp["details"]:
                        row[idx[d["sample_no"]]] = Fraction(d["amount_ul"])
                    A.append(row)
                    # Σ(明细用量×样本浓度) = 混池总量 × c
                    b.append(Fraction(fp["total_ul"]) * conc)
                    pool_snapshot.append({
                        "pool_no": r["pool_no"],
                        "total_ul": fp["total_ul"],
                        "concentration": format_fraction(conc),
                        "conc_num": r["conc_num"],
                        "conc_den": r["conc_den"],
                        "measurement_id": r["measurement_id"],
                        "version_id": r["version_id"],
                        "detail_sig": r["detail_sig"],
                        "details": fp["details"],
                    })

                # 6. 精确求解
                solution = solve_feasible_bounds(A, b)

                snapshot_json = {
                    "pools": pool_snapshot,
                    "samples": sample_set,
                }
                if solution["status"] == "infeasible":
                    status = "infeasible"
                    bounds_json = []
                else:
                    status = solution["status"]
                    bounds_json = [
                        {
                            "sample_no": sample_set[j],
                            "lower": format_fraction(bd["lower"]),
                            "upper": format_fraction(bd["upper"]),
                            "lower_num": bd["lower"].numerator,
                            "lower_den": bd["lower"].denominator,
                            "upper_num": bd["upper"].numerator,
                            "upper_den": bd["upper"].denominator,
                            "unique": bd["lower"] == bd["upper"],
                        }
                        for j, bd in enumerate(solution["bounds"])
                    ]

                # 7. 落库完整结论并发布 (同一事务; 守卫已通过, 行锁未释放)
                c.execute(
                    "INSERT INTO restoration_results "
                    "(status, pool_count, sample_count, snapshot_json, bounds_json, "
                    " published, valid, published_at) "
                    "VALUES (%s, %s, %s, %s, %s, true, true, now()) RETURNING result_id",
                    (status, len(pool_snapshot), n,
                     psycopg.types.json.Json(snapshot_json),
                     psycopg.types.json.Json(bounds_json)),
                )
                result_id = c.fetchone()["result_id"]
                c.executemany(
                    "INSERT INTO restoration_result_pools (result_id, pool_no) "
                    "VALUES (%s, %s)",
                    [(result_id, pno) for pno in ordered],
                )
                conn.execute(
                    "UPDATE restoration_jobs SET status='succeeded', "
                    "result_id=%s, finished_at=now() WHERE job_id=%s",
                    (result_id, job_id),
                )
            conn.commit()
        except _StaleResultError as exc:
            conn.rollback()
            _record_failure(job_id, ordered, str(exc), reason="stale_guard")
        except ConflictError as exc:
            conn.rollback()
            _record_failure(job_id, ordered, str(exc), reason="rejected")
        except Exception:
            conn.rollback()
            raise


def _record_failure(job_id: int, pool_nos: list[str],
                    message: str, reason: str) -> None:
    """登记一条 failed、未发布的结果行, 任务置 failed; 不产生任何供数记录。"""
    with pool.connection() as conn:
        try:
            row = conn.execute(
                "INSERT INTO restoration_results "
                "(status, pool_count, sample_count, snapshot_json, bounds_json, "
                " error_message, published, valid) "
                "VALUES ('failed', %s, 0, %s, %s, %s, false, true) "
                "RETURNING result_id",
                (len(pool_nos),
                 psycopg.types.json.Json({"pools": pool_nos, "reason": reason}),
                 psycopg.types.json.Json([]),
                 message[:500]),
            ).fetchone()
            result_id = row["result_id"]
            with conn.cursor() as c:
                c.executemany(
                    "INSERT INTO restoration_result_pools (result_id, pool_no) "
                    "VALUES (%s, %s)",
                    [(result_id, p) for p in pool_nos],
                )
            conn.execute(
                "UPDATE restoration_jobs SET status='failed', result_id=%s, "
                "finished_at=now(), error_message=%s WHERE job_id=%s",
                (result_id, message[:500], job_id),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            # 结果行落库失败也必须保证任务终结为 failed, 绝不留 pending 悬挂
            with pool.connection() as conn2:
                conn2.execute(
                    "UPDATE restoration_jobs SET status='failed', finished_at=now(), "
                    "error_message=%s WHERE job_id=%s AND status <> 'succeeded'",
                    (f"failure_recording_error: {exc!r}; original: {message}"[:500],
                     job_id),
                )
                conn2.commit()
            print(f"[restore] _record_failure 落库异常已兜底: {exc!r}")


class _StaleResultError(Exception):
    """计算期间组成/测值已变更, 迟到结果不得发布。"""


# ---------------------------------------------------------------------------
# 供数与回查
# ---------------------------------------------------------------------------

def list_measurements() -> list[dict[str, Any]]:
    """返回全部测值 (confirmed 在前), 供网页展示当前/失效状态。"""
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = conn.execute(
            "SELECT m.measurement_id, m.pool_no, m.conc_num, m.conc_den, "
            "       m.version_id, m.status, m.created_at, m.superseded_at, "
            "       v.total_ul AS version_total_ul, v.detail_sig "
            "FROM pool_measurements m "
            "JOIN composition_versions v ON v.version_id = m.version_id "
            "ORDER BY m.status DESC, m.measurement_id DESC"
        ).fetchall()
    out = []
    for r in rows:
        frac = Fraction(r["conc_num"], r["conc_den"])
        out.append({
            "measurement_id": r["measurement_id"],
            "pool_no": r["pool_no"],
            "concentration": format_fraction(frac),
            "version_id": r["version_id"],
            "status": r["status"],
            "version_total_ul": r["version_total_ul"],
            "detail_sig": r["detail_sig"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "superseded_at": r["superseded_at"].isoformat() if r["superseded_at"] else None,
        })
    return out


def list_published_results() -> list[dict[str, Any]]:
    """供数: 仅返回仍有效且已发布的完整结果 (失效结果永不出现)。"""
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = conn.execute(
            "SELECT result_id, status, pool_count, sample_count, "
            "       snapshot_json, bounds_json, error_message, published, valid, "
            "       invalid_reason, created_at, published_at, invalidated_at "
            "FROM restoration_results "
            "WHERE valid = true AND published = true "
            "ORDER BY result_id DESC"
        ).fetchall()
    return [_serialize_result(r) for r in rows]


def get_result(result_id: int) -> dict[str, Any] | None:
    """按 id 回查任意结果 (含已失效/失败), 附带有效标志与失效原因。"""
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        row = conn.execute(
            "SELECT * FROM restoration_results WHERE result_id = %s",
            (result_id,),
        ).fetchone()
    return _serialize_result(row) if row else None


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT j.job_id, j.status, j.result_id, j.error_message, "
            "       j.created_at, j.started_at, j.finished_at, j.request_json "
            "FROM restoration_jobs j ORDER BY j.job_id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "job_id": r["job_id"],
            "status": r["status"],
            "result_id": r["result_id"],
            "error_message": r["error_message"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        })
    return out


def get_job(job_id: int) -> dict[str, Any] | None:
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT job_id, status, result_id, error_message, "
            "created_at, started_at, finished_at "
            "FROM restoration_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    if not r:
        return None
    return {
        "job_id": r["job_id"],
        "status": r["status"],
        "result_id": r["result_id"],
        "error_message": r["error_message"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
    }


def _serialize_result(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "result_id": r["result_id"],
        "status": r["status"],
        "pool_count": r["pool_count"],
        "sample_count": r["sample_count"],
        "snapshot": r["snapshot_json"],
        "bounds": r["bounds_json"],
        "published": r["published"],
        "valid": r["valid"],
        "invalid_reason": r.get("invalid_reason"),
        "error_message": r.get("error_message"),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "invalidated_at": r["invalidated_at"].isoformat() if r.get("invalidated_at") else None,
    }
