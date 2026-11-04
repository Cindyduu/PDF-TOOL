"""R9 链接承前缩放。

只处理页面 `Link` 注释中指向本文档页面的目标。
URI 链接没有页面缩放语义，原样保留（AC-R9-N2），且必须仍然可点击。

豁免按**链接所在页**判断——与书签（按目标页）不同，这一点容易写混。
"""

from __future__ import annotations

from typing import Any, Iterator

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf.context import OpContext
from app.pdf.destinations import XYZ, resolve_dest, write_inherit_zoom
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "iter_links", "link_dest_slot"]


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


def _count_explicit(links: list[tuple[int, Any]], ctx: OpContext) -> int:
    explicit = 0
    for _page, annot in links:
        slot, key = link_dest_slot(annot)
        if slot is None:
            continue
        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            continue
        if resolved.fit_type == XYZ and resolved.zoom is not None:
            explicit += 1
    return explicit


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    links = list(iter_links(pdf))
    if not links:
        return OpOutcome(
            before={"links": 0, "internal": 0, "explicit_zoom": 0},
            after={"links": 0, "internal": 0, "explicit_zoom": 0},
            counts=new_counts(),
            reason_code="no_link",
            message="文档没有链接注释，无需调整",
        )

    before_explicit = _count_explicit(links, ctx)
    counts = new_counts()
    warnings: list[str] = []
    internal = 0
    shared_copies = 0

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

        internal += 1
        resolved = resolve_dest(slot[key], ctx.named, ctx.resolver)
        if resolved is None:
            counts["failed"] += 1
            warnings.append(f"第 {page_number} 页有一个链接的目标无法解析，已保留原样")
            continue

        if resolved.fit_type != XYZ:
            counts["not_applicable"] += 1
            continue

        if resolved.zoom is None:
            counts["compliant"] += 1
            continue

        if resolved.page_number is None:
            counts["failed"] += 1
            warnings.append(f"第 {page_number} 页有一个链接的目标页无法确定，未改动")
            continue

        try:
            mode = write_inherit_zoom(slot, key, resolved)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            warnings.append(
                f"第 {page_number} 页的链接无法安全拆分共享目标：{type(exc).__name__}"
            )
            continue
        if mode == "copied":
            shared_copies += 1
        counts["modified"] += 1

    after_explicit = _count_explicit(links, ctx)
    outcome = OpOutcome(
        before={"links": len(links), "internal": internal, "explicit_zoom": before_explicit},
        after={"links": len(links), "internal": internal, "explicit_zoom": after_explicit},
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
        outcome.reason_code = "explicit_zoom_removed"
        outcome.message = f"{counts['modified']} 个内部链接已改为承前缩放"
        if shared_copies:
            outcome.warn(
                f"其中 {shared_copies} 个链接与其他位置共用同一个目标，已复制后再改，未影响其他引用"
            )
    elif counts["exempt"]:
        outcome.reason_code = "all_exempt"
        outcome.message = f"{counts['exempt']} 个链接位于豁免页，未做修改"
    elif internal == 0:
        outcome.reason_code = "no_internal_link"
        outcome.message = "没有指向本文档页面的链接，URI 链接保持原样"
    else:
        outcome.reason_code = "already_inherit"
        outcome.message = "所有内部链接已是承前缩放"
    return outcome
