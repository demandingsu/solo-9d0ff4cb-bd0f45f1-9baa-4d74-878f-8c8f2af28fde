# 市政工程投标截止时间采集与双源核对服务

面向招投标专员的公告采集与核对 API，提供两套能力：

- **单源采集**：提交市政工程公告站的**列表页地址**，服务自动遍历所有分页与
  详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布时间**
  （而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。
- **双源核对**：传入**两个来源**的列表页 URL，各自沿分页抓取详情、归并更正，
  以**原公告编号**对齐同一批项目，返回两边各自的生效截止时间与来源链接，
  标明**一致 / 不同 / 仅单边刊载**。

## 核心规则

### 单源采集

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

### 双源核对

1. **各自独立抓取归并**：两个来源分别复用单源的分页抓取与更正归并逻辑；
   同一公告编号在两边内容可能不同，**采集和保存严格按来源隔离**
   （`recon_announcements` 以来源标识分区，与单源 `announcements` 表互不相通）。
2. **以原公告编号对齐**：跨来源按原公告编号（而非项目名称）对齐项目，逐条给出
   `consistent`（一致）、`conflict`（截止时间不同）、`source_a_only` /
   `source_b_only`（仅单边刊载）四种状态，并附两边各自的生效截止时间、
   截止时间来源公告编号与来源链接。
3. **成对发布**：一轮核对的两边结果在**同一个数据库事务**内写入并发布为一个
   快照（`recon_snapshots` + `recon_snapshot_items`），要么整体可见要么整体回滚，
   查询接口永远返回成对的一轮结果。
4. **失败保留上一轮**：任一来源读取或归并失败时，本轮不写入任何数据，
   上一轮已发布的成对快照原样保留——查询绝不会出现"一新一旧"。失败原因记入
   `reconciliation_runs` 表并通过 HTTP 422 返回。
5. **幂等核对**：重复核对按来源整源替换，不产生重复公告；每次成功发布一个新快照，
   查询接口始终返回最新一轮。

## 目录结构

```
tender-collector/
├── app/                          # FastAPI 应用
│   ├── main.py                   # 路由：采集 / 项目 / 双源核对 / 运行历史
│   ├── service.py                # 单源采集编排 + 事务边界 + 过期判定
│   ├── reconciliation_service.py # 双源核对编排：两侧抓取归并 + 快照事务
│   ├── scraper.py                # httpx + BeautifulSoup 抓取/解析（两能力共用）
│   ├── merge.py                  # 更正归并与生效截止时间判定（纯函数）
│   ├── reconcile.py              # 双源对齐与一致/冲突/单边状态判定（纯函数）
│   ├── database.py               # SQLite 连接、建表、单源与双源持久化
│   ├── models.py                 # Pydantic 模型
│   └── config.py                 # 环境变量配置
├── demo_site/                    # 来源 A 示例公告站（静态文件）
├── demo_site_b/                  # 来源 B 示例公告站：冲突 + 单边刊载
├── demo_site_broken/             # 页面故障示例：详情页 404
├── scripts/generate_demo_site.py # 三个示例站生成器（数据可复现）
├── tests/test_collection.py      # 单源端到端 + 归并单元测试
├── tests/test_reconciliation.py  # 双源核对端到端 + 对齐单元测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成三个示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000；三个示例站分别在 :8001 / :8002 / :8003
docker compose up --build -d

# 3. 单源采集（来源 A）
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 双源核对：来源 A（demo-site）对来源 B（demo-site-b）
curl -X POST http://localhost:8000/api/reconciliations \
  -H 'Content-Type: application/json' \
  -d '{
        "source_a_url": "http://demo-site/list_1.html",
        "source_b_url": "http://demo-site-b/list_1.html",
        "source_a_label": "市住建局公告栏",
        "source_b_label": "公共资源交易平台"
      }'

# 6. 查询最近一轮已发布的成对核对快照
curl http://localhost:8000/api/reconciliations/latest

# 7. 故障演示：把来源 B 换成故障站（详情页 404），本轮 422，上一轮快照保留
curl -X POST http://localhost:8000/api/reconciliations \
  -H 'Content-Type: application/json' \
  -d '{
        "source_a_url": "http://demo-site/list_1.html",
        "source_b_url": "http://demo-site-broken/list_1.html"
      }'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`、`demo-site-b`、
`demo-site-broken`）；从宿主机浏览则打开 <http://localhost:8001/list_1.html>、
<http://localhost:8002/list_1.html>、<http://localhost:8003/list_1.html>。
SQLite 数据存于命名卷 `tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A/B/C：三个示例公告站（来源 A、来源 B、故障站）
python3 -m http.server 8001 --directory demo_site
python3 -m http.server 8002 --directory demo_site_b
python3 -m http.server 8003 --directory demo_site_broken

# 终端 D：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 E：测试
pytest -q

# 单源采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 双源核对（全程离线，仅访问本地静态站）
curl -X POST http://localhost:8000/api/reconciliations \
  -H 'Content-Type: application/json' \
  -d '{
        "source_a_url": "http://127.0.0.1:8001/list_1.html",
        "source_b_url": "http://127.0.0.1:8002/list_1.html"
      }'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/reconciliations` — 双源核对（成对发布）

