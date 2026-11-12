"""R32 目录校验（v4.1 新增，只读）。

用户要求「每个文件的首页需要添加目录，且字体设置为蓝色；内链均为蓝色
（RGB 0 0 255；比如目录，但需要注意，目录 2 字仍为黑色）」。

按 D-10 的裁决，**工具不生成目录**：生成目录要先读懂正文、算出每一章在第几页，
那是另一个产品，而且自动生成的目录条目对 eCTD 审阅者没有帮助（同 §1.3 第 2 条
对书签的处理）。所以这一项只做三件能机器判定的事：

1. **首页有没有目录** —— 首页是否存在 ≥2 条指向本文档其他页的内链；
2. **目录文字是不是蓝色** —— 读 `get_text("dict")` 的 span 颜色，与 `0x0000FF` 比；
3. **每条内链能不能跳转** —— 目的地能否解析到具体页（复用 `destinations`）。

**措辞红线（AC-R32-E1）**：任何结论都不许写「目录正确」。工具没读过正文，
无法确认「1. 引言 …… 第 5 页」这一条写得对不对，只能说「首页有 N 条内链，
颜色均为蓝色」。这条红线在测试里用参数化扫描全部样本守着。

**为什么颜色要看 span 而不是注释 `/C`**：`/C` 是链接边框颜色，阅读器默认不显示，
改它用户看不见——这正是 v4.0 `link_color` 被判「并未修正」的原因。目录要求的是
**文字**是蓝色，所以这里必须读文字填充色。
"""

from __future__ import annotations

import re
from typing import Any

import pymupdf
from pikepdf import Array, Dictionary

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.destinations import PageResolver, collect_named_dests, resolve_dest
from app.pdf.links import link_dest_slot

__all__ = [
    "BLUE",
    "HUMAN_NOTE",
    "MIN_ENTRIES",
    "TOC_KEYWORDS",
    "run",
]

#: 目标蓝：RGB(0, 0, 255)。pymupdf 的 span `color` 是 `0xRRGGBB`。
BLUE = 0x0000FF

#: 少于这么多条内链就不认为首页有目录。一条内链更可能是交叉引用而不是目录。
MIN_ENTRIES = 2

#: 结论固定附注。pass 也必须带——工具核对的是「颜色和可跳转」，不是目录内容。
HUMAN_NOTE = "目录条目与正文页码的一致性需人工确认"

#: 首页出现这些词、却凑不出 2 条内链时，只能说「疑似有目录」。
TOC_KEYWORDS = ("目录", "contents", "table of contents")

#: 非蓝明细最多列这么多条，再多就把报告淹了。
_MAX_LISTED = 10


# --------------------------------------------------------------------------- #
# 结构读取
# --------------------------------------------------------------------------- #


def _link_annots(page_obj: Any) -> list[Any]:
    """首页的 `Link` 注释。结构坏了按「没有」处理，异常留给引擎记 fail。"""
    try:
        annots = page_obj.get("/Annots")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(annots, Array):
        return []
    found: list[Any] = []
    for annot in annots:
        if not isinstance(annot, Dictionary):
            continue
        if str(annot.get("/Subtype") or "") == "/Link":
            found.append(annot)
    return found


def _rect(annot: Any) -> tuple[float, float, float, float] | None:
    """注释矩形，规范化成 `(x0, y0, x1, y1)`（PDF 用户空间，y 向上）。"""
    raw = annot.get("/Rect")
    if not isinstance(raw, Array) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _spans(page: Any) -> list[tuple[int, tuple[float, float, float, float]]]:
    """页面上的文字片段：`(0xRRGGBB 颜色, bbox)`。

    bbox 是 pymupdf 的显示坐标（左上原点、已考虑 `/Rotate`），所以下面
    把注释矩形也转到同一坐标系再比，免得旋转页上颜色全判成「核对不了」。
    """
    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001 - 取不到文字就走「无法核对」分支
        return []
    spans: list[tuple[int, tuple[float, float, float, float]]] = []
    for block in data.get("blocks") or ():
        if block.get("type") != 0:  # 0 = 文字块；1 是图片
            continue
        for line in block.get("lines") or ():
            for span in line.get("spans") or ():
                if not str(span.get("text") or "").strip():
                    continue
                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                spans.append(
                    (
                        int(span.get("color") or 0) & 0xFFFFFF,
                        tuple(float(value) for value in bbox),
                    )
                )
    return spans


