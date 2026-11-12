"""R35 内链文字着色：把链接矩形覆盖的文字改成蓝色（写内容流）。

v4.0 的 `link_color` 只改注释 `/C`（链接边框颜色）。阅读器默认不显示链接边框，
所以用户看不到任何变化——「处理项内链颜色并未修正」这个反馈就是这么来的。
这个模块补的正是缺的那一半：**真的去改文字的填充色**。

做法是纯内容流手术，不改文字本身：

1. 收集页面上所有 `Link` 注释的 `/Rect`（PDF 用户空间）。
2. 顺序走一遍内容流，维护文本状态（`Tm` / `Tlm` / 字体 / 字号 / 字距 / 水平缩放）
   与图形状态栈（`q` / `Q` / `cm`），对每个文字绘制操作（`Tj` / `TJ` / `'` / `"`）
   算出它的**起点**在用户空间的位置。
3. 起点落在某个链接矩形内的绘制操作，在它前面插 `0 0 1 rg`，画完再插一条
   「恢复原色」的指令。

**为什么是「前置 rg + 画完恢复」而不是 `q … Q`**：PDF 规范不允许 `q` / `Q` 出现在
`BT` / `ET` 内部，MuPDF（以及多数阅读器）会直接忽略这对状态保存/恢复，于是夹在中间
的 `rg` 也跟着失效——实测把 `BT … q 0 0 1 rg (Page 1) Tj Q … ET` 写进文件，
`fitz` 读出来的 span 颜色仍是 `0x000000`。改成 `BT … 0 0 1 rg (Page 1) Tj 0 g … ET`
（不带 `q` / `Q`）立刻读到 `0x0000ff`。所以这里只插颜色指令，恢复靠**原样重放**
绘制前那条设置颜色的指令（`g` / `rg` / `k` / `sc` / `scn`，必要时连 `cs` 一起重放）。

**「目录」二字为什么不会变色**：目录标题不是链接，它的绘制操作起点不在任何链接
矩形内，压根不会被插 `rg`。这条不靠特判，靠几何关系自然成立。

几条刻意的取舍：

- **取不到字宽就不推进**。同一个 `BT` 里连着多个 `Tj` 时，第二个 `Tj` 的起点取决于
  第一个画了多宽。字宽来自字体 `/Widths`（简单字体）或 `/W`（Type0）；两样都没有
  （典型是手写的最小 PDF，`/BaseFont /Helvetica` 却不带 `/Widths`）时**不去猜**，
  原地不动并记一条 warning。猜出来的宽度会把起点算偏，结果是「该蓝的没蓝、
  不该蓝的蓝了」——比漏改更难查。
- **不重写没命中任何链接的页**。页面内容流一个字节都不动，这样「正文没被碰过」
  就是文件层面的事实，而不只是我们的声明。
- **单页失败不拖垮整项**。某页内容流解析不出来（例如 `/Filter` 与实际字节不符）
  → 该页跳过并记 `failed`，其余页照常处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pikepdf
from pikepdf import Array, Dictionary, String

from app.pdf.context import OpContext
from app.pdf.links import iter_links
from app.pdf.outcome import OpOutcome, new_counts

__all__ = [
    "TARGET_RGB",
    "TEXT_NOTE",
    "LinkText",
    "PageAnalysis",
    "analyze_page",
    "color_page",
    "describe",
    "link_rects",
    "process",
]

#: 目标填充色：RGB 纯蓝。
TARGET_RGB: tuple[float, float, float] = (0.0, 0.0, 1.0)

#: 结果页必须附带的说明（AC-R35-E2）。放这里而不是前端：报告与界面共用同一句话。
TEXT_NOTE = (
    "本项把链接覆盖范围内的文字改为蓝色（RGB 0,0,255），并同步链接边框颜色；"
    "目录标题等非链接文字保持原色。"
)

#: 字宽缺失时的提示。说清后果，否则用户不知道「有几处可能没改到」。
NO_WIDTHS_NOTE = "该页字体缺少字宽信息，同一行内多次绘制文字的位置无法推算，可能有个别文字未着色"

#: 无文字层的页：链接上没有文字可着色，不是失败。
NO_TEXT_NOTE = "该页无文字层（可能是扫描页），链接没有文字可着色"

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_SHOW_OPS = frozenset({"Tj", "TJ", "'", '"'})
#: 非描边色指令。`cs` 单独处理（它只换色空间，不改具体颜色）。
_FILL_OPS = frozenset({"g", "rg", "k", "sc", "scn"})
#: 描边色指令，与 `_FILL_OPS` 一一对应。
_STROKE_OPS = frozenset({"G", "RG", "K", "SC", "SCN"})
#: 文本渲染模式里「会描边」的那几档：1 描边、2 填充+描边、5 描边+裁剪、6 三者兼有。
_STROKE_MODES = frozenset({1, 2, 5, 6})
#: `/Resources` 逐级往上找时最多爬几层，防止坏文件里的 `/Parent` 成环。
_MAX_RESOURCE_HOPS = 32
#: 判断「这个颜色算不算蓝」的容差。
_TOLERANCE = 1e-6


# --------------------------------------------------------------------------- #
# 矩阵
# --------------------------------------------------------------------------- #


def _mul(left: Matrix, right: Matrix) -> Matrix:
    """`left × right`：先应用 `right`，再应用 `left`。"""
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _point(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _num(value: Any) -> float | None:
    """把操作数读成浮点；读不出来返回 `None`（调用方按「不认识」处理）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _matrix(operands: Sequence[Any]) -> Matrix | None:
    if len(operands) != 6:
        return None
    values = [_num(item) for item in operands]
    if any(item is None for item in values):
        return None
    return tuple(values)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 颜色状态
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Paint:
    """一条「设置颜色」的指令原样快照，用于画完把颜色放回去。

    `space` 非空表示当前色空间是显式 `cs` / `CS` 选出来的（常见于 ICCBased），
    恢复时必须先重放 `cs` 再重放 `sc` / `scn`，否则颜色分量会落到错误的色空间上。
    """

    op: str
    operands: tuple[float, ...]
    space: str | None = None

    @property
    def blue(self) -> bool:
        """是不是纯蓝。只有三分量且等于 (0,0,1) 才算——`g` 与 `k` 天然不成立。"""
        if len(self.operands) != 3:
            return False
        return all(
            abs(value - target) < _TOLERANCE
            for value, target in zip(self.operands, TARGET_RGB)
        )