请求（`source_a_label` / `source_b_label` 可选，仅用于响应展示）：

```json
{
  "source_a_url": "http://demo-site/list_1.html",
  "source_b_url": "http://demo-site-b/list_1.html",
  "source_a_label": "市住建局公告栏",
  "source_b_label": "公共资源交易平台"
}
```

成功响应（200）：按原公告编号对齐后的逐条核对结果与状态汇总。

```json
{
  "status": "success",
  "snapshot_id": 1,
  "run_id": 1,
  "created_at": "2026-11-12T12:00:00+00:00",
  "source_a_url": "http://demo-site/list_1.html",
  "source_b_url": "http://demo-site-b/list_1.html",
  "source_a_label": "市住建局公告栏",
  "source_b_label": "公共资源交易平台",
  "summary": {
    "consistent": 1,
    "conflict": 2,
    "source_a_only": 2,
    "source_b_only": 1,
    "total": 6
  },
  "items": [
    {
      "project_name": "滨河路市政改造工程",
      "original_code": "SZ-2026-001",
      "status": "conflict",
      "status_label": "不同",
      "source_a": {
        "project_name": "滨河路市政改造工程",
        "original_code": "SZ-2026-001",
        "deadline": "2026-11-25 14:30",
        "source_code": "SZ-2026-001-G1",
        "source_url": "http://demo-site/SZ-2026-001-G1.html"
      },
      "source_b": {
        "project_name": "滨河路市政改造工程",
        "original_code": "SZ-2026-001",
        "deadline": "2026-11-26 09:00",
        "source_code": "SZ-2026-001-G1",
        "source_url": "http://demo-site-b/SZ-2026-001-G1.html"
      }
    }
  ]
}
```

条目状态（`status` / `status_label`）：

| status | 中文 | 含义 |
| --- | --- | --- |
| `consistent` | 一致 | 两边都刊载且生效截止时间相同 |
| `conflict` | 不同 | 两边都刊载但生效截止时间不同（需人工核对） |
| `source_a_only` | 仅来源A刊载 | 来源 B 无此原公告编号 |
| `source_b_only` | 仅来源B刊载 | 来源 A 无此原公告编号 |

单边刊载时，缺失一侧的 `source_a` / `source_b` 为 `null`。

失败响应（422，任一来源读取或归并失败；本轮不写入，上一轮成对快照保留）：

```json
{ "detail": "读取页面失败 http://demo-site-broken/missing-detail-404.html: HTTP 404" }
```

### `GET /api/reconciliations/latest` — 最近一轮核对快照

始终返回一轮**成对**结果；从未成功核对过时返回
`{"status": "no_snapshot", "summary": {...全 0}, "items": []}`。

### `GET /api/reconciliation-runs?limit=20` — 核对运行历史

包含每轮核对的成功/失败状态、两侧采集条数与错误信息。

## 示例站数据设计

### 来源 A：`demo_site/`（5 个项目、10 条公告、3 个列表页）

列表按发布时间**倒序**排列（更正公告排在原公告之前），专门验证归并不受页面
顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

### 来源 B：`demo_site_b/`（另一平台对同一批项目的刊载）

以原公告编号与来源 A 对齐，覆盖全部四种核对状态：

| 原公告编号 | 来源 A 生效截止时间 | 来源 B 生效截止时间 | 核对状态 |
| --- | --- | --- | --- |
| SZ-2026-001 | 2026-11-25 14:30 | 2026-11-26 09:00 | **冲突** |
| SZ-2026-002 | 2026-11-30 10:00 | 2026-11-30 10:00 | 一致 |
| SZ-2026-003 | 2026-10-30 17:00 | （未刊载） | 仅来源 A |
| SZ-2026-004 | 2026-11-28 09:30 | 2026-11-29 16:00 | **冲突**（B 的更正不改时间） |
| SZ-2026-005 | 2026-11-20 15:00 | （未刊载） | 仅来源 A |
| SZ-2026-006 | （未刊载） | 2026-12-20 10:00 | 仅来源 B |

汇总：一致 1、冲突 2、仅来源 A 2、仅来源 B 1，共 6 条。

### 故障站：`demo_site_broken/`

列表页结构正常，但其中的详情链接指向不存在的 `missing-detail-404.html`，
抓取详情时返回 HTTP 404，用于演示"任一来源失败 → 本轮 422 → 上一轮成对快照
原样保留"。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。
