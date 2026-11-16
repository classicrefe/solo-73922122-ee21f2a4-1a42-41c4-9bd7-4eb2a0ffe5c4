"""FastAPI 入口: 订单录入/更正、试运行、组成取数、还原作业、静态网页。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import (
    ConflictError,
    NotFoundError,
    RuleViolation,
    close_pool,
    open_pool,
)
from .models import OrderRequest
from .restore_models import RestoreRequest
from .restore_service import (
    confirm_measurements,
    create_job,
    get_job,
    get_result,
    list_jobs,
    list_measurements,
    list_published_results,
    recover_interrupted_jobs,
)
from .service import get_composition, submit_order
from .solver import RationalError

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    # 重启恢复: 上次未完成的还原任务一律置 failed, 已完成结论原样保留
    try:
        recover_interrupted_jobs()
    except Exception as exc:  # noqa: BLE001 - 还原表尚未初始化时不应阻断主服务
        print(f"[startup] 还原任务恢复跳过: {exc!r}")
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="混样组成与浓度还原平台", version="2.0.0", lifespan=lifespan)


@app.exception_handler(RuleViolation)
async def _rule_handler(request: Request, exc: RuleViolation) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "rule_violation",
            "message": exc.message,
            "violations": exc.violations,
        },
    )


@app.exception_handler(NotFoundError)
async def _not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "not_found", "message": exc.message, "violations": []},
    )


@app.exception_handler(ConflictError)
async def _conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "conflict", "message": exc.message, "violations": []},
    )


@app.exception_handler(RationalError)
async def _rational_handler(request: Request, exc: RationalError) -> JSONResponse:
    # 浓度不是合法非负有理数: 入参错误, 与 Pydantic 422 保持一致
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_rational", "message": str(exc), "violations": []},
    )


@app.exception_handler(psycopg.Error)
async def _db_handler(request: Request, exc: psycopg.Error) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "database_error", "message": str(exc), "violations": []},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/orders")
def create_order(order: OrderRequest) -> dict:
    """按单录入或更正。整单事务, 失败全部回滚。"""
    return submit_order(order)


@app.get("/api/composition")
def composition() -> dict:
    """返回同一已提交状态的完整组成。"""
    return get_composition()


# ---------------------------------------------------------------------------
# 还原作业
# ---------------------------------------------------------------------------

@app.post("/api/restoration/measurements")
def restoration_confirm(req: RestoreRequest) -> dict:
    """只确认测值 (绑定当前组成版本), 不触发计算。"""
    return confirm_measurements(req)


@app.post("/api/restoration/jobs")
def restoration_create(req: RestoreRequest) -> dict:
    """确认测值并创建后台还原计算任务。"""
    return create_job(req)


@app.get("/api/restoration/jobs")
def restoration_jobs(limit: int = 50) -> dict:
    return {"jobs": list_jobs(limit)}


@app.get("/api/restoration/jobs/{job_id}")
def restoration_job(job_id: int) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
    return job


@app.get("/api/restoration/measurements")
def restoration_measurements() -> dict:
    return {"measurements": list_measurements()}


@app.get("/api/restoration/results")
def restoration_results() -> dict:
    """供数: 仅返回仍有效且已发布的完整结果。"""
    return {"results": list_published_results()}


@app.get("/api/restoration/results/{result_id}")
def restoration_result(result_id: int) -> dict:
    """按 id 回查任意结果 (含已失效/失败), 供审计。"""
    result = get_result(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"结果 {result_id} 不存在")
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