#: PDF 的初始非描边色：DeviceGray 黑。
DEFAULT_FILL = _Paint("g", (0.0,))
#: PDF 的初始描边色：同样是 DeviceGray 黑。
DEFAULT_STROKE = _Paint("G", (0.0,))
#: 要插进去的两条目标指令。
TARGET_FILL = _Paint("rg", TARGET_RGB)
TARGET_STROKE = _Paint("RG", TARGET_RGB)


# --------------------------------------------------------------------------- #
# 字宽
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FontMetrics:
    """只够算「画了多宽」的最小字体信息。宽度单位是 1/1000 文字空间。"""

    widths: dict[int, float]
    default: float
    code_bytes: int

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default)


def _widths_table(widths: Any, first_char: int) -> dict[int, float] | None:
    if not isinstance(widths, Array):
        return None
    table: dict[int, float] = {}
    for offset, item in enumerate(widths):
        value = _num(item)
        if value is not None:
            table[first_char + offset] = value
    return table


def _simple_metrics(font: Any) -> _FontMetrics | None:
    table = _widths_table(font.get("/Widths"), int(_num(font.get("/FirstChar")) or 0))
    if table is None:
        return None
    descriptor = font.get("/FontDescriptor")
    missing = 0.0
    if isinstance(descriptor, Dictionary):
        missing = _num(descriptor.get("/MissingWidth")) or 0.0
    return _FontMetrics(table, missing, 1)


def _composite_metrics(font: Any) -> _FontMetrics | None:
    """Type0：宽度在 `/DescendantFonts[0]` 的 `/W` 里，缺省 `/DW`（默认 1000）。"""
    descendants = font.get("/DescendantFonts")
    if not isinstance(descendants, Array) or not descendants:
        return None
    descendant = descendants[0]
    if not isinstance(descendant, Dictionary):
        return None
    default = _num(descendant.get("/DW"))
    table: dict[int, float] = {}
    entries = descendant.get("/W")
    if isinstance(entries, Array):
        items = list(entries)
        index = 0
        while index < len(items):
            first = _num(items[index])
            if first is None or index + 1 >= len(items):
                break
            following = items[index + 1]
            if isinstance(following, Array):
                for offset, item in enumerate(following):
                    value = _num(item)
                    if value is not None:
                        table[int(first) + offset] = value
                index += 2
                continue
            last = _num(following)
            value = _num(items[index + 2]) if index + 2 < len(items) else None
            if last is None or value is None:
                break
            for code in range(int(first), int(last) + 1):
                table[code] = value
            index += 3
    return _FontMetrics(table, 1000.0 if default is None else default, 2)


