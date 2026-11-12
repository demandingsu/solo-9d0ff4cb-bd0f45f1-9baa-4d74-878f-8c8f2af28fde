"""双源核对端到端测试。

用线程内静态 HTTP 服务器分别托管三个示例站：
- demo_site        来源 A（5 个项目）
- demo_site_b      来源 B（冲突 / 一致 / 单边刊载）
- demo_site_broken 故障站（详情页 404）

覆盖：对齐状态、按来源隔离存储、失败保留上一轮成对快照、
幂等核对不产生重复数据、核对纯函数的顺序无关性。
"""
from __future__ import annotations

import functools
import http.server
import importlib
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


class StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, directory, **kwargs):
        super().__init__(*args, directory=str(directory), **kwargs)

    def log_message(self, *args):  # 静默
        pass


def _start_server(directory: Path):
    handler = functools.partial(StaticHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return f"http://{host}:{port}", httpd


@pytest.fixture(scope="session")
def servers():
    base_a, httpd_a = _start_server(ROOT / "demo_site")
    base_b, httpd_b = _start_server(ROOT / "demo_site_b")
    base_broken, httpd_broken = _start_server(ROOT / "demo_site_broken")
    yield base_a, base_b, base_broken
    for httpd in (httpd_a, httpd_b, httpd_broken):
        httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


def reconcile(c, url_a, url_b):
    resp = c.post(
        "/api/reconciliations",
        json={"source_a_url": url_a, "source_b_url": url_b},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def item_map(payload):
    return {item["original_code"]: item for item in payload["items"]}


# ---------------------------------------------------------------------------
def test_reconciliation_happy_path_and_statuses(client, servers):
    c, _, _ = client
    base_a, base_b, _ = servers
    payload = reconcile(c, f"{base_a}/list_1.html", f"{base_b}/list_1.html")

    assert payload["status"] == "success"
    assert payload["snapshot_id"] is not None
    items = item_map(payload)

    # 6 个原公告编号：A 的 001-005 + B 独有的 006
    assert set(items) == {
        "SZ-2026-001",
        "SZ-2026-002",
        "SZ-2026-003",
        "SZ-2026-004",
        "SZ-2026-005",
        "SZ-2026-006",
    }

    # 001：截止时间冲突，两边都带各自来源链接
    i1 = items["SZ-2026-001"]
    assert i1["status"] == "conflict"
    assert i1["status_label"] == "不同"
    assert i1["source_a"]["deadline"] == "2026-11-25 14:30"
    assert i1["source_a"]["source_code"] == "SZ-2026-001-G1"
    assert i1["source_a"]["source_url"].endswith("SZ-2026-001-G1.html")
    assert i1["source_b"]["deadline"] == "2026-11-26 09:00"
    assert i1["source_b"]["source_url"].endswith("SZ-2026-001-G1.html")

    # 002：两次更正后最终一致
    i2 = items["SZ-2026-002"]
    assert i2["status"] == "consistent"
    assert i2["status_label"] == "一致"
    assert i2["source_a"]["deadline"] == "2026-11-30 10:00"
    assert i2["source_b"]["deadline"] == "2026-11-30 10:00"

    # 003、005：仅来源 A 刊载
    for code in ("SZ-2026-003", "SZ-2026-005"):
        only_a = items[code]
        assert only_a["status"] == "source_a_only"
        assert only_a["source_a"] is not None
        assert only_a["source_b"] is None

    # 004：来源 B 原公告时间不同且其更正不改时间，仍为冲突
    i4 = items["SZ-2026-004"]
    assert i4["status"] == "conflict"
    assert i4["source_a"]["deadline"] == "2026-11-28 09:30"
    assert i4["source_b"]["deadline"] == "2026-11-29 16:00"

    # 006：仅来源 B 刊载
    i6 = items["SZ-2026-006"]
    assert i6["status"] == "source_b_only"
    assert i6["source_a"] is None
    assert i6["source_b"]["deadline"] == "2026-12-20 10:00"
    assert i6["source_b"]["source_code"] == "SZ-2026-006-G1"

    assert payload["summary"] == {
        "consistent": 1,
        "conflict": 2,
        "source_a_only": 2,
        "source_b_only": 1,
        "total": 6,
    }


def test_latest_endpoint_returns_published_snapshot(client, servers):
    c, _, _ = client
    base_a, base_b, _ = servers
    reconcile(c, f"{base_a}/list_1.html", f"{base_b}/list_1.html")

    resp = c.get("/api/reconciliations/latest")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert len(payload["items"]) == 6
    assert payload["source_a_url"].endswith("list_1.html")


def test_latest_endpoint_empty_before_any_run(client):
    c, _, _ = client
    resp = c.get("/api/reconciliations/latest")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "no_snapshot"
    assert payload["snapshot_id"] is None
    assert payload["items"] == []
    assert payload["summary"]["total"] == 0


def test_source_isolated_storage_and_idempotency(client, servers):
    c, _, database = client
    base_a, base_b, _ = servers
    url_a, url_b = f"{base_a}/list_1.html", f"{base_b}/list_1.html"
    reconcile(c, url_a, url_b)
    first = c.get("/api/reconciliations/latest").json()

    # 再核对一轮：整源替换，不产生重复公告，发布新快照
    reconcile(c, url_a, url_b)
    second = c.get("/api/reconciliations/latest").json()
    assert second["snapshot_id"] != first["snapshot_id"]
    assert len(second["items"]) == 6

    from app.reconciliation_service import source_id_for

    with database.get_conn() as conn:
        rows_a = database.fetch_recon_announcements(conn, source_id_for(url_a))
        rows_b = database.fetch_recon_announcements(conn, source_id_for(url_b))
        assert len(rows_a) == 10
        assert len(rows_b) == 9

        # 同一公告编号两边内容不同，必须各存一份（按来源隔离）
        a1 = next(r for r in rows_a if r["code"] == "SZ-2026-001-G1")
        b1 = next(r for r in rows_b if r["code"] == "SZ-2026-001-G1")
        assert a1["deadline"] != b1["deadline"]
        assert a1["detail_url"] != b1["detail_url"]

        # 存储层联合唯一键保证同来源同编号只有一行
        dup = conn.execute(
            """
            SELECT source_id, code, COUNT(*) FROM recon_announcements
            GROUP BY source_id, code HAVING COUNT(*) > 1
            """
        ).fetchall()
        assert dup == []

        # 双源数据与单源 announcements 表完全隔离
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0

        snapshots = conn.execute("SELECT COUNT(*) FROM recon_snapshots").fetchone()[0]
        assert snapshots == 2
        # 每个快照都是完整的 6 条成对结果
        counts = conn.execute(
            "SELECT snapshot_id, COUNT(*) FROM recon_snapshot_items GROUP BY snapshot_id"
        ).fetchall()
        assert [row[1] for row in counts] == [6, 6]

    runs = c.get("/api/reconciliation-runs").json()
    assert [r["status"] for r in runs] == ["success", "success"]


def test_failed_reconciliation_keeps_last_paired_snapshot(client, servers):
    c, _, database = client
    base_a, base_b, base_broken = servers

    # 第一轮：A + B 成功，发布成对快照
    good = reconcile(c, f"{base_a}/list_1.html", f"{base_b}/list_1.html")
    good_snapshot_id = good["snapshot_id"]

    # 第二轮：B 换成故障站（详情页 404），整体 422
    resp = c.post(
        "/api/reconciliations",
        json={
            "source_a_url": f"{base_a}/list_1.html",
            "source_b_url": f"{base_broken}/list_1.html",
        },
    )
    assert resp.status_code == 422
    assert "missing-detail-404" in resp.json()["detail"] or "HTTP 404" in resp.json()["detail"]

    # 关键：查询仍是上一轮成对结果，绝不会一新一旧
    latest = c.get("/api/reconciliations/latest").json()
    assert latest["status"] == "success"
    assert latest["snapshot_id"] == good_snapshot_id
    assert len(latest["items"]) == 6
    assert latest["source_b_url"].endswith("list_1.html")

    # 故障轮次被记录；故障来源没有写入任何公告数据
    runs = c.get("/api/reconciliation-runs").json()
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"]
    from app.reconciliation_service import source_id_for

    with database.get_conn() as conn:
        broken_rows = database.fetch_recon_announcements(
            conn, source_id_for(f"{base_broken}/list_1.html")
        )
        assert broken_rows == []

    # 恢复后可再次成功发布
    recovered = reconcile(c, f"{base_a}/list_1.html", f"{base_b}/list_1.html")
    assert recovered["status"] == "success"
    assert len(recovered["items"]) == 6


def test_reconcile_pure_function_order_and_labels():
    from app.merge import EffectiveDeadline
    from app.reconcile import (
        CONFLICT,
        CONSISTENT,
        SOURCE_A_ONLY,
        SOURCE_B_ONLY,
        reconcile,
        summarize,
    )

    a = [
        EffectiveDeadline("项目甲", "X-1", "2026-12-01 10:00", "X-1", "http://a/X-1"),
        EffectiveDeadline("项目乙", "X-2", "2026-12-02 10:00", "X-2", "http://a/X-2"),
    ]
    b = [
        EffectiveDeadline("项目甲（转载）", "X-1", "2026-12-03 10:00", "X-1", "http://b/X-1"),
        EffectiveDeadline("项目丙", "X-3", "2026-12-04 10:00", "X-3", "http://b/X-3"),
    ]
    items = reconcile(list(reversed(a)), list(reversed(b)))
    by_code = {i.original_code: i for i in items}
    assert [i.original_code for i in items] == ["X-1", "X-2", "X-3"]
    assert by_code["X-1"].status == CONFLICT
    assert by_code["X-2"].status == SOURCE_A_ONLY
    assert by_code["X-3"].status == SOURCE_B_ONLY
    assert by_code["X-1"].project_name == "项目甲"  # 名称分歧时取 A

    summary = summarize(items)
    assert summary == {
        CONSISTENT: 0,
        CONFLICT: 1,
        SOURCE_A_ONLY: 1,
        SOURCE_B_ONLY: 1,
    }