def _to_display(page: Any, rect: tuple[float, float, float, float]) -> pymupdf.Rect:
    """PDF 用户空间矩形 → pymupdf 显示坐标矩形。"""
    x0, y0, x1, y1 = rect
    return pymupdf.Rect(x0, y0, x1, y1) * page.transformation_matrix


def _colors_inside(
    box: pymupdf.Rect,
    spans: list[tuple[int, tuple[float, float, float, float]]],
) -> list[int]:
    """落在矩形内的 span 颜色。

    判据是**中心点在内**而不是「有交集」：一行目录文字往往只有一个 span，
    它的 bbox 会略微超出链接矩形；用交集会把相邻行的 span 也捞进来。
    """
    colors: list[int] = []
    for color, bbox in spans:
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        if box.x0 <= cx <= box.x1 and box.y0 <= cy <= box.y1:
            colors.append(color)
    return colors


def _first_page_text(ctx: CheckContext) -> str:
    try:
        return ctx.doc[0].get_text("text") or ""
    except Exception:  # noqa: BLE001
        return ""


def _matched_keyword(text: str) -> str | None:
    """命中的目录关键词，返回**页面上实际出现的那串字**（保留原大小写）。

    两个细节：

    - **最长优先**：「Table of Contents」里也含「contents」，按短的先匹配会把
      结论写成「首页出现「contents」」，读起来像在说别的东西。
    - **回原文而不是回小写规范形**：结论是要给人看的，写「首页出现
      「table of contents」」等于把我们的匹配规则漏进了报告。
    """
    best: tuple[int, str] | None = None
    for keyword in TOC_KEYWORDS:
        match = re.search(re.escape(keyword), text, re.IGNORECASE)
        if match is None:
            continue
        if best is None or len(match.group(0)) > len(best[1]):
            best = (match.start(), match.group(0))
    return best[1] if best is not None else None


