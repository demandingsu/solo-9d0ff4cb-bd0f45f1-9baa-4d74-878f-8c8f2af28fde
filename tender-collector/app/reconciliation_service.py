"""双源核对编排：两侧各自抓取/归并 -> 对齐 -> 单事务整体发布快照。

关键保证：
1. 两侧先在内存中完成抓取与归并校验，任一侧读取或归并失败都不写任何数据，
   上一轮已发布的成对快照原样保留，查询绝不会出现“一新一旧”；
2. 成功时两侧数据与快照在同一个事务内提交，要么整体可见要么整体回滚；
3. 采集与保存按来源隔离：``recon_announcements`` 以 source_id 分区，
   同一公告编号在两个来源内容不同也互不覆盖。
"""
from __future__ import annotations

from hashlib import sha1

from . import database
from .merge import MergeError, merge_projects
from .reconcile import (
    STATUS_LABELS,
    ReconciliationItem,
    reconcile,
    summarize,
)
from .scraper import CollectionError, collect
from .service import ServiceError


def source_id_for(url: str) -> str:
    """由列表页 URL 派生稳定的来源标识，保证按来源隔离存储。"""
    return "src_" + sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def _scrape_side(list_url: str) -> tuple[list[dict], list]:
    """抓取单侧并完成归并校验；失败抛出 CollectionError/MergeError。"""
    raw = collect(list_url)
    effective = merge_projects(raw)  # 校验悬空引用等数据错误
    return raw, effective


def _snapshot_row(item: ReconciliationItem) -> dict:
    a, b = item.source_a, item.source_b
    return {
        "original_code": item.original_code,
        "project_name": item.project_name,
        "status": item.status,
        "a_project_name": a.project_name if a else None,
        "a_deadline": a.deadline if a else None,
        "a_source_code": a.source_code if a else None,
        "a_source_url": a.source_url if a else None,
        "b_project_name": b.project_name if b else None,
        "b_deadline": b.deadline if b else None,
        "b_source_code": b.source_code if b else None,
        "b_source_url": b.source_url if b else None,
    }


def _item_payload(item: ReconciliationItem) -> dict:
    return {
        "project_name": item.project_name,
        "original_code": item.original_code,
        "status": item.status,
        "status_label": STATUS_LABELS[item.status],
        "source_a": vars(item.source_a) if item.source_a else None,
        "source_b": vars(item.source_b) if item.source_b else None,
    }


def _empty_response() -> dict:
    return {
        "status": "no_snapshot",
        "snapshot_id": None,
        "run_id": None,
        "created_at": None,
        "source_a_url": None,
        "source_b_url": None,
        "source_a_label": None,
        "source_b_label": None,
        "summary": {
            "consistent": 0,
            "conflict": 0,
            "source_a_only": 0,
            "source_b_only": 0,
            "total": 0,
        },
        "items": [],
    }


def run_reconciliation(
    source_a_url: str,
    source_b_url: str,
    source_a_label: str | None = None,
    source_b_label: str | None = None,
) -> dict:
    started_at = database.utcnow_iso()

    # 1) 两侧分别抓取 + 内存归并：任一失败即整体放弃，不触碰任何已发布快照
    try:
        raw_a, effective_a = _scrape_side(source_a_url)
        raw_b, effective_b = _scrape_side(source_b_url)
    except (CollectionError, MergeError) as exc:
        with database.get_conn() as conn:
            database.record_reconciliation_run(
                conn,
                source_a_url=source_a_url,
                source_b_url=source_b_url,
                status="failed",
                items_a=0,
                items_b=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise ServiceError(str(exc)) from exc

    # 2) 跨来源对齐（纯函数，不依赖任何顺序）
    items = reconcile(effective_a, effective_b)
    rows = [_snapshot_row(item) for item in items]

    # 3) 单事务：两侧数据隔离替换 + 运行记录 + 整快照一起发布
    with database.get_conn() as conn:
        try:
            database.replace_recon_announcements(
                conn, source_id_for(source_a_url), raw_a
            )
            database.replace_recon_announcements(
                conn, source_id_for(source_b_url), raw_b
            )
            run_id = database.record_reconciliation_run(
                conn,
                source_a_url=source_a_url,
                source_b_url=source_b_url,
                status="success",
                items_a=len(raw_a),
                items_b=len(raw_b),
                error=None,
                started_at=started_at,
            )
            snapshot_id = database.insert_recon_snapshot(
                conn,
                run_id=run_id,
                source_a_url=source_a_url,
                source_b_url=source_b_url,
                items=rows,
            )
            conn.commit()
        except Exception as exc:  # pragma: no cover - 兜底回滚
            conn.rollback()
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    summary = summarize(items)
    return {
        "status": "success",
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "created_at": database.utcnow_iso(),
        "source_a_url": source_a_url,
        "source_b_url": source_b_url,
        "source_a_label": source_a_label,
        "source_b_label": source_b_label,
        "summary": {**summary, "total": len(items)},
        "items": [_item_payload(i) for i in items],
    }


def query_latest_reconciliation() -> dict:
    """返回最近一轮已发布快照；从未成功核对过时返回 no_snapshot 空结果。"""
    with database.get_conn() as conn:
        snapshot = database.fetch_latest_snapshot(conn)
        if snapshot is None:
            return _empty_response()
        rows = database.fetch_snapshot_items(conn, snapshot["id"])

    items = [
        {
            "project_name": r["project_name"],
            "original_code": r["original_code"],
            "status": r["status"],
            "status_label": STATUS_LABELS[r["status"]],
            "source_a": _side(r, "a"),
            "source_b": _side(r, "b"),
        }
        for r in rows
    ]
    counts = {"consistent": 0, "conflict": 0, "source_a_only": 0, "source_b_only": 0}
    for r in rows:
        counts[r["status"]] += 1

    return {
        "status": "success",
        "snapshot_id": snapshot["id"],
        "run_id": snapshot["run_id"],
        "created_at": snapshot["created_at"],
        "source_a_url": snapshot["source_a_url"],
        "source_b_url": snapshot["source_b_url"],
        "source_a_label": None,
        "source_b_label": None,
        "summary": {**counts, "total": len(rows)},
        "items": items,
    }


def _side(row, prefix: str) -> dict | None:
    deadline = row[f"{prefix}_deadline"]
    if deadline is None:
        return None
    return {
        "project_name": row[f"{prefix}_project_name"],
        "original_code": row["original_code"],
        "deadline": deadline,
        "source_code": row[f"{prefix}_source_code"],
        "source_url": row[f"{prefix}_source_url"],
    }
