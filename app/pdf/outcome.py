"""处理器统一返回结构与状态判定。

九个处理器都返回同一个 `OpOutcome`。状态**只在这里**判定，
避免每个处理器各写一套「看起来成功」的规则——假成功基本都是从这种分散判定里长出来的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas import COUNT_KEYS, empty_counts, new_operation_result

__all__ = [
    "OpOutcome",
    "new_counts",
    "decide_status",
]


def new_counts(**values: int) -> dict[str, int]:
    """构造六元组计数；写错键名直接报错，不静默丢数。"""
    counts = empty_counts()
    for key, value in values.items():
        if key not in counts:
            raise KeyError(f"未知计数项：{key}")
        counts[key] = int(value)
    return counts


def decide_status(counts: dict[str, int]) -> str:
    """由计数推导状态。

    判定顺序是有意为之：
    1. 只要有失败就报 `failed`——哪怕改成功了一部分，也不能算完成；
    2. 有修改就是 `adjusted`；
    3. 什么都没检查到 → `not_applicable`；
    4. 检查过且全部已符合或豁免 → `unchanged`；
    5. 其余（全部不适用）→ `not_applicable`。
    """
    if counts["failed"]:
        return "failed"
    if counts["modified"]:
        return "adjusted"
    if counts["checked"] == 0:
        return "not_applicable"
    if counts["compliant"] + counts["exempt"] >= counts["checked"]:
        return "unchanged"
    return "not_applicable"


@dataclass
class OpOutcome:
    """单项处理的完整结论。"""

    before: Any = None
    after: Any = None
    counts: dict[str, int] = field(default_factory=empty_counts)
    status: str | None = None
    reason_code: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    next_action: str | None = None
    # 是否真的改动了候选文件。None 表示按 modified 计数推断。
    changed: bool | None = None

    def __post_init__(self) -> None:
        for key in COUNT_KEYS:
            self.counts.setdefault(key, 0)
        if self.status is None:
            self.status = decide_status(self.counts)
        if self.changed is None:
            self.changed = self.counts["modified"] > 0

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def to_result(
        self,
        key: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        """转成 `OperationResult`（字段与 schemas 的约定逐项对齐）。"""
        result = new_operation_result(key, self.status or "unchanged")
        result["before"] = self.before
        result["after"] = self.after
        result["counts"] = dict(self.counts)
        result["reason_code"] = self.reason_code
        result["message"] = self.message
        result["warnings"] = list(self.warnings)
        result["next_action"] = self.next_action
        result["started_at"] = started_at
        result["finished_at"] = finished_at
        return result
