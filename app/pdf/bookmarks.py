"""R1 书签承前缩放。

判定对象：大纲项中指向本文档页面的目标。缩放因子为 `null` 即承前缩放。

**所有目标类型都要转**：`/Fit`、`/FitH`、`/FitR` 这些本身没有缩放位，
必须换类型改写成 `[页 /XYZ 左 上 null]`（规则见 `destinations` 模块文档）。
第一版把这类目标记成「不适用」整批跳过，等于用户要求「全部改成承前缩放」
却一条都没改。

豁免按**目标页**判断（AC-R10-E2）：书签本身挂在哪一页无关紧要，
它跳到哪一页才决定要不要改。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterator

import pikepdf
from pikepdf import Dictionary

from app.pdf.context import OpContext
from app.pdf.destinations import (
    UNKNOWN_FIT,
    XYZ,
    fit_breakdown,
    fit_name,
    resolve_dest,
    to_inherit_xyz,
    write_dest,
)
from app.pdf.outcome import OpOutcome, new_counts

__all__ = [
    "process",
    "iter_outline_items",
    "dest_slot",
    "count_explicit_zoom",
    "summarize",
]

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


def summarize(items: list[Any], ctx: OpContext) -> dict[str, Any]:
    """统计 before/after 实测值：书签数、带显式缩放数、各 fit 类型条数。"""
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
    return {
        "bookmarks": len(items),
        "explicit_zoom": explicit,
        "fit_breakdown": fit_breakdown(items, dest_slot, ctx),
    }


def count_explicit_zoom(items: list[Any], ctx: OpContext) -> tuple[int, int]:
    """返回 (书签数, 带显式缩放的书签数)。旧入口，保留为薄封装。"""
    summary = summarize(items, ctx)
    return summary["bookmarks"], summary["explicit_zoom"]


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    items = list(iter_outline_items(pdf))
    if not items:
        empty = {"bookmarks": 0, "explicit_zoom": 0, "fit_breakdown": {}}
        return OpOutcome(
            before=dict(empty),
            after=dict(empty),
            counts=new_counts(),
            reason_code="no_outline",
            message="文档没有书签，无需调整",
        )

    before = summarize(items, ctx)
    counts = new_counts()
    warnings: list[str] = []
    shared_copies = 0
    converted_fits: Counter[str] = Counter()
    rect_dropped = 0

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

        # 只有 /XYZ + null 才是承前缩放；/Fit 之类一律要改写
        if resolved.already_inherit:
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
            mode = write_dest(slot, key, to_inherit_xyz(resolved), resolved)
        except Exception as exc:  # noqa: BLE001 - 单个书签失败不能拖垮整项
            counts["failed"] += 1
            warnings.append(
                f"书签「{_title(item)}」无法安全拆分共享目标：{type(exc).__name__}"
            )
            continue
        if mode == "copied":
            shared_copies += 1
        if resolved.fit_type != XYZ:
            converted_fits[resolved.fit_type or UNKNOWN_FIT] += 1
            if resolved.fit_type == "/FitR":
                rect_dropped += 1
        counts["modified"] += 1

    after = summarize(items, ctx)

    outcome = OpOutcome(
        before=before,
        after=after,
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
        outcome.reason_code = "fit_converted" if converted_fits else "explicit_zoom_removed"
        outcome.message = f"{counts['modified']} 个书签已改为承前缩放"
        if converted_fits:
            detail = "、".join(
                f"{fit_name(fit)} {count} 个" for fit, count in converted_fits.most_common()
            )
            outcome.message += f"，其中原本是视图模式的：{detail}"
        if shared_copies:
            outcome.warn(
                f"其中 {shared_copies} 个书签与其他位置共用同一个目标，已复制后再改，未影响其他引用"
            )
        if rect_dropped:
            outcome.warn(
                f"有 {rect_dropped} 个书签原本用「适合矩形区域」定位，"
                f"承前缩放无法表达矩形范围，已改为只保留目标页"
            )
    elif counts["compliant"]:
        # 有已符合项时报「已是承前缩放」，不能因为夹着几个豁免项就报成「全部豁免」
        outcome.reason_code = "already_inherit"
        if counts["exempt"]:
            outcome.message = (
                f"{counts['compliant']} 个书签已是承前缩放，"
                f"{counts['exempt']} 个指向豁免页未做修改"
            )
        else:
            outcome.message = "所有书签已是承前缩放"
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