def font_metrics(font: Any) -> _FontMetrics | None:
    """读字体的字宽表；读不出来返回 `None`（调用方选择「不推进 + 告警」）。"""
    if not isinstance(font, Dictionary):
        return None
    subtype = str(font.get("/Subtype") or "")
    if subtype == "/Type0":
        return _composite_metrics(font)
    return _simple_metrics(font)


def _codes(value: Any, code_bytes: int) -> list[int]:
    """把字符串操作数拆成字符码。Type0 是双字节码，其余是单字节。"""
    if not isinstance(value, String):
        return []
    raw = bytes(value)
    if code_bytes == 2:
        if len(raw) % 2:
            raw = raw[:-1]
        return [raw[index] << 8 | raw[index + 1] for index in range(0, len(raw), 2)]
    return list(raw)


# --------------------------------------------------------------------------- #
# 文本状态
# --------------------------------------------------------------------------- #


@dataclass
class _TextState:
    ctm: Matrix = IDENTITY
    tm: Matrix = IDENTITY
    tlm: Matrix = IDENTITY
    font: Any = None
    font_size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    hscale: float = 1.0
    leading: float = 0.0
    render_mode: int = 0
    fill: _Paint = DEFAULT_FILL
    stroke: _Paint = DEFAULT_STROKE

    def copy(self) -> "_TextState":
        return _TextState(
            ctm=self.ctm,
            tm=self.tm,
            tlm=self.tlm,
            font=self.font,
            font_size=self.font_size,
            char_spacing=self.char_spacing,
            word_spacing=self.word_spacing,
            hscale=self.hscale,
            leading=self.leading,
            render_mode=self.render_mode,
            fill=self.fill,
            stroke=self.stroke,
        )


@dataclass(frozen=True)
class TextRun:
    """一次文字绘制操作：在内容流里的下标、起点、以及当时的填充/描边色。"""

    index: int
    x: float
    y: float
    fill: _Paint
    stroke: _Paint
    strokes: bool

    @property
    def blue(self) -> bool:
        """可见颜色是不是已经全蓝。描边模式下要连描边色一起看。"""
        if not self.fill.blue:
            return False
        return not self.strokes or self.stroke.blue


def _page_resources(page_obj: Any) -> Any:
    """`/Resources` 可以挂在页树祖先上，逐级往上找。"""
    node = page_obj
    for _ in range(_MAX_RESOURCE_HOPS):
        if node is None:
            return None
        resources = node.get("/Resources")
        if resources is not None:
            return resources
        node = node.get("/Parent")
    return None


def _font_of(resources: Any, name: Any) -> Any:
    if not isinstance(resources, Dictionary):
        return None
    fonts = resources.get("/Font")
    if not isinstance(fonts, Dictionary):
        return None
    return fonts.get(str(name))


def _advance(state: _TextState, metrics: _FontMetrics | None, payload: Any) -> float | None:
    """算出一次绘制操作推进了多少文字空间单位；算不出来返回 `None`。"""
    tokens: list[Any]
    if isinstance(payload, Array):
        tokens = list(payload)
    else:
        tokens = [payload]

    total = 0.0
    for token in tokens:
        if isinstance(token, String):
            codes = _codes(token, metrics.code_bytes if metrics else 1)
            if metrics is None:
                if codes:
                    return None
                continue
            for code in codes:
                total += (
                    metrics.width(code) / 1000.0 * state.font_size + state.char_spacing
                    + (state.word_spacing if code == 32 else 0.0)
                ) * state.hscale
            continue
        adjustment = _num(token)
        if adjustment is None:
            return None
        total -= adjustment / 1000.0 * state.font_size * state.hscale
    return total


def _paint(operator: str, operands: Sequence[Any], space: str | None) -> _Paint | None:
    """把一条设置颜色的指令读成 `_Paint`；分量读不出来返回 `None`。"""
    values: list[float] = []
    for item in operands:
        value = _num(item)
        if value is None:
            return None
        values.append(value)
    return _Paint(operator, tuple(values), space)


