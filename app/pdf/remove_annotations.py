"""R34 清除高亮和批注。

做法：把 `/Annots` 里**白名单内**的注释整条摘掉。不是展平，是删除——
页面上不留任何痕迹，所以这一步不可逆。

| 删除 | 保留 |
| --- | --- |
| `Highlight`、`Underline`、`StrikeOut`、`Squiggly` | `Link`（链接必须保留） |
| `Text`（便签）、`FreeText`（文本框）、`Popup` | `Stamp`（图章） |
| — | `Widget`（表单域，且不动 `/AcroForm /Fields`） |
| — | 其余所有类型（`Ink`、`Square`、`Circle`、`Line`、`Polygon`、`PolyLine`、`FileAttachment`、`Sound` 等） |

**为什么用白名单而不是「非 Link 全删」**：审阅痕迹里图章（`Stamp`）往往是
有法律意义的（批准章、签名章），表单域删了会让文档失去可填写性。宁可少删，
让用户看到「图章保留」这一行，也不要替用户做不可逆的决定。

几条硬规则：

- 受**豁免页**约束：豁免页整页跳过，计入 `exempt`（与扁平化同口径）。
- `/Annots` 不是数组 → 该页跳过并记 `failed`，不整体失败（AC-R34-E1）。
- 删 `Popup` 后，其他注释里指向它的 `/Popup` 键一并摘掉，否则留下悬空引用。
- `/Annots` 变空要整键删除——留一个空数组，部分阅读器会报警告。
- `Widget` 一律保留，`/AcroForm /Fields` 一个节点都不动（AC-R34-E2）。
  前后各数一次字段树，数量变了就整项判失败：字段树被悄悄改过，
  这份文件就不能拿去提交。

「链接一条都不少」不是一句口头承诺（与 `flatten_annotations` 同口径）：
处理前后各取一次链接签名（页码 + 目的地），逐条比对，不一致就判失败。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf.annotations import (
    count_annotations,
    link_signature,
    strip_popup_reference,
)
from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "DELETED_SUBTYPES", "count_removable"]

#: 要删除的注释子类型（不带前导 `/`，与 `_subtype()` 的返回值同形）。
DELETED_SUBTYPES = frozenset(
    {
        "Highlight",
        "Underline",
        "StrikeOut",
        "Squiggly",
        "Text",
        "FreeText",
        "Popup",
    }
)


def _subtype(annot: Any) -> str:
    value = annot.get("/Subtype")
    return str(value).lstrip("/") if value is not None else ""


def count_removable(pdf: pikepdf.Pdf) -> int:
    """全文档白名单内的注释条数（用于 `before`，不参与删除）。"""
    total = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        for annot in annots:
            if isinstance(annot, Dictionary) and _subtype(annot) in DELETED_SUBTYPES:
                total += 1
    return total


def _field_count(pdf: pikepdf.Pdf) -> int:
    """`/AcroForm /Fields` 的节点总数（含 `/Kids` 递归）。

    只数「有没有被删」，不关心顺序：字段树是个树，节点数变了就说明被改过。
    """
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, Dictionary):
        return 0
    fields = acroform.get("/Fields")
    if not isinstance(fields, Array):
        return 0
    return _count_nodes(fields)


def _count_nodes(fields: Array) -> int:
    total = 0
    for field in fields:
        if not isinstance(field, Dictionary):
            continue
        total += 1
        kids = field.get("/Kids")
        if isinstance(kids, Array):
            total += _count_nodes(kids)
    return total


def _widget_count(pdf: pikepdf.Pdf) -> int:
    """页面上 `/Subtype /Widget` 的注释条数。"""
    total = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        for annot in annots:
            if isinstance(annot, Dictionary) and _subtype(annot) == "Widget":
                total += 1
    return total


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    before_total = count_annotations(pdf)
    links_before = link_signature(pdf)
    before = {
        "annotations": before_total,
        "removable": count_removable(pdf),
        "links": len(links_before),
    }
    fields_before = _field_count(pdf)
    widgets_before = _widget_count(pdf)

    counts = new_counts()
    warnings: list[str] = []

    for page_number, page in enumerate(pdf.pages, start=1):
        annots = page.obj.get("/Annots")
        if annots is None:
            continue
        if not isinstance(annots, Array):
            # AC-R34-E1：结构异常只跳过这一页。`checked` 也要跟着加一，
            # 否则「检查数 = 各归宿之和」这条不变量就破了，报告对不上账。
            counts["checked"] += 1
            counts["failed"] += 1
            warnings.append(
                f"第 {page_number} 页的 /Annots 不是数组，已跳过该页（其余页照常处理）"
            )
            continue

        entries = list(annots)
        if not entries:
            continue

        if ctx.is_exempt(page_number):
            counts["checked"] += len(entries)
            counts["exempt"] += len(entries)
            continue

        keep: list[Any] = []
        for annot in entries:
            counts["checked"] += 1
            if not isinstance(annot, Dictionary):
                counts["failed"] += 1
                warnings.append(f"第 {page_number} 页有一个注释不是字典对象，已保留原样")
                keep.append(annot)
                continue
            if _subtype(annot) not in DELETED_SUBTYPES:
                counts["not_applicable"] += 1
                keep.append(annot)
                continue
            if _subtype(annot) == "Popup":
                # 先摘掉别人指向它的 `/Popup`，再删它自己——
                # 顺序反过来的话，被删对象已经不在 `entries` 语义里了，
                # 比对还得靠 objgen，容易漏。
                strip_popup_reference(entries, annot)
            counts["modified"] += 1

        if len(keep) == len(entries):
            continue
        if keep:
            page.obj["/Annots"] = Array(keep)
        else:
            del page.obj["/Annots"]

    after_total = count_annotations(pdf)
    removed = before_total - after_total
    links_after = link_signature(pdf)
    outcome = OpOutcome(
        before=before,
        after={
            "annotations": after_total,
            "links": len(links_after),
            "removed": removed,
        },
        counts=counts,
        warnings=warnings,
    )

    if links_after != links_before:
        # 白名单里根本没有 Link，真发生了说明有别的代码路径动了它。
        # 输出不可信，宁可整项判失败。
        outcome.status = "failed"
        outcome.reason_code = "link_preservation_failed"
        outcome.message = "清除后链接数量或目的地发生变化，本次结果不可信"
        outcome.next_action = "请勿使用该输出文件，用原始文件重新处理"
        outcome.warn(
            f"链接签名在处理前后不一致：处理前 {len(links_before)} 条，"
            f"处理后 {len(links_after)} 条"
        )
        return outcome

    fields_after = _field_count(pdf)
    widgets_after = _widget_count(pdf)
    if fields_after != fields_before or widgets_after != widgets_before:
        outcome.status = "failed"
        outcome.reason_code = "widget_tree_changed"
        outcome.message = "表单域被改动，本次结果不可信"
        outcome.next_action = "请勿使用该输出文件，用原始文件重新处理"
        outcome.warn(
            f"表单结构发生变化：字段树 {fields_before} → {fields_after}，"
            f"页面表单域 {widgets_before} → {widgets_after}"
        )
        return outcome

    if counts["failed"]:
        outcome.reason_code = "annotation_removal_partial"
        outcome.message = f"已清除 {removed} 个高亮/批注，另有部分页面未能处理"
        outcome.next_action = "请按提示核对列出的页面，必要时在源文件中处理后重新上传"
    elif counts["modified"]:
        outcome.reason_code = "annotations_removed"
        outcome.message = f"已清除 {removed} 个高亮/批注，链接与图章保持原样"
    elif counts["exempt"]:
        outcome.reason_code = "all_exempt"
        outcome.message = "注释都在豁免页上，未做清除"
    elif counts["checked"] == 0:
        outcome.reason_code = "no_annotation"
        outcome.message = "文档没有注释，无需清除"
    else:
        outcome.reason_code = "nothing_removable"
        outcome.message = "没有可清除的高亮或批注，其余注释按规则保留"
    return outcome
