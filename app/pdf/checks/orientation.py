"""R17 页面方向校验。

要检出的只有一种情况：**横向内容被放进了纵向页面**（常见于把宽表格
贴进 A4 纵向文档）。**不自动旋转**（§1.3 第 7 条）——旋转错了比不旋转更糟，
所以结论一律写成「疑似」+「请人工确认」（AC-R17-E1）。

判定算法固定为 PRD §4 R17 的 5 步，不做自由发挥：

1. `page.rect`（已应用 `/Rotate`）满足 `h <= w` → 本页跳过；
2. 用 `get_text("dict")` 收文本行的 `bbox` 与 `dir`；
3. `p_horizontal` = 宽 > 高×3 的行占比，`p_rotated` = `dir != (1,0)` 的行占比；
4. 无文本行 → 本页 skipped；`p_horizontal < 0.6 且 p_rotated >= 0.6` → 告警；
   文本整体宽度 > 页宽×1.15 → 追加一条告警；
5. 任一页告警 → warning；全部 skipped → skipped；否则 pass。
"""

from __future__ import annotations

from typing import Any

from app.pdf.checks import CheckContext, CheckResult, Finding, make_result
from app.pdf.checks._common import page_ranges

__all__ = ["run", "HORIZONTAL_RATIO", "ROTATED_RATIO", "OVERFLOW_RATIO"]

#: 「这一行是横排的」判定线：宽 > 高 × 3。
HORIZONTAL_ASPECT = 3.0
#: 正常横排行的占比下限。低于它说明这页的文本不像是横排的。
HORIZONTAL_RATIO = 0.6
#: 被旋转过的行占比上限。达到它说明文本整体转了 90°/270°。
ROTATED_RATIO = 0.6
#: 文本整体宽度超过页宽的这个倍数，视为可能溢出页面。
OVERFLOW_RATIO = 1.15

_EPSILON = 1e-6


def _lines(page: Any) -> list[tuple[tuple[float, float, float, float], tuple[float, float]]]:
    """收集所有文本行的 `(bbox, dir)`。"""
    rows: list[tuple[tuple[float, float, float, float], tuple[float, float]]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # 0 = 文本块；图片块没有行
            continue
        for line in block.get("lines", []):
            bbox = line.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            direction = line.get("dir") or (1.0, 0.0)
            rows.append(
                (
                    (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    (float(direction[0]), float(direction[1])),
                )
            )
    return rows


def _is_horizontal_row(bbox: tuple[float, float, float, float]) -> bool:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    return width > height * HORIZONTAL_ASPECT


def _is_rotated(direction: tuple[float, float]) -> bool:
    return (
        abs(direction[0] - 1.0) > _EPSILON or abs(direction[1]) > _EPSILON
    )


def run(ctx: CheckContext) -> CheckResult:
    findings: list[Finding] = []
    warned_pages: list[int] = []
    no_text_pages: list[int] = []
    landscape_pages: list[int] = []
    judged = 0

    for index, page in enumerate(ctx.doc, start=1):
        rect = page.rect
        if rect.height <= rect.width:
            landscape_pages.append(index)
            continue

        rows = _lines(page)
        if not rows:
            no_text_pages.append(index)
            continue

        judged += 1
        total = len(rows)
        p_horizontal = sum(1 for bbox, _ in rows if _is_horizontal_row(bbox)) / total
        p_rotated = sum(1 for _, direction in rows if _is_rotated(direction)) / total

        page_warned = False
        if p_horizontal < HORIZONTAL_RATIO and p_rotated >= ROTATED_RATIO:
            page_warned = True
            findings.append(
                Finding(
                    level="warning",
                    message=f"第 {index} 页疑似横向内容置于纵向页面",
                    page=index,
                    detail=(
                        f"横排文本行占 {p_horizontal:.0%}，被旋转的文本行占 {p_rotated:.0%}；"
                        f"本工具不自动旋转，请人工确认"
                    ),
                )
            )

        left = min(bbox[0] for bbox, _ in rows)
        right = max(bbox[2] for bbox, _ in rows)
        if (right - left) > rect.width * OVERFLOW_RATIO:
            page_warned = True
            findings.append(
                Finding(
                    level="warning",
                    message=f"第 {index} 页内容疑似超出页面宽度",
                    page=index,
                    detail=(
                        f"文本横向跨度 {(right - left):.1f} pt，页宽 {rect.width:.1f} pt；"
                        f"请人工确认是否被裁切"
                    ),
                )
            )

        if page_warned:
            warned_pages.append(index)

    if warned_pages:
        return make_result(
            "page_orientation",
            "warning",
            f"{len(warned_pages)} 页疑似方向或版式异常，请人工确认",
            findings,
        )

    if judged == 0:
        if landscape_pages:
            detail = f"全部 {len(landscape_pages)} 页都是横向页面，无需判定"
            message = "全部页面为横向，跳过方向判定"
        else:
            detail = "没有任何可提取的文本行，扫描件无法判定方向"
            message = "无文本层，无法判定方向"
        return make_result(
            "page_orientation",
            "skipped",
            f"{message}，请人工确认",
            [Finding(level="skipped", message=message, detail=detail)],
        )

    return make_result(
        "page_orientation",
        "pass",
        f"未发现方向异常（{judged} 页参与判定），请人工确认",
        [
            Finding(
                level="pass",
                message=f"{judged} 页文本方向正常",
                detail="本项只做启发式判断，结论请人工确认",
            )
        ],
    )
