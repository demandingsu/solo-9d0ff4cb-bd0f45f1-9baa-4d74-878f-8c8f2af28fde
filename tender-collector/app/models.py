from __future__ import annotations

from pydantic import BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


# ---------------------------------------------------------------------------
# 双源核对
# ---------------------------------------------------------------------------
class ReconcileRequest(BaseModel):
    source_a_url: str = Field(
        ..., min_length=1, description="来源 A 的公告列表页地址（自动跟随分页）"
    )
    source_b_url: str = Field(
        ..., min_length=1, description="来源 B 的公告列表页地址（自动跟随分页）"
    )
    source_a_label: str | None = Field(
        default=None, description="来源 A 的可读名称（可选，仅用于响应展示）"
    )
    source_b_label: str | None = Field(
        default=None, description="来源 B 的可读名称（可选，仅用于响应展示）"
    )


class SideDeadlineOut(BaseModel):
    project_name: str
    original_code: str
    deadline: str
    source_code: str
    source_url: str


class ReconciliationItemOut(BaseModel):
    project_name: str
    original_code: str
    status: str
    status_label: str
    source_a: SideDeadlineOut | None
    source_b: SideDeadlineOut | None


class ReconciliationSummary(BaseModel):
    consistent: int = 0
    conflict: int = 0
    source_a_only: int = 0
    source_b_only: int = 0
    total: int = 0


class ReconciliationResponse(BaseModel):
    status: str  # success | no_snapshot
    snapshot_id: int | None = None
    run_id: int | None = None
    created_at: str | None = None
    source_a_url: str | None = None
    source_b_url: str | None = None
    source_a_label: str | None = None
    source_b_label: str | None = None
    summary: ReconciliationSummary
    items: list[ReconciliationItemOut]


class ReconciliationRunOut(BaseModel):
    id: int
    source_a_url: str
    source_b_url: str
    status: str
    items_a: int
    items_b: int
    error: str | None
    started_at: str
    finished_at: str
