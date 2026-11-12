"""校验模块共用的小工具。

只放**纯函数**，不碰 PDF、不读写文件——这样任何一个 check 想复用都行，
也不会因为共用状态让"单项隔离"（AC-R13-E2）失效。
"""

from __future__ import annotations

from typing import Iterable

__all__ = ["page_ranges", "truncate"]

#: 明细里 URI 的截断长度（AC-R16-N1 的「超长截断到 120 字符」）。
URI_LIMIT = 120


def page_ranges(numbers: Iterable[int]) -> str:
    """把页码集合压成 `1-5、8、10-12`。

    报告里连着 40 个页码排成一行没人看得下去，但也不能只写「若干页」——
    用户要拿这份明细去逐页核对，页号必须一个不少。
    """
    ordered = sorted(set(numbers))
    if not ordered:
        return ""

    parts: list[str] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = value
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return "、".join(parts)


def truncate(text: str, limit: int = URI_LIMIT) -> str:
    """超长文本截断，末尾用省略号，保证长度不超过 `limit`。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"