def _walk(instructions: list[Any], resources: Any) -> tuple[list[TextRun], list[str]]:
    """走一遍内容流，产出所有文字绘制操作的起点，以及过程中发现的告警。"""
    runs: list[TextRun] = []
    warnings: list[str] = []
    state = _TextState()
    stack: list[_TextState] = []
    no_widths = False
    fill_space: str | None = None
    stroke_space: str | None = None

    for index, item in enumerate(instructions):
        operator = str(item.operator)
        operands = item.operands

        if operator == "q":
            stack.append(state.copy())
            continue
        if operator == "Q":
            if stack:
                state = stack.pop()
            continue
        if operator == "cm":
            matrix = _matrix(operands)
            if matrix is not None:
                state.ctm = _mul(matrix, state.ctm)
            continue
        if operator == "BT":
            state.tm = state.tlm = IDENTITY
            continue
        if operator == "Tf":
            if len(operands) >= 2:
                state.font = _font_of(resources, operands[0])
                size = _num(operands[1])
                state.font_size = 0.0 if size is None else size
            continue
        if operator == "Tc":
            state.char_spacing = (_num(operands[0]) or 0.0) if operands else 0.0
            continue
        if operator == "Tw":
            state.word_spacing = (_num(operands[0]) or 0.0) if operands else 0.0
            continue
        if operator == "Tz":
            percent = _num(operands[0]) if operands else None
            state.hscale = 1.0 if percent is None else percent / 100.0
            continue
        if operator == "TL":
            state.leading = (_num(operands[0]) or 0.0) if operands else 0.0
            continue
        if operator == "Tr":
            mode = _num(operands[0]) if operands else None
            state.render_mode = 0 if mode is None else int(mode)
            continue
        if operator == "Tm":
            matrix = _matrix(operands)
            if matrix is not None:
                state.tm = state.tlm = matrix
            continue
        if operator in ("Td", "TD"):
            if len(operands) < 2:
                continue
            tx = _num(operands[0]) or 0.0
            ty = _num(operands[1]) or 0.0
            if operator == "TD":
                state.leading = -ty
            state.tlm = _mul((1.0, 0.0, 0.0, 1.0, tx, ty), state.tlm)
            state.tm = state.tlm
            continue
        if operator == "T*":
            state.tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), state.tlm)
            state.tm = state.tlm
            continue
        if operator == "cs":
            fill_space = str(operands[0]) if operands else None
            # `cs` 会把当前色重置为该空间的初始色，具体是什么得看空间定义 → 记成「未知」
            state.fill = _Paint("scn", (), fill_space)
            continue
        if operator == "CS":
            stroke_space = str(operands[0]) if operands else None
            state.stroke = _Paint("SCN", (), stroke_space)
            continue
        if operator in _FILL_OPS:
            paint = _paint(operator, operands, fill_space if operator in ("sc", "scn") else None)
            if paint is not None:
                state.fill = paint
            continue
        if operator in _STROKE_OPS:
            paint = _paint(
                operator, operands, stroke_space if operator in ("SC", "SCN") else None
            )
            if paint is not None:
                state.stroke = paint
            continue
        if operator not in _SHOW_OPS:
            continue

        # `'` 与 `"` 自带一次换行（等价于先 T*），起点必须在换行之后算
        if operator == '"' and len(operands) >= 3:
            state.word_spacing = _num(operands[0]) or 0.0
            state.char_spacing = _num(operands[1]) or 0.0
        if operator in ("'", '"'):
            state.tlm = _mul((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), state.tlm)
            state.tm = state.tlm

        x, y = _point(_mul(state.tm, state.ctm), 0.0, 0.0)
        runs.append(
            TextRun(
                index=index,
                x=x,
                y=y,
                fill=state.fill,
                stroke=state.stroke,
                strokes=state.render_mode in _STROKE_MODES,
            )
        )

        payload = operands[-1] if operands else None
        metrics = font_metrics(state.font)
        advance = _advance(state, metrics, payload)
        if advance is None:
            if not no_widths:
                no_widths = True
                warnings.append(NO_WIDTHS_NOTE)
            continue
        state.tm = _mul((1.0, 0.0, 0.0, 1.0, advance, 0.0), state.tm)

    return runs, warnings


