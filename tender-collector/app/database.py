"""SQLite 持久化层。

announcements 以公告编号 (code) 为唯一键：重复采集走 UPSERT，不会产生重复记录。
collection_runs 记录每次采集的成败，失败时不触碰 announcements，
因此上次成功采集的完整结果始终保留。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);
CREATE INDEX IF NOT EXISTS idx_announcements_corrects
    ON announcements(corrects_code);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);

-- --------------------------------------------------------------------------
-- 双源核对：采集与保存严格按来源隔离（recon_* 与单源 announcements 互不相通）
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recon_announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    code            TEXT NOT NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE(source_id, code)
);
CREATE INDEX IF NOT EXISTS idx_recon_source
    ON recon_announcements(source_id);
CREATE INDEX IF NOT EXISTS idx_recon_corrects
    ON recon_announcements(source_id, corrects_code);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_a_url    TEXT NOT NULL,
    source_b_url    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_a         INTEGER NOT NULL DEFAULT 0,
    items_b         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);

-- 一轮成功核对发布一个快照；两侧条目同快照同事务写入，查询永远看到成对结果
CREATE TABLE IF NOT EXISTS recon_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    source_a_url    TEXT NOT NULL,
    source_b_url    TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES reconciliation_runs(id)
);

CREATE TABLE IF NOT EXISTS recon_snapshot_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL,
    original_code   TEXT NOT NULL,
    project_name    TEXT NOT NULL,
    status          TEXT NOT NULL,
    a_project_name  TEXT,
    a_deadline      TEXT,
    a_source_code   TEXT,
    a_source_url    TEXT,
    b_project_name  TEXT,
    b_deadline      TEXT,
    b_source_code   TEXT,
    b_source_url    TEXT,
    FOREIGN KEY(snapshot_id) REFERENCES recon_snapshots(id)
);
CREATE INDEX IF NOT EXISTS idx_recon_snapshot_items
    ON recon_snapshot_items(snapshot_id);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_announcements(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """在调用方给定的事务内 UPSERT；重复 code 不产生重复记录。"""
    now = utcnow_iso()
    # 原公告先写，保证更正公告引用的数据已存在（顺序不影响业务判定，仅约束友好）
    rows = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO announcements
            (code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES
            (:code, :title, :kind, :project_name, :published_at, :deadline,
             :corrects_code, :detail_url, :last_seen_at)
        ON CONFLICT(code) DO UPDATE SET
            title=excluded.title,
            kind=excluded.kind,
            project_name=excluded.project_name,
            published_at=excluded.published_at,
            deadline=excluded.deadline,
            corrects_code=excluded.corrects_code,
            detail_url=excluded.detail_url,
            last_seen_at=excluded.last_seen_at
        """,
        [{**r, "last_seen_at": now} for r in rows],
    )


def record_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    status: str,
    items_collected: int,
    error: str | None,
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_url, status, items_collected, error, started_at, utcnow_iso()),
    )


def fetch_all_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM announcements ORDER BY project_name, published_at, code"
        )
    )


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )


# ---------------------------------------------------------------------------
# 双源核对持久化
# ---------------------------------------------------------------------------
def replace_recon_announcements(
    conn: sqlite3.Connection, source_id: str, rows: list[dict[str, Any]]
) -> None:
    """整源替换：在调用方事务内先清掉该来源旧数据再写入，天然按来源隔离。"""
    now = utcnow_iso()
    conn.execute(
        "DELETE FROM recon_announcements WHERE source_id = ?", (source_id,)
    )
    ordered = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO recon_announcements
            (source_id, code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES
            (:source_id, :code, :title, :kind, :project_name, :published_at,
             :deadline, :corrects_code, :detail_url, :last_seen_at)
        """,
        [{**r, "source_id": source_id, "last_seen_at": now} for r in ordered],
    )


def fetch_recon_announcements(
    conn: sqlite3.Connection, source_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT code, title, kind, project_name, published_at, deadline,
                   corrects_code, detail_url
            FROM recon_announcements
            WHERE source_id = ?
            ORDER BY project_name, published_at, code
            """,
            (source_id,),
        )
    )


def record_reconciliation_run(
    conn: sqlite3.Connection,
    *,
    source_a_url: str,
    source_b_url: str,
    status: str,
    items_a: int,
    items_b: int,
    error: str | None,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconciliation_runs
            (source_a_url, source_b_url, status, items_a, items_b,
             error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_a_url,
            source_b_url,
            status,
            items_a,
            items_b,
            error,
            started_at,
            utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


def insert_recon_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    source_a_url: str,
    source_b_url: str,
    items: list[dict[str, Any]],
) -> int:
    """在调用方事务内写入快照头与全部条目；失败回滚则整条快照不可见。"""
    cur = conn.execute(
        """
        INSERT INTO recon_snapshots (run_id, created_at, source_a_url, source_b_url)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, utcnow_iso(), source_a_url, source_b_url),
    )
    snapshot_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO recon_snapshot_items
            (snapshot_id, original_code, project_name, status,
             a_project_name, a_deadline, a_source_code, a_source_url,
             b_project_name, b_deadline, b_source_code, b_source_url)
        VALUES
            (:snapshot_id, :original_code, :project_name, :status,
             :a_project_name, :a_deadline, :a_source_code, :a_source_url,
             :b_project_name, :b_deadline, :b_source_code, :b_source_url)
        """,
        [
            {
                "snapshot_id": snapshot_id,
                "original_code": item["original_code"],
                "project_name": item["project_name"],
                "status": item["status"],
                "a_project_name": item["a_project_name"],
                "a_deadline": item["a_deadline"],
                "a_source_code": item["a_source_code"],
                "a_source_url": item["a_source_url"],
                "b_project_name": item["b_project_name"],
                "b_deadline": item["b_deadline"],
                "b_source_code": item["b_source_code"],
                "b_source_url": item["b_source_url"],
            }
            for item in items
        ],
    )
    return snapshot_id


def fetch_latest_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM recon_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()


def fetch_snapshot_items(
    conn: sqlite3.Connection, snapshot_id: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM recon_snapshot_items WHERE snapshot_id = ? ORDER BY id",
            (snapshot_id,),
        )
    )


def fetch_reconciliation_runs(
    conn: sqlite3.Connection, limit: int = 20
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )
