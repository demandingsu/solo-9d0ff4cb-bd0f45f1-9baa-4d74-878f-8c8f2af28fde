#!/usr/bin/env python3
"""生成本地静态 HTML 示例公告站，供离线演示采集与双源核对。

生成三个站点（均为纯静态文件，可用任意 HTTP 服务器托管）：

1. demo_site/         来源 A：5 个项目、10 条公告（单源采集的原始示例站）；
2. demo_site_b/       来源 B：同一批市政项目的另一刊载来源，与来源 A 存在
                       截止时间冲突、单边刊载等分歧；
3. demo_site_broken/  页面故障示例：列表页指向一个不存在的详情页（HTTP 404），
                       用于演示双源核对失败时保留上一轮成对快照。

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 来源 A/B 的同一原公告编号内容可以不同，采集与保存按来源隔离。
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

PAGE_SIZE = 4
ROOT_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT_DIR / "demo_site"
OUT_DIR_B = ROOT_DIR / "demo_site_b"
OUT_DIR_BROKEN = ROOT_DIR / "demo_site_broken"


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 来源 A 示例数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
    ),
]

# ---------------------------------------------------------------------------
# 来源 B 示例数据：另一公告平台对同一批项目的刊载
# - 001：截止时间与来源 A 冲突（26 日 09:00 vs 25 日 14:30）
# - 002：两次更正后最终截止时间与来源 A 一致（11-30 10:00）
# - 004：原公告截止时间与来源 A 不同，更正公告不改时间，仍构成冲突
# - 006：仅来源 B 刊载的项目（来源 A 没有）
# - 003、005：来源 B 未刊载（仅来源 A 刊载）
# ---------------------------------------------------------------------------
ANNOUNCEMENTS_B: list[Announcement] = [
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告（公共资源交易平台转载）",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:30",
        deadline="2026-11-05 14:30",
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-29 10:00",
        deadline="2026-11-26 09:00",  # 与来源 A 的 2026-11-25 14:30 冲突
        corrects_code="SZ-2026-001",
        reason="平台同步的更正公告，截止时间以本平台刊载为准。",
    ),
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告（公共资源交易平台转载）",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 11:00",
        deadline="2026-11-12 09:00",
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-23 10:00",
        deadline="2026-11-20 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 14:00",
        deadline="2026-11-30 10:00",  # 与来源 A 最终一致
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
    ),
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告（公共资源交易平台转载）",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 15:00",
        deadline="2026-11-29 16:00",  # 与来源 A 的 2026-11-28 09:30 冲突
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 10:30",
        deadline=None,  # 不改截止时间，沿用来源 B 原公告时间
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
    ),
    # 仅来源 B 刊载：来源 A 完全没有 SZ-2026-006
    Announcement(
        code="SZ-2026-006",
        title="沿江景观带提升工程招标公告",
        kind="original",
        project_name="沿江景观带提升工程",
        published_at="2026-10-18 09:00",
        deadline="2026-12-15 09:00",
    ),
    Announcement(
        code="SZ-2026-006-G1",
        title="沿江景观带提升工程招标更正公告",
        kind="correction",
        project_name="沿江景观带提升工程",
        published_at="2026-11-04 16:20",
        deadline="2026-12-20 10:00",
        corrects_code="SZ-2026-006",
        reason="设计方案优化，开标顺延。",
    ),
]


def render_detail_page(a: Announcement) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def render_list_page(
    items: list[Announcement], page: int, total_pages: int
) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def _index_page() -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n"
    )


def write_site(
    out_dir: Path, announcements: list[Announcement], page_size: int = PAGE_SIZE
) -> None:
    """把一组公告渲染为完整的分页静态站点。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(announcements, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + page_size - 1) // page_size

    for a in ordered:
        (out_dir / a.filename).write_text(render_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * page_size: page * page_size]
        (out_dir / f"list_{page}.html").write_text(
            render_list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (out_dir / "index.html").write_text(_index_page(), encoding="utf-8")
    print(
        f"generated {len(ordered)} detail pages, {total_pages} list pages "
        f"in {out_dir}"
    )


def write_broken_site(out_dir: Path) -> None:
    """故障站点：列表页解析正常，但指向的详情页不存在（抓取详情时 HTTP 404）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    list_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站（故障示例）</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 1/1 页）</h1>
  <ul class="announcement-list" data-page="1">
    <li class="announcement-item" data-code="SZ-2026-001">
      <a class="detail-link" href="missing-detail-404.html">滨河路市政改造工程施工招标公告</a>
      <span class="published-at">2026-10-20 09:00</span>
    </li>
  </ul>
  <nav class="pagination">
    <span class="page-current">1</span>
    <span class="page-next-disabled">下一页</span>
  </nav>
</body>
</html>
"""
    (out_dir / "list_1.html").write_text(list_html, encoding="utf-8")
    (out_dir / "index.html").write_text(_index_page(), encoding="utf-8")
    print(f"generated broken demo site (missing detail page) in {out_dir}")


def main() -> None:
    write_site(OUT_DIR, ANNOUNCEMENTS)
    write_site(OUT_DIR_B, ANNOUNCEMENTS_B)
    write_broken_site(OUT_DIR_BROKEN)


if __name__ == "__main__":
    main()
