from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import (
    CollectRequest,
    CollectResponse,
    ProjectOut,
    ReconcileRequest,
    ReconciliationResponse,
    ReconciliationRunOut,
    RunOut,
)
from .reconciliation_service import (
    query_latest_reconciliation,
    run_reconciliation,
)
from .service import ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集与双源核对 API",
    description=(
        "单源采集：提交公告列表页地址，抓取全部详情页并将更正公告按引用的原公告编号"
        "归并，按发布时间确定生效截止时间；失败时保留上次成功结果。\n\n"
        "双源核对：传入两个来源的列表页 URL，各自分页抓取、归并更正，以原公告编号"
        "对齐项目，标明两边截止时间一致/不同/仅单边刊载；两侧结果同事务发布，"
        "任一来源失败时保留上一轮成对快照。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]


@app.post(
    "/api/reconciliations",
    response_model=ReconciliationResponse,
    status_code=200,
)
def reconcile_endpoint(req: ReconcileRequest) -> ReconciliationResponse:
    """触发一轮双源核对：两边各自抓取归并，成对发布核对快照。"""
    try:
        payload = run_reconciliation(
            req.source_a_url,
            req.source_b_url,
            req.source_a_label,
            req.source_b_label,
        )
    except ServiceError as exc:
        # 422：任一来源读取/归并失败，本轮未写入，上一轮成对快照保持不变
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ReconciliationResponse(**payload)


@app.get("/api/reconciliations/latest", response_model=ReconciliationResponse)
def latest_reconciliation() -> ReconciliationResponse:
    """查询最近一轮已发布的双源核对快照（始终成对，不会一新一旧）。"""
    return ReconciliationResponse(**query_latest_reconciliation())


@app.get(
    "/api/reconciliation-runs",
    response_model=list[ReconciliationRunOut],
)
def list_reconciliation_runs(
    limit: int = Query(20, ge=1, le=200),
) -> list[ReconciliationRunOut]:
    with database.get_conn() as conn:
        return [
            ReconciliationRunOut(**dict(r))
            for r in database.fetch_reconciliation_runs(conn, limit)
        ]
