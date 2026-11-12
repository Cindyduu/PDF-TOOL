"""R19 批注与高亮校验。

`/Subtype != /Link` 的全部计入——`Link` 是导航对象不是批注（AC-R19-N2），
把它算进来会让每个带目录链接的文件都告警，用户很快就不看了。

本项**只报告现状**，不做任何处理。扁平化（R26）是处理页上的可选项，
默认不勾：它不可逆，而且会改变页面的视觉呈现方式。
"""

from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.checks._common import page_ranges

__all__ = ["run", "GROUPS"]

#: 展示顺序即报告顺序。`其他` 兜底，永远排最后。
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("高亮", ("Highlight", "Underline", "StrikeOut", "Squiggly")),
    ("文本注释", ("Text",)),
    ("图章", ("Stamp",)),
    ("文本框", ("FreeText",)),
    ("手绘", ("Ink",)),
)

_GROUP_OF: dict[str, str] = {
    subtype: name for name, subtypes in GROUPS for subtype in subtypes
}
OTHER = "其他"

#: 无批注时的固定说明（AC-R19-E1）。工具分不清「本来就没有批注」和
#: 「批注刚被扁平化掉」，所以这句话必须无条件写出来，让用户自己判断。
FLATTEN_NOTE = "页面上的标记图形仍在，但已不是批注对象（若此前做过扁平化，这是预期结果）"


def _subtype(annot: Any) -> str:
    value = annot.get("/Subtype")
    return "" if value is None else str(value).lstrip("/")


def run(ctx: CheckContext) -> CheckResult:
    # 分组 -> 页码列表（同页多条只记一次页号，条数另外统计）
    counts: dict[str, int] = {}
    pages: dict[str, list[int]] = {}
    # `/Annots` 结构坏掉的页。悬空引用会被读成 `None`，那不是"没有批注"，
    # 是"这几页根本没看过"——和 R16 的处理保持一致（AC-R16-E1）。
    broken: list[int] = []

    for index, page in enumerate(ctx.pdf.pages, start=1):
        try:
            annots = page.obj.get("/Annots")
        except Exception:  # noqa: BLE001 - 读不出来就记下这一页，别炸整项
            broken.append(index)
            continue
        if annots is None:
            continue
        if not isinstance(annots, Array):
            broken.append(index)
            continue
        unreadable = False
        for annot in annots:
            if not isinstance(annot, Dictionary):
                unreadable = True
                continue
            subtype = _subtype(annot)
            if subtype == "Link":
                continue
            group = _GROUP_OF.get(subtype, OTHER)
            counts[group] = counts.get(group, 0) + 1
            pages.setdefault(group, []).append(index)
        if unreadable:
            broken.append(index)

    broken_note = (
        Finding(
            level="warning",
            message=f"第 {page_ranges(broken)} 页批注读取失败",
            detail="这些页的 /Annots 结构异常，其中的批注对象无法识别，请在原工具中确认",
        )
        if broken
        else None
    )

    total = sum(counts.values())
    if total == 0:
        if broken_note is not None:
            # 有页读不出来就不能断言"没有批注"——那几页根本没看过。
            return make_result(
                "annotations",
                "warning",
                f"未发现可读取的批注，{len(broken)} 页批注读取失败",
                [
                    Finding(
                        level="warning",
                        message="未发现可读取的批注",
                        detail="部分页面批注结构异常，无法确认是否真的没有批注",
                    ),
                    broken_note,
                ],
            )
        return make_result(
            "annotations",
            "pass",
            "未发现批注或高亮",
            [Finding(level="pass", message="文档内没有批注对象", detail=FLATTEN_NOTE)],
        )

    findings: list[Finding] = []
    order = [name for name, _ in GROUPS] + [OTHER]
    for name in order:
        if name not in counts:
            continue
        findings.append(
            Finding(
                level="warning",
                message=f"{name} {counts[name]} 处（第 {page_ranges(pages[name])} 页）",
                detail=f"共 {counts[name]} 处，分布在 {len(set(pages[name]))} 页",
            )
        )
    if broken_note is not None:
        findings.append(broken_note)

    breakdown = "、".join(f"{name} {counts[name]} 处" for name in order if name in counts)
    summary = f"发现 {total} 处批注（{breakdown}）"
    if broken:
        summary += f"，{len(broken)} 页批注读取失败"
    return make_result("annotations", "warning", summary, findings)
