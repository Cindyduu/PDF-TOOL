"""R9 链接承前缩放。

只处理页面 `Link` 注释中指向本文档页面的目标。
URI 链接没有页面缩放语义，原样保留（AC-R9-N2），且必须仍然可点击。

与书签一样，**所有目标类型都要转**：`/Fit`、`/FitR` 这些没有缩放位，
必须换类型改写成 `[页 /XYZ 左 上 null]`（规则见 `destinations` 模块文档）。

豁免按**链接所在页**判断——与书签（按目标页）不同，这一点容易写混。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterator

import pikepdf
from pikepdf import Array, Dictionary

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

__all__ = ["process", "iter_links", "link_dest_slot", "summarize"]


def iter_links(pdf: pikepdf.Pdf) -> Iterator[tuple[int, Any]]:
    """产出 (1 基页码, 链接注释字典)。"""
    for index, page in enumerate(pdf.pages, start=1):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        for annot in annots:
            if not isinstance(annot, Dictionary):
                continue
            subtype = annot.get("/Subtype")
            if subtype is not None and str(subtype) == "/Link":
                yield index, annot


def link_dest_slot(annot: Any) -> tuple[Any, str] | tuple[None, None]:
    """返回 `(容器, "/Dest")` 或 `(动作字典, "/D")`。"""
    if "/Dest" in annot:
        return annot, "/Dest"
    action = annot.get("/A")
    if isinstance(action, Dictionary):
        subtype = action.get("/S")
        if subtype is not None and str(subtype) == "/GoTo" and "/D" in action:
            return action, "/D"
    return None, None


def _action_kind(annot: Any) -> str | None:
    action = annot.get("/A")
    if not isinstance(action, Dictionary):
        return None
    subtype = action.get("/S")
    return None if subtype is None else str(subtype)


def summarize(links: list[tuple[int, Any]], ctx: OpContext) -> dict[str, Any]:
    """统计 before/after 实测值：链接数、内部链接数、显式缩放数、各 fit 类型条数。"""
    internal = 0
    explicit = 0
    for _page, annot in links:
        slot, key = link_dest_slot(annot)
        if slot is None:
            continue
        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            continue
        internal += 1
        if resolved.fit_type == XYZ and resolved.zoom is not None:
            explicit += 1
    return {
        "links": len(links),
        "internal": internal,
        "explicit_zoom": explicit,
        "fit_breakdown": fit_breakdown(links, _link_slot, ctx),
    }


def _link_slot(entry: tuple[int, Any]) -> tuple[Any, str] | tuple[None, None]:
    """`fit_breakdown` 只按链接注释取槽位，忽略页码。"""
    return link_dest_slot(entry[1])


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    links = list(iter_links(pdf))
    if not links:
        empty = {"links": 0, "internal": 0, "explicit_zoom": 0, "fit_breakdown": {}}
        return OpOutcome(
            before=dict(empty),
            after=dict(empty),
            counts=new_counts(),
            reason_code="no_link",
            message="文档没有链接注释，无需调整",
        )

    before = summarize(links, ctx)
    counts = new_counts()
    warnings: list[str] = []
    shared_copies = 0
    converted_fits: Counter[str] = Counter()
    rect_dropped = 0

    for page_number, annot in links:
        counts["checked"] += 1

        if ctx.is_exempt(page_number):
            counts["exempt"] += 1
            continue

        slot, key = link_dest_slot(annot)
        if slot is None:
            # URI / Launch / Named 等动作没有页面缩放语义
            counts["not_applicable"] += 1
            continue

        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            counts["failed"] += 1
            warnings.append(f"第 {page_number} 页有一个链接的目标无法解析，已保留原样")
            continue

        # 只有 /XYZ + null 才是承前缩放；/Fit 之类一律要改写
        if resolved.already_inherit:
            counts["compliant"] += 1
            continue

        if resolved.page_number is None:
            counts["failed"] += 1
            warnings.append(f"第 {page_number} 页有一个链接的目标页无法确定，未改动")
            continue

        try:
            mode = write_dest(slot, key, to_inherit_xyz(resolved), resolved)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            warnings.append(
                f"第 {page_number} 页的链接无法安全拆分共享目标：{type(exc).__name__}"
            )
            continue
        if mode == "copied":
            shared_copies += 1
        if resolved.fit_type != XYZ:
            converted_fits[resolved.fit_type or UNKNOWN_FIT] += 1
            if resolved.fit_type == "/FitR":
                rect_dropped += 1
        counts["modified"] += 1

    after = summarize(links, ctx)
    outcome = OpOutcome(
        before=before,
        after=after,
        counts=counts,
        warnings=warnings,
    )
    if counts["failed"]:
        outcome.reason_code = "link_zoom_failed"
        outcome.message = (
            f"{counts['failed']} 个链接未能处理，已保留原样；成功调整 {counts['modified']} 个"
        )
        outcome.next_action = "请人工检查提示中列出的链接，或改用无链接的副本重新处理"
    elif counts["modified"]:
        outcome.reason_code = "fit_converted" if converted_fits else "explicit_zoom_removed"
        outcome.message = f"{counts['modified']} 个内部链接已改为承前缩放"
        if converted_fits:
            detail = "、".join(
                f"{fit_name(fit)} {count} 个" for fit, count in converted_fits.most_common()
            )
            outcome.message += f"，其中原本是视图模式的：{detail}"
        if shared_copies:
            outcome.warn(
                f"其中 {shared_copies} 个链接与其他位置共用同一个目标，已复制后再改，未影响其他引用"
            )
        if rect_dropped:
            outcome.warn(
                f"有 {rect_dropped} 个链接原本用「适合矩形区域」定位，"
                f"承前缩放无法表达矩形范围，已改为只保留目标页"
            )
    elif counts["compliant"]:
        outcome.reason_code = "already_inherit"
        if counts["exempt"]:
            outcome.message = (
                f"{counts['compliant']} 个内部链接已是承前缩放，"
                f"{counts['exempt']} 个位于豁免页未做修改"
            )
        else:
            outcome.message = "所有内部链接已是承前缩放"
    elif counts["exempt"]:
        outcome.reason_code = "all_exempt"
        outcome.message = f"{counts['exempt']} 个链接位于豁免页，未做修改"
    elif counts["not_applicable"]:
        outcome.reason_code = "no_internal_link"
        outcome.message = "没有指向本文档页面的链接，URI 链接保持原样"
    else:
        outcome.reason_code = "already_inherit"
        outcome.message = "所有内部链接已是承前缩放"
    return outcome
