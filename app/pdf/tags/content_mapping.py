"""内容流文本定位：算出每条文本绘制指令在页面上的起点，供标签包裹使用。

为什么不能只用 PyMuPDF 的块坐标：`BDC`/`EMC` 必须插在**内容流**里，
而 PyMuPDF 给的是解析后的块，两者之间没有现成的对应关系。
这里用 `pikepdf.parse_content_stream` 跟踪 `CTM`/`Tm`，
把每条 `Tj`/`TJ`/`'`/`"` 的起点算出来，再和块矩形做几何匹配。

坐标系约定：
- 内容流里的坐标是 PDF 用户空间（原点在页面左下、y 向上）；
- PyMuPDF 的块坐标是**未旋转**页面的坐标（原点左上、y 向下）。
`user_to_text_space()` 负责换算：一次 CropBox 平移加一次 y 翻转，与 `/Rotate` 无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pikepdf

__all__ = [
    "TextRun",
    "Matrix",
    "IDENTITY",
    "concat",
    "apply",
    "user_to_text_space",
    "page_crop_box",
    "page_instructions",
    "mcid_of",
    "runs_from_instructions",
    "text_runs",
    "SHOW_TEXT_OPS",
    "TEXT_STATE_OPS",
]

Matrix = tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

SHOW_TEXT_OPS = frozenset({"Tj", "TJ", "'", '"'})

# 包裹文本时允许跨越的指令：只放行文本状态类操作，
# 一旦中间夹了图形操作就不跨过去，避免把无关的绘图圈进结构元素。
TEXT_STATE_OPS = frozenset(
    {
        "BT",
        "ET",
        "Tf",
        "Td",
        "TD",
        "Tm",
        "T*",
        "TL",
        "Tc",
        "Tw",
        "Tz",
        "Ts",
        "Tr",
        "BDC",
        "BMC",
        "EMC",
        "MP",
        "DP",
    }
)


@dataclass(frozen=True)
class TextRun:
    """一条文本绘制指令的起点与字号（用户空间）。"""

    index: int
    x: float
    y: float
    size: float


# --------------------------------------------------------------------------- #
# 矩阵
# --------------------------------------------------------------------------- #


def concat(inner: Matrix, outer: Matrix) -> Matrix:
    """先 `inner` 后 `outer` 的复合。"""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _scale(matrix: Matrix) -> float:
    a, b, c, d, _, _ = matrix
    return abs(a * d - b * c) ** 0.5


def _numbers(values: Iterable[Any], count: int) -> Matrix | None:
    items = list(values)
    if len(items) != count:
        return None
    try:
        numbers = tuple(float(item) for item in items)
    except (TypeError, ValueError):
        return None
    return numbers  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 页面几何
# --------------------------------------------------------------------------- #


def _inherited(page_obj: pikepdf.Object, key: str) -> Any:
    node: Any = page_obj
    for _ in range(32):  # 防御：坏文档里 /Parent 可能成环
        if node is None:
            return None
        value = node.get(key)
        if value is not None:
            return value
        node = node.get("/Parent")
    return None


def page_crop_box(page_obj: pikepdf.Object) -> tuple[float, float, float, float]:
    """页面显示区域：优先 `/CropBox`，回退 `/MediaBox`。"""
    box = _inherited(page_obj, "/CropBox")
    if box is None:
        box = _inherited(page_obj, "/MediaBox")
    numbers = _numbers(box or (), 4)
    if numbers is None:
        return (0.0, 0.0, 612.0, 792.0)
    x0, y0, x1, y1 = numbers
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def user_to_text_space(
    x: float,
    y: float,
    crop: tuple[float, float, float, float],
) -> tuple[float, float]:
    """PDF 用户空间 → PyMuPDF 文本坐标空间（原点左上、y 向下、**不旋转**）。

    这里**刻意不做 `/Rotate` 换算**。PyMuPDF 的 `get_text("dict")` 返回的是
    未旋转页面的坐标：`/Rotate 90` 的页面上，一条水平写的文字，其块矩形仍是
    宽而矮的横条，x 也在未旋转页宽之内。若在这里按 `/Rotate` 再转一次，
    坐标会被推到页面外面，匹配必然失败（实测：`/Rotate 90` 页面上
    用户空间 (72, 760) 的文本，块矩形在 (72, 66.9)-(156, 86.2)）。
    两个空间只差一次 y 翻转与 CropBox 平移。
    """
    cx0, cy0, cx1, cy1 = crop
    return (x - cx0, cy1 - y)


# --------------------------------------------------------------------------- #
# 文本指令定位
# --------------------------------------------------------------------------- #


def mcid_of(instruction: Any) -> int | None:
    """取出 `BDC` 指令里的 `/MCID`；不是标记指令就返回 `None`。"""
    if str(instruction.operator) != "BDC":
        return None
    operands = instruction.operands
    if len(operands) < 2:
        return None
    properties = operands[1]
    if not isinstance(properties, pikepdf.Dictionary):
        return None
    value = properties.get("/MCID")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def page_instructions(page: pikepdf.Page | pikepdf.Object) -> list[Any]:
    """解析一页的内容流；坏流返回空列表（没有可定位的文本）。

    注意 `parse_content_stream` 是 pikepdf 的**模块级函数**，不是 `Pdf` 的方法；
    传页面进去时它会把 `/Contents` 数组自动合并成一条流来解析。
    """
    target = page.obj if isinstance(page, pikepdf.Page) else page
    try:
        return list(pikepdf.parse_content_stream(target))
    except Exception:  # noqa: BLE001 - 内容流坏了就没有可定位的文本
        return []


def text_runs(page: pikepdf.Page | pikepdf.Object) -> list[TextRun]:
    """按内容流顺序返回每条文本绘制指令的起点。"""
    return runs_from_instructions(page_instructions(page))


def runs_from_instructions(instructions: list[Any]) -> list[TextRun]:
    """在已解析的指令序列上算文本起点。

    生成标签时要先剔除旧的 MCID 标记再定位文本，所以这里必须能接受
    「调用方自己维护的那一份指令列表」——否则 `TextRun.index` 会对不上。
    """
    ctm: Matrix = IDENTITY
    saved: list[Matrix] = []
    tm: Matrix = IDENTITY
    line: Matrix = IDENTITY
    font_size = 0.0
    leading = 0.0
    runs: list[TextRun] = []

    for index, instruction in enumerate(instructions):
        operator = str(instruction.operator)
        operands = instruction.operands

        if operator == "q":
            saved.append(ctm)
        elif operator == "Q":
            if saved:
                ctm = saved.pop()
        elif operator == "cm":
            matrix = _numbers(operands, 6)
            if matrix is not None:
                ctm = concat(matrix, ctm)
        elif operator == "BT":
            tm = IDENTITY
            line = IDENTITY
        elif operator == "Tf":
            if len(operands) >= 2:
                try:
                    font_size = float(operands[1])
                except (TypeError, ValueError):
                    font_size = 0.0
        elif operator == "Td":
            offset = _numbers(operands, 2)
            if offset is not None:
                line = concat((1.0, 0.0, 0.0, 1.0, offset[0], offset[1]), line)
                tm = line
        elif operator == "TD":
            offset = _numbers(operands, 2)
            if offset is not None:
                leading = -offset[1]
                line = concat((1.0, 0.0, 0.0, 1.0, offset[0], offset[1]), line)
                tm = line
        elif operator == "Tm":
            matrix = _numbers(operands, 6)
            if matrix is not None:
                tm = matrix
                line = matrix
        elif operator == "TL":
            if operands:
                try:
                    leading = float(operands[0])
                except (TypeError, ValueError):
                    leading = 0.0
        elif operator == "T*":
            line = concat((1.0, 0.0, 0.0, 1.0, 0.0, -leading), line)
            tm = line
        elif operator in SHOW_TEXT_OPS:
            if operator in ("'", '"'):
                line = concat((1.0, 0.0, 0.0, 1.0, 0.0, -leading), line)
                tm = line
            transform = concat(tm, ctm)
            x, y = apply(transform, 0.0, 0.0)
            runs.append(
                TextRun(
                    index=index,
                    x=x,
                    y=y,
                    size=font_size * _scale(transform),
                )
            )

    return runs
