"""双源核对：以原公告编号对齐两个来源各自归并后的生效截止时间。

输入是两个来源各自完成抓取与更正归并后的 ``EffectiveDeadline`` 列表
（每个来源内部已按自己的规则得到生效截止时间），本模块只做跨来源对齐与
状态判定，不访问网络、不访问数据库，便于单元测试。

状态：
- ``consistent``     两边都刊载了同一原公告编号，且生效截止时间一致；
- ``conflict``       两边都刊载了同一原公告编号，但生效截止时间不同；
- ``source_a_only``  仅来源 A 刊载（来源 B 缺失）；
- ``source_b_only``  仅来源 B 刊载（来源 A 缺失）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .merge import EffectiveDeadline

CONSISTENT = "consistent"
CONFLICT = "conflict"
SOURCE_A_ONLY = "source_a_only"
SOURCE_B_ONLY = "source_b_only"

STATUS_LABELS = {
    CONSISTENT: "一致",
    CONFLICT: "不同",
    SOURCE_A_ONLY: "仅来源A刊载",
    SOURCE_B_ONLY: "仅来源B刊载",
}


@dataclass(frozen=True)
class SideDeadline:
    """单个来源一侧的生效截止时间信息。"""

    project_name: str
    original_code: str
    deadline: str
    source_code: str
    source_url: str


@dataclass(frozen=True)
class ReconciliationItem:
    project_name: str
    original_code: str
    status: str
    source_a: SideDeadline | None
    source_b: SideDeadline | None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


def _to_side(d: EffectiveDeadline) -> SideDeadline:
    return SideDeadline(
        project_name=d.project_name,
        original_code=d.original_code,
        deadline=d.deadline,
        source_code=d.source_code,
        source_url=d.source_url,
    )


def reconcile(
    side_a: list[EffectiveDeadline], side_b: list[EffectiveDeadline]
) -> list[ReconciliationItem]:
    """按原公告编号对齐两个来源的生效截止时间，输出稳定排序的核对条目。"""
    map_a = {d.original_code: d for d in side_a}
    map_b = {d.original_code: d for d in side_b}

    items: list[ReconciliationItem] = []
    for code in sorted(set(map_a) | set(map_b)):
        a = map_a.get(code)
        b = map_b.get(code)

        if a is not None and b is not None:
            status = CONSISTENT if a.deadline == b.deadline else CONFLICT
            # 项目名称两边可能有细微差异，优先取 A，其次 B
            project_name = a.project_name or b.project_name
        elif a is not None:
            status = SOURCE_A_ONLY
            project_name = a.project_name
        else:
            assert b is not None
            status = SOURCE_B_ONLY
            project_name = b.project_name

        items.append(
            ReconciliationItem(
                project_name=project_name,
                original_code=code,
                status=status,
                source_a=_to_side(a) if a else None,
                source_b=_to_side(b) if b else None,
            )
        )
    return items


def summarize(items: list[ReconciliationItem]) -> dict[str, int]:
    summary = {CONSISTENT: 0, CONFLICT: 0, SOURCE_A_ONLY: 0, SOURCE_B_ONLY: 0}
    for item in items:
        summary[item.status] += 1
    return summary