# --------------------------------------------------------------------------- #
# 链接矩形
# --------------------------------------------------------------------------- #


def link_rects(annots: Sequence[Any]) -> list[tuple[float, float, float, float] | None]:
    """逐条读 `/Rect`；结构不对的位置放 `None`（调用方按「该链接没有矩形」处理）。"""
    rects: list[tuple[float, float, float, float] | None] = []
    for annot in annots:
        raw = annot.get("/Rect") if isinstance(annot, Dictionary) else None
        if not isinstance(raw, Array) or len(raw) != 4:
            rects.append(None)
            continue
        values = [_num(item) for item in raw]
        if any(value is None for value in values):
            rects.append(None)
            continue
        x0, y0, x1, y1 = (float(value) for value in values)  # type: ignore[misc]
        rects.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return rects


def _inside(rect: tuple[float, float, float, float], x: float, y: float) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


# --------------------------------------------------------------------------- #
# 单页分析
# --------------------------------------------------------------------------- #


@dataclass
class PageAnalysis:
    """一页的着色现状与（可选的）待执行计划。"""

    page_number: int
    links: int
    runs: int = 0
    failed: bool = False
    error: str | None = None
    #: 逐条链接：`True` = 矩形内文字已全蓝，`False` = 有非蓝文字，`None` = 矩形内没有文字
    states: list[bool | None] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 需要着色的绘制操作（只含当前不是蓝色的那些）
    targets: list[TextRun] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return self.runs > 0


def analyze_page(pdf: pikepdf.Pdf, page_index: int, annots: Sequence[Any]) -> PageAnalysis:
    """只读分析：这一页的链接矩形里现在是什么颜色。"""
    page = pdf.pages[page_index]
    analysis = PageAnalysis(page_number=page_index + 1, links=len(annots))
    rects = link_rects(annots)

    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都只影响这一页
        analysis.failed = True
        analysis.error = f"{type(exc).__name__}: {exc}"
        analysis.states = [None] * len(annots)
        return analysis

    runs, warnings = _walk(instructions, _page_resources(page.obj))
    analysis.runs = len(runs)
    analysis.warnings.extend(warnings)

    for rect in rects:
        if rect is None:
            analysis.states.append(None)
            continue
        inside = [run for run in runs if _inside(rect, run.x, run.y)]
        if not inside:
            analysis.states.append(None)
            continue
        analysis.states.append(all(run.blue for run in inside))

    # 需要着色的绘制操作：起点落在任意链接矩形内、且当前不是蓝色
    if any(state is False for state in analysis.states):
        analysis.targets = [
            run
            for run in runs
            if not run.blue
            and any(rect is not None and _inside(rect, run.x, run.y) for rect in rects)
        ]
    return analysis


# --------------------------------------------------------------------------- #
# 单页着色
# --------------------------------------------------------------------------- #


def _instruction(operands: Sequence[Any], operator: str) -> Any:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(operator))


def _set_paint(out: list[Any], paint: _Paint) -> None:
    """写出一条把颜色设成 `paint` 的指令；显式色空间要先重放 `cs` / `CS`。"""
    if paint.space is not None:
        out.append(_instruction([pikepdf.Name(paint.space)], "cs" if paint.op in _FILL_OPS else "CS"))
        if not paint.operands:
            # 原指令只是 `cs`，它已经把颜色重置成该空间的初始值，重放 `cs` 就够了
            return
    out.append(_instruction(paint.operands, paint.op))


def _wrap(instructions: list[Any], targets: Sequence[TextRun]) -> bytes:
    """在命中的绘制操作前后插入颜色指令，其余指令原样保留。

    命中处先插目标蓝，画完把绘制前的颜色原样重放回去。相邻命中不重复插入——
    上一条已经设成蓝色时直接沿用，避免内容流无谓膨胀。
    """
    wanted = {run.index: run for run in targets}
    out: list[Any] = []
    fill_now: _Paint | None = None
    stroke_now: _Paint | None = None

    for index, item in enumerate(instructions):
        run = wanted.get(index)
        if run is None:
            out.append(item)
            continue

        if not run.fill.blue:
            if fill_now is None or not fill_now.blue:
                _set_paint(out, TARGET_FILL)
                fill_now = TARGET_FILL
        if run.strokes and not run.stroke.blue:
            if stroke_now is None or not stroke_now.blue:
                _set_paint(out, TARGET_STROKE)
                stroke_now = TARGET_STROKE
        out.append(item)

        if not run.fill.blue:
            _set_paint(out, run.fill)
            fill_now = run.fill
        if run.strokes and not run.stroke.blue:
            _set_paint(out, run.stroke)
            stroke_now = run.stroke

    return pikepdf.unparse_content_stream(out)