def _hex(color: int) -> str:
    return f"#{color:06X}"


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def run(ctx: CheckContext) -> CheckResult:
    total = int(ctx.page_count)
    if total <= 1:
        return make_result(
            "toc",
            "skipped",
            "单页文档无目录",
            [
                Finding(
                    level="skipped",
                    message="单页文档无目录",
                    detail="只有一页的文件不存在目录，本项不适用",
                )
            ],
        )

    pdf = ctx.pdf
    annots = _link_annots(pdf.pages[0].obj)

    resolver = PageResolver(pdf)
    named = collect_named_dests(pdf)

    # entries: `(页内序号, 目标页, 矩形)`，只收能跳到本文档具体页的内链；
    # broken: 是内链但跳不过去。序号跟着注释位置走，不随 `entries` 压缩——
    # 否则第 1 条坏掉之后，明细里的「第 2 条」会指向真正的第 3 条。
    entries: list[tuple[int, int, tuple[float, float, float, float] | None]] = []
    broken: list[int] = []
    for order, annot in enumerate(annots, start=1):
        slot, key = link_dest_slot(annot)
        if slot is None:
            # URI / Launch / Named 之外的动作：不是目录条目
            continue
        resolved = resolve_dest(slot[key], named, resolver)
        if resolved is None or resolved.page_number is None:
            broken.append(order)
            continue
        entries.append((order, resolved.page_number, _rect(annot)))

    total_entries = len(entries) + len(broken)

    if total_entries < MIN_ENTRIES:
        keyword = _matched_keyword(_first_page_text(ctx))
        if keyword:
            return make_result(
                "toc",
                "warning",
                f"首页出现「{keyword}」，但内链不足 {MIN_ENTRIES} 条，无法确认目录可跳转",
                [
                    Finding(
                        level="warning",
                        message=f"首页疑似有目录标题「{keyword}」，内链仅 {total_entries} 条",
                        detail="目录条目应当是指向正文对应页的内链；本工具不生成目录，请人工确认",
                    )
                ],
            )
        return make_result(
            "toc",
            "fail",
            "首页未发现目录",
            [
                Finding(
                    level="fail",
                    message=f"首页内链 {total_entries} 条，未达到目录所需的 {MIN_ENTRIES} 条",
                    detail="首页需要有指向正文各页的内链，且目录文字应为蓝色（RGB 0,0,255）",
                )
            ],
        )

    if broken:
        detail = "、".join(str(item) for item in broken[:_MAX_LISTED])
        findings = [
            Finding(
                level="fail",
                message=f"首页有 {len(broken)} 条内链的目标无法解析",
                detail=f"页内序号：{detail}；这些条目点了不会跳转",
            )
        ]
        findings.extend(_color_findings(ctx, entries))
        return make_result(
            "toc",
            "fail",
            f"首页 {total_entries} 条内链中有 {len(broken)} 条无法跳转",
            findings,
        )

    color_findings, non_blue, unknown = _color_report(ctx, entries)

    if non_blue:
        listed = "、".join(
            f"第 {order} 条（{_hex(color)}）" for order, color in non_blue[:_MAX_LISTED]
        )
        return make_result(
            "toc",
            "warning",
            f"首页 {total_entries} 条内链中有 {len(non_blue)} 条非蓝",
            [
                Finding(
                    level="warning",
                    message=f"{len(non_blue)} 条目录文字不是蓝色（RGB 0,0,255）",
                    detail=f"非蓝条目：{listed}",
                ),
                *color_findings,
            ],
        )

    if unknown:
        return make_result(
            "toc",
            "warning",
            f"首页 {len(unknown)} 条内链覆盖范围内未找到可提取文字，颜色无法核对",
            [
                Finding(
                    level="warning",
                    message=f"{len(unknown)} 条内链的颜色无法自动核对",
                    detail="链接矩形内没有可提取的文字（可能是扫描件或文字被转成图形），"
                    "请人工确认目录文字是否为蓝色",
                )
            ],
        )

    return make_result(
        "toc",
        "pass",
        f"首页 {total_entries} 条内链，颜色均为蓝色",
        [
            Finding(
                level="pass",
                message=f"首页 {total_entries} 条内链，颜色均为蓝色（RGB 0,0,255）",
                detail=HUMAN_NOTE,
            )
        ],
    )


def _color_report(
    ctx: CheckContext,
    entries: list[tuple[int, int, tuple[float, float, float, float] | None]],
) -> tuple[list[Finding], list[tuple[int, int]], list[int]]:
    """逐条核对颜色，返回 `(明细, 非蓝条目, 无法核对条目)`。"""
    page = ctx.doc[0]
    spans = _spans(page)

    findings: list[Finding] = []
    non_blue: list[tuple[int, int]] = []
    unknown: list[int] = []

    if not spans:
        # 整页没有文字层：所有条目都核对不了
        return findings, non_blue, [order for order, _target, _rect in entries]

    for order, _target, rect in entries:
        if rect is None:
            unknown.append(order)
            continue
        colors = _colors_inside(_to_display(page, rect), spans)
        if not colors:
            unknown.append(order)
            continue
        bad = [color for color in colors if color != BLUE]
        if bad:
            non_blue.append((order, bad[0]))

    return findings, non_blue, unknown


def _color_findings(
    ctx: CheckContext,
    entries: list[tuple[int, int, tuple[float, float, float, float] | None]],
) -> list[Finding]:
    """跳转失败时的附注：颜色照常判定，结论里不能只有「跳不过去」。"""
    _findings, non_blue, unknown = _color_report(ctx, entries)
    notes: list[Finding] = []
    if non_blue:
        listed = "、".join(
            f"第 {order} 条（{_hex(color)}）" for order, color in non_blue[:_MAX_LISTED]
        )
        notes.append(
            Finding(
                level="warning",
                message=f"另有 {len(non_blue)} 条目录文字不是蓝色",
                detail=f"非蓝条目：{listed}",
            )
        )
    if unknown:
        notes.append(
            Finding(
                level="warning",
                message=f"另有 {len(unknown)} 条内链的颜色无法自动核对",
                detail="链接矩形内没有可提取的文字",
            )
        )
    return notes
