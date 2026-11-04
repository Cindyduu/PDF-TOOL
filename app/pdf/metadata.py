"""R6 清除文档描述。

清空 DocInfo（作者、标题、主题、关键字等）与文档级 XMP 元数据流。
「清过了」不算数——最终文件（含线性化之后）还要再验一次，见流水线的收尾校验。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Dictionary

from app.pdf.context import OpContext
from app.pdf.outcome import OpOutcome, new_counts

__all__ = ["process", "describe"]


def describe(pdf: pikepdf.Pdf) -> dict[str, Any]:
    """当前文档描述的实测快照。"""
    info = pdf.trailer.get("/Info")
    fields = sorted(str(key).lstrip("/") for key in info.keys()) if isinstance(info, Dictionary) else []
    page_metadata = sum(
        1 for page in pdf.pages if page.obj.get("/Metadata") is not None
    )
    return {
        "info_fields": fields,
        "has_xmp": pdf.Root.get("/Metadata") is not None,
        "pages_with_xmp": page_metadata,
    }


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    before = describe(pdf)
    counts = new_counts(checked=1)

    if not before["info_fields"] and not before["has_xmp"] and not before["pages_with_xmp"]:
        counts["compliant"] = 1
        return OpOutcome(
            before=before,
            after=describe(pdf),
            counts=counts,
            reason_code="already_clean",
            message="文档本就没有描述信息，无需修改",
        )

    removed_info = 0
    if isinstance(pdf.trailer.get("/Info"), Dictionary):
        del pdf.trailer["/Info"]
        removed_info = 1
    if pdf.Root.get("/Metadata") is not None:
        del pdf.Root["/Metadata"]
    for page in pdf.pages:
        if page.obj.get("/Metadata") is not None:
            del page.obj["/Metadata"]

    counts["modified"] = 1
    after = describe(pdf)
    outcome = OpOutcome(
        before=before,
        after=after,
        counts=counts,
        reason_code="metadata_cleared",
        message="已清空文档描述信息",
    )
    if removed_info:
        outcome.message = f"已删除 DocInfo（{len(before['info_fields'])} 个字段）"
        if before["has_xmp"]:
            outcome.message += "并移除 XMP 元数据流"
    elif before["has_xmp"]:
        outcome.message = "已移除 XMP 元数据流"
    else:
        outcome.message = "已移除页面级 XMP 元数据流"
    return outcome