def color_page(pdf: pikepdf.Pdf, analysis: PageAnalysis) -> int:
    """按分析结果改写这一页的内容流；返回实际着色的绘制操作数。

    只读分析说「没有要改的」时**一个字节都不写**——正文没被碰过这件事，
    应该是文件层面的事实，而不是我们嘴上说的。
    """
    if analysis.failed or not analysis.targets:
        return 0
    page = pdf.pages[analysis.page_number - 1]
    instructions = list(pikepdf.parse_content_stream(page))
    data = _wrap(instructions, analysis.targets)
    page.obj["/Contents"] = pdf.make_stream(data)
    return len(analysis.targets)


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #


def _annot_blue(annot: Any) -> bool:
    raw = annot.get("/C") if isinstance(annot, Dictionary) else None
    if not isinstance(raw, Array):
        return False
    values = [_num(item) for item in raw]
    if any(value is None for value in values):
        return False
    return _Paint("rg", tuple(values)).blue


def describe(pdf: pikepdf.Pdf) -> dict[str, int]:
    """当前文档的实测快照：链接数、文字已蓝数、边框已蓝数。"""
    by_page = _group_by_page(pdf)

    blue_text = 0
    blue_annot = 0
    for page_number, annots in by_page.items():
        analysis = analyze_page(pdf, page_number - 1, annots)
        for annot, state in zip(annots, analysis.states):
            if state is True:
                blue_text += 1
            if _annot_blue(annot):
                blue_annot += 1
    return {
        "links": sum(len(items) for items in by_page.values()),
        "blue_text": blue_text,
        "blue_annot": blue_annot,
        "text_recolored": 0,
        "annot_recolored": 0,
    }


def _set_annot_blue(annot: Any) -> bool:
    """把 `/C` 设为纯蓝；返回是否真的改了。结构异常时抛 `ValueError`。"""
    raw = annot.get("/C")
    if isinstance(raw, String):
        raise ValueError("颜色值不是数字数组")
    if raw is not None and not isinstance(raw, Array):
        raise ValueError("颜色值不是数字数组")
    if _annot_blue(annot):
        return False
    annot["/C"] = Array(list(TARGET_RGB))
    return True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def _group_by_page(pdf: pikepdf.Pdf) -> dict[int, list[Any]]:
    by_page: dict[int, list[Any]] = {}
    for page_number, annot in iter_links(pdf):
        by_page.setdefault(page_number, []).append(annot)
    return by_page


def broken_links(pdf: pikepdf.Pdf) -> list[tuple[int, Any]]:
    """缺 `/Subtype` 但明显是链接的注释（有 `/Dest` 或 `/GoTo` 动作）。

    `iter_links` 只认 `"/Subtype /Link"`，这类对象会被它整个漏掉。
    漏掉等于「静默不改」——用户看到链接没变蓝，会以为工具坏了。
    所以单独捞出来，计入失败数并在明细里说明。
    """
    found: list[tuple[int, Any]] = []
    for index, page in enumerate(pdf.pages, start=1):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        for annot in annots:
            if not isinstance(annot, Dictionary):
                continue
            if annot.get("/Subtype") is not None:
                continue
            action = annot.get("/A")
            has_goto = (
                isinstance(action, Dictionary) and str(action.get("/S") or "") == "/GoTo"
            )
            if "/Dest" in annot or has_goto:
                found.append((index, annot))
    return found


def _pages_text(pages: Sequence[int]) -> str:
    """把页号列表写成「1、3、5」；空列表写成「—」，避免出现「第  页」。"""
    if not pages:
        return "—"
    return "、".join(str(page) for page in pages)


