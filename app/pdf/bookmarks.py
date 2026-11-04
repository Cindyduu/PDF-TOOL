"""R1 书签承前缩放。

判定对象：大纲项中指向本文档页面的目标。缩放因子为 `null` 即承前缩放。

豁免按**目标页**判断（AC-R10-E2）：书签本身挂在哪一页无关紧要，
它跳到哪一页才决定要不要改。
"""

from __future__ import annotations

from typing import Any, Iterator

import pikepdf
from pikepdf import Dictionary

from app.pdf.context import OpContext
from app.pdf.destinations import XYZ, resolve_dest, write_inherit_zoom
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "iter_outline_items", "dest_slot", "count_explicit_zoom"]

_MAX_OUTLINE_NODES = 200_000


def iter_outline_items(pdf: pikepdf.Pdf) -> Iterator[Any]:
    """深度优先遍历大纲项，带环保护（坏文档里 Parent/Next 可能互相指）。"""
    outlines = pdf.Root.get("/Outlines")
    if not isinstance(outlines, Dictionary):
        return
    seen: set[tuple[int, int]] = set()
    stack: list[Any] = [outlines.get("/First")]
    visited = 0
    while stack:
        node = stack.pop()
        while node is not None and isinstance(node, Dictionary):
            key = node.objgen
            if key != (0, 0):
                if key in seen:
                    break
                seen.add(key)
            visited += 1
            if visited > _MAX_OUTLINE_NODES:  # pragma: no cover - 防御
                return
            yield node
            child = node.get("/First")
            if isinstance(child, Dictionary):
                stack.append(child)
            node = node.get("/Next")


def dest_slot(item: Any) -> tuple[Any, str] | tuple[None, None]:
    """找出大纲项里存目的地的槽位与键名。

    返回 `(容器, "/Dest")` 或 `(动作字典, "/D")`；没有页面目的地时返回 `(None, None)`。
    键名带前导斜杠是 PDF 名字的硬性要求，pikepdf 不接受裸字符串。
    """
    if "/Dest" in item:
        return item, "/Dest"
    action = item.get("/A")
    if isinstance(action, Dictionary):
        subtype = action.get("/S")
        if subtype is not None and str(subtype) == "/GoTo" and "/D" in action:
            return action, "/D"
    return None, None


def count_explicit_zoom(items: list[Any], ctx: OpContext) -> tuple[int, int]:
    """返回 (书签数, 带显式缩放的书签数)。用于 before/after 实测值。"""
    explicit = 0
    for item in items:
        slot, key = dest_slot(item)
        if slot is None:
            continue
        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            continue
        if resolved.fit_type == XYZ and resolved.zoom is not None:
            explicit += 1
    return len(items), explicit


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    items = list(iter_outline_items(pdf))
    if not items:
        return OpOutcome(
            before={"bookmarks": 0, "explicit_zoom": 0},
            after={"bookmarks": 0, "explicit_zoom": 0},
            counts=new_counts(),
            reason_code="no_outline",
            message="文档没有书签，无需调整",
        )

    before_total, before_explicit = count_explicit_zoom(items, ctx)
    counts = new_counts()
    warnings: list[str] = []
    shared_copies = 0

    for item in items:
        counts["checked"] += 1
        slot, key = dest_slot(item)
        if slot is None:
            counts["not_applicable"] += 1
            continue

        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            counts["failed"] += 1
            warnings.append(f"书签「{_title(item)}」的目标无法解析，已保留原样")
            continue

        if resolved.fit_type != XYZ:
            # /Fit、/FitH 这类目的地本身没有缩放倍数，改它会连带改变滚动位置
            counts["not_applicable"] += 1
            continue

        if resolved.zoom is None:
            counts["compliant"] += 1
            continue

        if resolved.page_number is None:
            counts["failed"] += 1
            warnings.append(f"书签「{_title(item)}」的目标页无法确定，未改动")
            continue

        if ctx.is_exempt(resolved.page_number):
            counts["exempt"] += 1
            continue

        try:
            mode = write_inherit_zoom(slot, key, resolved)
        except Exception as exc:  # noqa: BLE001 - 单个书签失败不能拖垮整项
            counts["failed"] += 1
            warnings.append(
                f"书签「{_title(item)}」无法安全拆分共享目标：{type(exc).__name__}"
            )
            continue
        if mode == "copied":
            shared_copies += 1
        counts["modified"] += 1

    after_total, after_explicit = count_explicit_zoom(items, ctx)

    outcome = OpOutcome(
        before={"bookmarks": before_total, "explicit_zoom": before_explicit},
        after={"bookmarks": after_total, "explicit_zoom": after_explicit},
        counts=counts,
        warnings=warnings,
    )
    if counts["failed"]:
        outcome.reason_code = "bookmark_zoom_failed"
        outcome.message = (
            f"{counts['failed']} 个书签未能处理，已保留原样；"
            f"成功调整 {counts['modified']} 个"
        )
        outcome.next_action = "请人工检查提示中列出的书签，或改用无书签的副本重新处理"
    elif counts["modified"]:
        outcome.reason_code = "explicit_zoom_removed"
        outcome.message = f"{counts['modified']} 个书签已改为承前缩放"
        if shared_copies:
            outcome.warn(
                f"其中 {shared_copies} 个书签与其他位置共用同一个目标，已复制后再改，未影响其他引用"
            )
    elif counts["exempt"]:
        outcome.reason_code = "all_exempt"
        outcome.message = f"{counts['exempt']} 个书签指向豁免页，未做修改"
    else:
        outcome.reason_code = "already_inherit"
        outcome.message = "所有书签已是承前缩放"
    return outcome


def _title(item: Any) -> str:
    title = item.get("/Title")
    if title is None:
        return "(无标题)"
    text = str(title).strip()
    return text[:40] if text else "(无标题)"
