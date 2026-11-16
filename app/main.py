"""FastAPI 入口: 订单录入/更正、试运行、组成取数、静态网页。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
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
from .service import get_composition, submit_order

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="混样组成录入平台", version="1.0.0", lifespan=lifespan)


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