def process(pdf: pikepdf.Pdf, ctx: OpContext) -> OpOutcome:
    """两条腿一起走：文字填充色（内容流）+ 注释 `/C`（边框）。

    R35 没有豁免语义：`link_color` 不在 `EXEMPT_SENSITIVE_OPERATIONS` 里，
    豁免页上的链接同样要改色。
    """
    by_page = _group_by_page(pdf)
    broken = broken_links(pdf)
    if not by_page and not broken:
        empty = {
            "links": 0,
            "blue_text": 0,
            "blue_annot": 0,
            "text_recolored": 0,
            "annot_recolored": 0,
        }
        outcome = OpOutcome(
            before=dict(empty),
            after=dict(empty),
            counts=new_counts(),
            reason_code="no_link",
            message="文档没有链接注释，无需修正颜色",
        )
        # AC-R35-E2 要求结果页恒定附带说明，没有链接也不例外：
        # 「这次什么都没做」和「这次做了什么」用同一句话解释，用户不必去猜。
        outcome.warn(TEXT_NOTE)
        return outcome

    before = describe(pdf)
    counts = new_counts()
    warnings: list[str] = []
    text_recolored = 0
    annot_recolored = 0
    failed_pages: list[int] = []
    no_text_pages: list[int] = []

    for page_number in sorted(by_page):
        annots = by_page[page_number]
        analysis = analyze_page(pdf, page_number - 1, annots)
        for warning in analysis.warnings:
            if warning not in warnings:
                warnings.append(warning)

        if analysis.failed:
            failed_pages.append(page_number)
            counts["checked"] += len(annots)
            counts["failed"] += len(annots)
            warnings.append(
                f"第 {page_number} 页的内容流无法解析，已跳过该页（其余页照常处理）"
            )
            continue

        if not analysis.has_text:
            # 没有文字层（典型是扫描页）：链接上没有可着色的文字。
            # 整页原样保留——连 `/C` 也不动，这样「未做修改」是文件层面的事实。
            no_text_pages.append(page_number)
            counts["checked"] += len(annots)
            counts["not_applicable"] += len(annots)
            continue

        text_recolored += color_page(pdf, analysis)

        for annot, state in zip(annots, analysis.states):
            counts["checked"] += 1
            try:
                annot_changed = _set_annot_blue(annot)
            except ValueError:
                counts["failed"] += 1
                warnings.append(
                    f"第 {page_number} 页有一个链接的颜色值结构异常，已跳过该链接"
                )
                continue
            annot_recolored += int(annot_changed)

            if state is False or annot_changed:
                counts["modified"] += 1
            elif state is True:
                counts["compliant"] += 1
            else:
                counts["not_applicable"] += 1

    if no_text_pages:
        warnings.append(f"{NO_TEXT_NOTE}：第 {_pages_text(no_text_pages)} 页")

    for page_number, _annot in broken:
        counts["checked"] += 1
        counts["failed"] += 1
        failed_pages.append(page_number)
        warnings.append(f"第 {page_number} 页有一个链接注释缺少 /Subtype，已跳过")

    failed_pages = sorted(set(failed_pages))

    after = describe(pdf)
    after["text_recolored"] = text_recolored
    after["annot_recolored"] = annot_recolored

    outcome = OpOutcome(
        before=before,
        after=after,
        counts=counts,
        warnings=warnings,
    )
    outcome.warn(TEXT_NOTE)

    if counts["failed"]:
        outcome.reason_code = "link_color_partial"
        detail = f"{counts['failed']} 条未能处理"
        if failed_pages:
            detail += f"（第 {_pages_text(failed_pages)} 页需人工检查）"
        outcome.message = f"{counts['modified']} 条链接已改为蓝色，{detail}"
        outcome.next_action = "请人工检查提示中列出的页面与链接，其余页面不受影响"
    elif counts["modified"]:
        outcome.reason_code = "link_color_adjusted"
        outcome.message = (
            f"{counts['modified']} 条链接已改为蓝色"
            f"（文字着色 {text_recolored} 处，边框颜色调整 {annot_recolored} 条）"
        )
    elif counts["compliant"]:
        outcome.reason_code = "already_blue"
        outcome.message = f"所有 {counts['compliant']} 条链接的文字与边框已是蓝色"
    elif counts["not_applicable"]:
        outcome.reason_code = "no_text_layer"
        outcome.message = "链接上没有可着色的文字，未做修改"
        outcome.next_action = "若这些页面是扫描件，请人工确认链接文字颜色是否需要处理"
    else:
        outcome.reason_code = "no_link"
        outcome.message = "文档没有链接注释，无需修正颜色"
    return outcome
