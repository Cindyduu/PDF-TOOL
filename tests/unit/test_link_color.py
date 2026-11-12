"""R35 内链颜色重做——改链接覆盖范围内**文字的填充色**（AC-R35-N1…E2）。

v4.0 的 R25 只改注释 `/C`，而阅读器默认不显示链接边框，所以用户看不到任何变化。
R35 换了做法：解析内容流、算出每个文字绘制操作的起点、把落在链接矩形里的那些
改成蓝色。这个改法很容易「顺手做多」，所以断言分成两组：

**必须变的**（AC-R35-N1 / N6）：链接矩形内的 span 颜色 → `0x0000FF`，`/C` → `[0,0,1]`。

**必须不变的**（AC-R35-N2 / N3 / N4 / N5）：

- 目录标题「Table of Contents」不在任何矩形内 → 仍是 `0x000000`；
- 正文非链接文字逐 span 比对零差异；
- 链接总数与每条 `/Dest` 原样；
- `page.get_text("text")` 逐页相等——只插颜色操作符，不动任何 `Tj` 的操作数。

**为什么不用 `q … Q` 包住 `Tj`**：PDF 不允许 `q`/`Q` 出现在 `BT`/`ET` 内部，MuPDF 会
忽略这对状态栈操作，夹在中间的 `rg` 也跟着失效。这里改成「前置 `rg` + 画完重放原色」。
`test_blue_survives_a_round_trip_through_the_file` 就是钉这条的：只改内存对象不够，
必须落盘再让 MuPDF 读一遍。

**继承自 R25 的三条**（新方案没取消它们）：AC-R25-N3 已符合的不计入修改数、
AC-R25-N4 链接数与目的地不变、AC-R25-E1 单个坏链接不拖垮整项。

全部在内存里改，只有读 span 颜色时才落盘一次。
"""

from __future__ import annotations

import io

import pikepdf
import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name, Stream, String

from app.pdf import content_color, link_color, links
from app.pdf.context import OpContext
from tests.fixtures.make_fixtures import (
    add_page,
    attach_annots,
    dest_xyz,
    make_annot,
    new_pdf,
)
from tests.pdf_ops import annots_of, page_content_bytes

BLUE = [0, 0, 1]
BLUE_SPAN = 0x0000FF
BLACK_SPAN = 0x000000

HEADING = "Table of Contents"
HEADING_Y = 780
ROW_TOP = 700
ROW_GAP = 26


# --------------------------------------------------------------------------- #
# 样本
# --------------------------------------------------------------------------- #


def _row_y(index: int) -> int:
    return ROW_TOP - index * ROW_GAP


def _row_rect(index: int) -> tuple[float, float, float, float]:
    """第 `index` 行目录条目的链接矩形（PDF 用户空间）。"""
    y = _row_y(index)
    return (68.0, float(y - 5), 320.0, float(y + 15))


def _rgb(op: tuple[float, float, float]) -> bytes:
    return b"%g %g %g rg " % op


def _row_ops(index: int, color: tuple[float, float, float] | None) -> bytes:
    prefix = b"" if color is None else _rgb(color)
    return (
        b"BT /F1 12 Tf "
        + prefix
        + b"72 %d Td (Chapter %d ........ %d) Tj ET"
        % (_row_y(index), index + 1, index + 2)
    )


def _build(
    count: int = 12,
    *,
    colors: list[tuple[float, float, float] | None] | None = None,
    annot_colors: list | None = None,
    pages: int = 1,
    footer: bool = True,
):
    """造一份「目录首页 + 正文」的样本。

    首页顶部有一行**不是链接**的标题 `Table of Contents`，下面是 `count` 行目录条目，
    每行都有链接矩形把它整行盖住。`colors[i]` 控制第 `i` 行原本的填充色。

    注意 `colors[i] = None` 的含义是**不写颜色指令**，而不是「黑色」：填充色是图形状态，
    不随 `BT` / `ET` 复位，所以 `None` 行会沿用上一条 `rg` 的颜色。想让某行回到黑色
    必须显式写 `(0.0, 0.0, 0.0)`。
    """
    pdf = new_pdf()
    page_objs = [add_page(pdf) for _ in range(pages)]

    ops = [b"BT /F1 16 Tf 72 %d Td (%s) Tj ET" % (HEADING_Y, HEADING.encode())]
    for index in range(count):
        color = None if colors is None else colors[index]
        ops.append(_row_ops(index, color))
    if footer:
        ops.append(b"BT /F1 12 Tf 72 90 Td (Footer text outside every link) Tj ET")
    page_objs[0].Contents = Stream(pdf, b"\n".join(ops) + b"\n")

    annots = []
    for index in range(count):
        extra: dict = {
            "Dest": dest_xyz(page_objs[min(1, pages - 1)], None),
            "Border": Array([0, 0, 0]),
        }
        if annot_colors is not None and annot_colors[index] is not None:
            extra["C"] = Array(annot_colors[index])
        annots.append(make_annot(pdf, "Link", _row_rect(index), appearance=None, extra=extra))
    if annots:
        attach_annots(pdf, page_objs[0], annots)
    return pdf, page_objs, annots


def _run(pdf: pikepdf.Pdf, exempt: tuple[int, ...] = ()):
    return link_color.process(pdf, OpContext.build(pdf, exempt))


def _to_bytes(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _spans(pdf: pikepdf.Pdf, index: int = 0) -> list[tuple[str, int]]:
    """落盘后用 MuPDF 读 span。**必须落盘**：内存对象对不上阅读器的真实解读。"""
    with pymupdf.open(stream=_to_bytes(pdf), filetype="pdf") as doc:
        return [
            (span["text"], span["color"])
            for block in doc[index].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
        ]


def _text(pdf: pikepdf.Pdf, index: int = 0) -> str:
    with pymupdf.open(stream=_to_bytes(pdf), filetype="pdf") as doc:
        return doc[index].get_text("text")


def _row_span(spans: list[tuple[str, int]], index: int) -> tuple[str, int]:
    needle = f"Chapter {index + 1}"
    for text, color in spans:
        if text.startswith(needle):
            return text, color
    raise AssertionError(f"找不到第 {index + 1} 行的 span：{[s[0] for s in spans]}")


# --------------------------------------------------------------------------- #
# AC-R35-N1：链接矩形内的文字变蓝
# --------------------------------------------------------------------------- #


def test_all_twelve_link_rows_turn_blue():
    pdf, _pages, _annots = _build(12)
    before = _spans(pdf)
    assert [color for _text_value, color in before if color != BLACK_SPAN] == []
    assert len([1 for value, _color in before if value.startswith("Chapter")]) == 12

    outcome = _run(pdf)

    assert outcome.status == "adjusted"
    assert outcome.reason_code == "link_color_adjusted"
    assert outcome.counts == {
        "checked": 12,
        "modified": 12,
        "compliant": 0,
        "exempt": 0,
        "not_applicable": 0,
        "failed": 0,
    }
    for index in range(12):
        _value, color = _row_span(_spans(pdf), index)
        assert color == BLUE_SPAN, f"第 {index + 1} 行没变蓝"


def test_blue_survives_a_round_trip_through_the_file():
    """只改内存对象不够：必须落盘、重开、让 MuPDF 真的读到 `0x0000FF`。

    这条是 R35 的命门。v4.1 早期用 `q … 0 0 1 rg … Q` 包住 `Tj`，内容流写出来
    长得对，MuPDF 读出来却仍是黑——因为 `q`/`Q` 不允许出现在 `BT`/`ET` 内部。
    """
    pdf, _pages, _annots = _build(3)
    _run(pdf)
    data = _to_bytes(pdf)

    with pikepdf.open(io.BytesIO(data)) as reopened:
        stream = page_content_bytes(reopened.pages[0])
    assert b"0 0 1 rg" in stream
    # 不允许出现「把 q/Q 塞进 BT 里」的痕迹
    assert b"q\n0 0 1 rg" not in stream

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        colors = [
            span["color"]
            for block in doc[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span["text"].startswith("Chapter")
        ]
    assert colors == [BLUE_SPAN] * 3


def test_counts_split_text_and_annotation():
    """文字着色与边框改色分开记：两边都可能只成功一半。"""
    pdf, _pages, _annots = _build(4)
    outcome = _run(pdf)

    assert outcome.after["text_recolored"] == 4
    assert outcome.after["annot_recolored"] == 4
    assert outcome.before["blue_text"] == 0
    assert outcome.before["blue_annot"] == 0
    assert outcome.after["blue_text"] == 4
    assert outcome.after["blue_annot"] == 4


# --------------------------------------------------------------------------- #
# AC-R35-N2：目录标题「二字」保持黑色
# --------------------------------------------------------------------------- #


def test_toc_heading_stays_black():
    """标题不是链接，不在任何矩形内 → 不该被着色。靠几何关系，不靠特判。"""
    pdf, _pages, _annots = _build(5)
    _run(pdf)

    spans = _spans(pdf)
    heading = [color for value, color in spans if value == HEADING]
    assert heading == [BLACK_SPAN], "目录标题被误着色"


def test_heading_survives_even_when_it_sits_right_above_the_first_link():
    """标题紧贴第一行链接（y=780 vs 715），仍然不能连坐。"""
    pdf, _pages, _annots = _build(1)
    _run(pdf)
    spans = dict((value, color) for value, color in _spans(pdf))
    assert spans[HEADING] == BLACK_SPAN
    assert _row_span(_spans(pdf), 0)[1] == BLUE_SPAN


# --------------------------------------------------------------------------- #
# AC-R35-N3：正文非链接文字逐 span 零差异
# --------------------------------------------------------------------------- #


def test_non_link_body_text_is_untouched():
    pdf, _pages, _annots = _build(4, footer=True)
    before = [(value, color) for value, color in _spans(pdf) if not value.startswith("Chapter")]

    _run(pdf)

    after = [(value, color) for value, color in _spans(pdf) if not value.startswith("Chapter")]
    assert after == before
    assert any(value.startswith("Footer") for value, _color in after)


def test_page_without_text_in_any_rect_is_not_rewritten():
    """链接矩形里没有文字 → 内容流一个字节都不动。

    「正文没被碰过」要是文件层面的事实，不能只是我们嘴上说的。
    """
    pdf = new_pdf()
    page = add_page(pdf, b"BT /F1 14 Tf 72 760 Td (Page One) Tj ET")
    annot = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])
    before = page_content_bytes(page)

    outcome = _run(pdf)

    assert page_content_bytes(page) == before
    assert outcome.after["text_recolored"] == 0
    assert outcome.after["annot_recolored"] == 1
    assert outcome.reason_code == "link_color_adjusted"


# --------------------------------------------------------------------------- #
# AC-R35-N4：链接数与目的地不变
# --------------------------------------------------------------------------- #


def test_link_count_and_destinations_survive():
    pdf, _pages, _annots = _build(6)
    before = [(page, annot.get("/Dest").unparse()) for page, annot in links.iter_links(pdf)]

    _run(pdf)

    after = [(page, annot.get("/Dest").unparse()) for page, annot in links.iter_links(pdf)]
    assert after == before
    assert len(after) == 6


def test_border_and_rect_and_action_are_untouched():
    """只改颜色：边框、矩形、URI 动作都得原样在。"""
    pdf = new_pdf()
    page = add_page(pdf, b"BT /F1 12 Tf 72 700 Td (Click me) Tj ET")
    annot = make_annot(
        pdf,
        "Link",
        (68, 695, 320, 715),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/URI"), URI=String("https://example.com")),
            "Border": Array([0, 0, 0]),
        },
    )
    attach_annots(pdf, page, [annot])

    def dump():
        return {
            str(key): annot[key].unparse() if hasattr(annot[key], "unparse") else repr(annot[key])
            for key in annot.keys()
            if str(key) != "/C"
        }

    before = dump()
    _run(pdf)

    assert dump() == before
    assert str(annot.A.URI) == "https://example.com"


# --------------------------------------------------------------------------- #
# AC-R35-N5：文字内容不变
# --------------------------------------------------------------------------- #


def test_extracted_text_is_identical_page_by_page():
    """只插颜色操作符，`Tj` 的操作数一个都不许动。"""
    pdf, _pages, _annots = _build(6, pages=2)
    before = [_text(pdf, index) for index in range(2)]

    _run(pdf)

    after = [_text(pdf, index) for index in range(2)]
    assert after == before
    assert HEADING in after[0]


def test_number_of_text_showing_operators_is_unchanged():
    """比文本更硬：内容流里 `Tj` 的条数前后一致。"""
    pdf, _pages, _annots = _build(4)

    def count_shows():
        return sum(
            1
            for page in pdf.pages
            for item in pikepdf.parse_content_stream(page)
            if str(item.operator) in {"Tj", "TJ", "'", '"'}
        )

    before = count_shows()
    _run(pdf)
    assert count_shows() == before


# --------------------------------------------------------------------------- #
# AC-R35-N6：注释 /C 同步
# --------------------------------------------------------------------------- #


def test_annotation_color_is_synchronised():
    pdf, _pages, annots = _build(3, annot_colors=[[1, 0, 0], None, BLUE])
    outcome = _run(pdf)

    for annot in annots:
        assert [float(item) for item in annot.get("/C")] == BLUE
    assert outcome.after["blue_annot"] == 3
    # 原本就是蓝的那条只改文字，不算「边框改色」
    assert outcome.after["annot_recolored"] == 2


# --------------------------------------------------------------------------- #
# AC-R25-N3：已符合的不计入修改数
# --------------------------------------------------------------------------- #


def test_already_blue_rows_are_left_alone():
    """已蓝的两行不再插颜色指令；黑的两行才插。

    黑必须**显式**写出来（`(0,0,0)`）：填充色跨 `BT` / `ET` 延续，若第 3、4 行不写颜色，
    它们会继承第 2 行留下的蓝——那样这份样本根本不存在「需要改的行」。
    """
    blue = (0.0, 0.0, 1.0)
    black = (0.0, 0.0, 0.0)
    pdf, _pages, _annots = _build(
        4, colors=[blue, blue, black, black], annot_colors=[BLUE] * 4
    )
    before = page_content_bytes(pdf.pages[0])

    outcome = _run(pdf)

    assert outcome.status == "adjusted"
    assert outcome.counts["modified"] == 2
    assert outcome.counts["compliant"] == 2
    after = page_content_bytes(pdf.pages[0])
    # 已蓝的两行不该再被插一遍颜色指令：只多了两处 `0 0 1 rg`
    assert after.count(b"0 0 1 rg") - before.count(b"0 0 1 rg") == 2
    # 变蓝的正是第 3、4 行
    for index in (2, 3):
        _value, color = _row_span(_spans(pdf), index)
        assert color == BLUE_SPAN, f"第 {index + 1} 行没变蓝"
    for index in (0, 1):
        _value, color = _row_span(_spans(pdf), index)
        assert color == BLUE_SPAN, f"第 {index + 1} 行被改坏了"


def test_second_run_is_a_no_op():
    """幂等：处理过的文件再跑一次，内容流一个字节都不动。"""
    pdf, _pages, _annots = _build(4)
    _run(pdf)
    snapshot = page_content_bytes(pdf.pages[0])

    again = _run(pdf)

    assert again.reason_code == "already_blue"
    assert again.counts["modified"] == 0
    assert again.counts["compliant"] == 4
    assert page_content_bytes(pdf.pages[0]) == snapshot


# --------------------------------------------------------------------------- #
# 字宽推进：同一 BT 里连着多个 Tj
# --------------------------------------------------------------------------- #


def _widths_font(pdf: pikepdf.Pdf) -> Dictionary:
    """带 `/Widths` 的字体：每个字符 600/1000 em，12pt 下宽 7.2。"""
    return pdf.make_indirect(
        Dictionary(
            Type=Name("/Font"),
            Subtype=Name("/Type1"),
            BaseFont=Name("/Helvetica"),
            Encoding=Name("/WinAnsiEncoding"),
            FirstChar=32,
            LastChar=126,
            Widths=Array([600] * 95),
        )
    )


def test_advance_with_widths_picks_the_right_run_in_a_shared_text_object():
    """`BT … (AB) Tj (CD) Tj ET`：第二个 Tj 的起点要按字宽推出来。

    `AB` 在 12pt 下宽 14.4，所以 `CD` 的起点是 x=86.4。链接矩形只盖住后半段，
    只有 `CD` 该变蓝——推进算错了就会把 `AB` 一起染蓝，或者一个都不染。
    """
    pdf = new_pdf()
    page = add_page(pdf, b"")
    font = _widths_font(pdf)
    page.Resources = Dictionary(Font=Dictionary(F2=font))
    page.Contents = Stream(
        pdf, b"BT /F2 12 Tf 72 700 Td (AB) Tj (CD) Tj ET\n"
    )
    annot = make_annot(
        pdf,
        "Link",
        (80.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    _run(pdf)

    spans = dict((value, color) for value, color in _spans(pdf))
    assert spans["AB"] == BLACK_SPAN, "矩形外的文字被误着色"
    assert spans["CD"] == BLUE_SPAN, "矩形内的文字没被着色"


def test_missing_widths_reports_instead_of_guessing():
    """字体没有 `/Widths` 时不猜宽度，记一条 warning 说明可能漏改。

    注意必须用 `embed_font=False`：v4.3 起 `add_page()` 默认挂的是**带
    `/Widths` 的真嵌入 Helvetica**，用它就造不出"缺字宽"这个场景了。
    """
    pdf = new_pdf()
    page = add_page(pdf, b"BT /F1 12 Tf 72 700 Td (AB) Tj (CD) Tj ET\n", embed_font=False)
    annot = make_annot(
        pdf,
        "Link",
        (80.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    outcome = _run(pdf)

    assert content_color.NO_WIDTHS_NOTE in outcome.warnings
    # 不推进 → 第二个 Tj 的起点仍算在 x=72，落在矩形外 → 什么都不改
    assert outcome.after["text_recolored"] == 0


def test_tj_array_and_quote_operators_are_recognised():
    """`TJ` 与 `'` 也要算起点：`'` 自带一次换行，起点在 `TL` 之后。

    这里用 `Tm` 而不是 `Td` 定位：`Td` 是**相对**当前行矩阵的位移，连着写
    `72 700 Td` + `72 660 Td` 会把第二行推到 (144, 1360)，落到页面外，MuPDF 干脆
    不渲染它。`Tm` 才是绝对定位。
    """
    pdf = new_pdf()
    page = add_page(pdf, b"")
    page.Resources = Dictionary(Font=Dictionary(F2=_widths_font(pdf)))
    page.Contents = Stream(
        pdf,
        b"BT /F2 12 Tf 12 TL 1 0 0 1 72 700 Tm [(He) -300 (llo)] TJ\n"
        b"1 0 0 1 72 660 Tm (Next) Tj\n"
        b"(Quoted) '\nET\n",
    )
    # 三条链接矩形分别盖住三行
    rects = [(68.0, 695.0, 320.0, 715.0), (68.0, 655.0, 320.0, 675.0), (68.0, 643.0, 320.0, 663.0)]
    annots = [
        make_annot(
            pdf,
            "Link",
            rect,
            appearance=None,
            extra={"Dest": dest_xyz(page, None)},
        )
        for rect in rects
    ]
    attach_annots(pdf, page, annots)

    outcome = _run(pdf)

    assert outcome.counts["modified"] == 3
    blue = [value for value, color in _spans(pdf) if color == BLUE_SPAN]
    # 三行各自一个 span；`TJ` 里那个 -300 的间距会被 MuPDF 读成一个空格，
    # 所以只按前缀认，不钉死 "Hello"。
    assert len(blue) == 3, blue
    assert any(value.startswith("He") for value in blue), blue
    assert "Next" in blue and "Quoted" in blue, blue


# --------------------------------------------------------------------------- #
# 颜色状态的恢复
# --------------------------------------------------------------------------- #


def test_original_color_is_restored_after_the_blue_run():
    """红色标题之后紧跟一个链接行：链接变蓝，红标题仍是红。"""
    pdf = new_pdf()
    page = add_page(pdf, b"")
    page.Resources = Dictionary(Font=Dictionary(F2=_widths_font(pdf)))
    page.Contents = Stream(
        pdf,
        b"BT /F2 12 Tf 1 0 0 rg 72 700 Td (Red heading) Tj ET\n"
        b"BT /F2 12 Tf 1 0 0 rg 72 660 Td (Red link) Tj ET\n",
    )
    annot = make_annot(
        pdf,
        "Link",
        (68.0, 655.0, 320.0, 675.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    _run(pdf)

    spans = dict((value, color) for value, color in _spans(pdf))
    assert spans["Red heading"] == 0xFF0000
    assert spans["Red link"] == BLUE_SPAN


def test_explicit_color_space_is_replayed_on_restore():
    """`cs` / `scn` 选出来的色空间必须跟着重放，否则颜色分量会落到错误的色空间。"""
    pdf = new_pdf()
    page = add_page(pdf, b"")
    page.Resources = Dictionary(
        Font=Dictionary(F2=_widths_font(pdf)),
        ColorSpace=Dictionary(CS0=Name("/DeviceRGB")),
    )
    page.Contents = Stream(
        pdf,
        b"BT /F2 12 Tf /CS0 cs 1 0 0 scn 72 700 Td (Spacey) Tj ET\n",
    )
    annot = make_annot(
        pdf,
        "Link",
        (68.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    _run(pdf)

    stream = page_content_bytes(page)
    assert b"0 0 1 rg" in stream
    # 恢复时必须重新选一次色空间，再重放原分量
    assert b"/CS0 cs" in stream
    assert b"1 0 0 scn" in stream
    spans = dict((value, color) for value, color in _spans(pdf))
    assert spans["Spacey"] == BLUE_SPAN


def test_stroke_render_mode_also_sets_stroke_color():
    """`Tr 1`（只描边）时文字颜色由描边色决定，只改填充色等于没改。"""
    pdf = new_pdf()
    page = add_page(pdf, b"")
    page.Resources = Dictionary(Font=Dictionary(F2=_widths_font(pdf)))
    page.Contents = Stream(
        pdf, b"BT /F2 12 Tf 1 Tr 72 700 Td (Outlined) Tj ET\n"
    )
    annot = make_annot(
        pdf,
        "Link",
        (68.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    _run(pdf)

    stream = page_content_bytes(page)
    assert b"0 0 1 rg" in stream
    assert b"0 0 1 RG" in stream
    # 恢复：描边色回到默认黑
    assert b"0 G" in stream


# --------------------------------------------------------------------------- #
# AC-R35-E1：单页内容流坏掉不拖垮整项
# --------------------------------------------------------------------------- #


def _poison_contents(pdf: pikepdf.Pdf, page) -> None:
    """把内容流换成「`/Filter` 与实际字节不符」的流。

    pikepdf 对垃圾字节极其宽容（`((( `、`Q Q ~ !` 都能「解析成功」），
    唯一稳定抛错的是解码器与数据对不上，所以这里必须伪装成 FlateDecode。
    """
    stream = Stream(pdf, b"this is not deflate data at all")
    stream["/Filter"] = Name("/FlateDecode")
    page.Contents = stream


def test_broken_content_stream_is_reported_and_others_still_processed():
    pdf, pages, _annots = _build(2, pages=2)
    _poison_contents(pdf, pages[1])
    second = make_annot(
        pdf,
        "Link",
        (68.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(pages[0], None)},
    )
    attach_annots(pdf, pages[1], [second])

    outcome = _run(pdf)

    assert outcome.counts["failed"] == 1
    assert outcome.status == "failed"
    assert outcome.reason_code == "link_color_partial"
    assert "第 2 页" in outcome.message
    assert any("无法解析" in warning for warning in outcome.warnings)
    # 第 1 页照常处理
    assert _row_span(_spans(pdf), 0)[1] == BLUE_SPAN


def test_one_broken_annotation_color_does_not_fail_the_others():
    """AC-R25-E1：`/C` 结构异常只跳过这一条。"""
    pdf, pages, annots = _build(3)
    broken = make_annot(
        pdf,
        "Link",
        (68.0, 500.0, 320.0, 520.0),
        appearance=None,
        extra={"Dest": dest_xyz(pages[0], None), "C": String("blue-ish")},
    )
    attach_annots(pdf, pages[0], annots_of(pages[0]) + [broken])

    outcome = _run(pdf)

    assert outcome.counts["checked"] == 4
    assert outcome.counts["modified"] == 3
    assert outcome.counts["failed"] == 1
    assert any("颜色值结构异常" in warning for warning in outcome.warnings)
    assert str(broken.C) == "blue-ish"


def test_link_without_subtype_is_reported_not_silently_skipped():
    """缺 `/Subtype` 但带 `/Dest` 的哑链接要被计数，不能当它不存在。"""
    pdf, pages, _annots = _build(2)
    mute = Dictionary(
        Type=Name("/Annot"),
        Rect=Array([68, 400, 320, 420]),
        P=pages[0],
        F=4,
        Dest=dest_xyz(pages[0], None),
    )
    attach_annots(pdf, pages[0], annots_of(pages[0]) + [mute])

    outcome = _run(pdf)

    assert outcome.counts["checked"] == 3
    assert outcome.counts["modified"] == 2
    assert outcome.counts["failed"] == 1
    assert any("缺少 /Subtype" in warning for warning in outcome.warnings)


# --------------------------------------------------------------------------- #
# AC-R35-E2：必须附带的说明
# --------------------------------------------------------------------------- #


def test_text_note_is_always_attached():
    for count in (0, 3):
        pdf, _pages, _annots = _build(count)
        outcome = _run(pdf)
        assert content_color.TEXT_NOTE in outcome.warnings


def test_text_note_is_not_duplicated():
    pdf, _pages, _annots = _build(3)
    outcome = _run(pdf)
    assert outcome.warnings.count(content_color.TEXT_NOTE) == 1


def test_text_note_says_what_the_operation_really_does():
    """文案不能退回「只改链接边框」——那正是用户抱怨「并未修正」的原因。"""
    note = content_color.TEXT_NOTE
    assert "文字" in note
    assert "蓝色" in note
    assert "目录" in note
    assert "只改链接对象颜色" not in note


def test_legacy_names_point_at_the_new_note():
    """`link_color.VISIBILITY_NOTE` 的旧文案已经错了，必须指到新说明。"""
    assert link_color.VISIBILITY_NOTE == content_color.TEXT_NOTE
    assert link_color.TARGET_COLOR == content_color.TARGET_RGB


def test_forwarding_layer_does_not_hold_a_second_implementation():
    """`pipeline.py` 注册的是 `link_color.process`，它必须就是新实现本身。

    R35 的起因就是「内链颜色并未修正」——旧实现只改 `/C`，阅读器不画链接
    边框，用户看不见任何变化。如果哪天有人往 `link_color.py` 里补回一套
    自己的逻辑，流水线会继续跑旧行为，而 `content_color.py` 的测试全绿，
    这种回归最难发现。用**同一对象**来钉，而不是用行为来钉。
    """
    assert link_color.process is content_color.process
    assert link_color.describe is content_color.describe
    assert link_color.broken_links is content_color.broken_links


# --------------------------------------------------------------------------- #
# 没有链接 / 没有文字
# --------------------------------------------------------------------------- #


def test_document_without_links_does_not_pretend():
    pdf = new_pdf()
    add_page(pdf)
    before = page_content_bytes(pdf.pages[0])

    outcome = _run(pdf)

    assert outcome.status == "not_applicable"
    assert outcome.reason_code == "no_link"
    assert outcome.changed is False
    assert outcome.counts["checked"] == 0
    assert outcome.before == outcome.after == {
        "links": 0,
        "blue_text": 0,
        "blue_annot": 0,
        "text_recolored": 0,
        "annot_recolored": 0,
    }
    assert page_content_bytes(pdf.pages[0]) == before


def test_scanned_page_without_text_layer_is_not_applicable():
    """扫描件没有文字层：链接上没字可着色，报 `not_applicable` 而不是失败。"""
    pdf = new_pdf()
    page = add_page(pdf, b"")
    annot = make_annot(
        pdf,
        "Link",
        (68.0, 695.0, 320.0, 715.0),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    attach_annots(pdf, page, [annot])

    outcome = _run(pdf)

    assert outcome.reason_code == "no_text_layer"
    assert outcome.counts["not_applicable"] == 1
    assert outcome.counts["failed"] == 0
    assert any(content_color.NO_TEXT_NOTE in warning for warning in outcome.warnings)
    # 整页原样保留：连 `/C` 都不动，否则「未做修改」这句话就是假的
    assert annot.get("/C") is None
    assert outcome.changed is False


# --------------------------------------------------------------------------- #
# 豁免语义
# --------------------------------------------------------------------------- #


def test_exempt_pages_still_get_colored():
    """R35 不在 `EXEMPT_SENSITIVE_OPERATIONS` 里：豁免页上的链接同样改色。"""
    pdf, _pages, _annots = _build(4)
    outcome = _run(pdf, exempt=(1,))

    assert outcome.counts["exempt"] == 0
    assert outcome.counts["modified"] == 4


# --------------------------------------------------------------------------- #
# describe()
# --------------------------------------------------------------------------- #


def test_describe_reports_measured_snapshot():
    pdf, _pages, _annots = _build(2, annot_colors=[BLUE, None])
    snapshot = content_color.describe(pdf)

    assert snapshot["links"] == 2
    assert snapshot["blue_text"] == 0
    assert snapshot["blue_annot"] == 1
    assert snapshot["text_recolored"] == 0
    assert snapshot["annot_recolored"] == 0


def test_describe_sees_the_blue_after_processing():
    pdf, _pages, _annots = _build(3)
    _run(pdf)
    snapshot = content_color.describe(pdf)

    assert snapshot["blue_text"] == 3
    assert snapshot["blue_annot"] == 3


# --------------------------------------------------------------------------- #
# 内容流手术的边界
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("count", [1, 3, 12])
def test_stream_growth_is_proportional_to_targets(count):
    """只给命中处加指令：内容流长度随命中数线性增长，不是整页重写。"""
    pdf, _pages, _annots = _build(count)
    before = len(page_content_bytes(pdf.pages[0]))

    _run(pdf)

    after = len(page_content_bytes(pdf.pages[0]))
    growth = after - before
    assert growth > 0
    # 每条命中插入 `0 0 1 rg` 与 `0 g` 两条指令，远小于一页正文的体积
    assert growth < 200 * count


def test_links_without_rect_are_not_guessed():
    """`/Rect` 结构坏掉的链接不猜位置：跳过文字着色，只同步 `/C`。

    `/Rect` 只有两个数时位置无从判断，猜一个矩形出来会把不相干的正文染蓝——
    比漏改更难查。所以这里允许「文字一处没改」，但必须说清到底改了什么。
    """
    pdf = new_pdf()
    page = add_page(pdf, b"BT /F1 12 Tf 72 700 Td (Hello) Tj ET")
    annot = make_annot(
        pdf,
        "Link",
        (68, 695, 320, 715),
        appearance=None,
        extra={"Dest": dest_xyz(page, None)},
    )
    annot["/Rect"] = Array([68, 695])
    attach_annots(pdf, page, [annot])
    before = page_content_bytes(page)

    outcome = _run(pdf)

    assert page_content_bytes(page) == before
    assert outcome.counts["failed"] == 0
    assert outcome.after["text_recolored"] == 0
    assert outcome.counts["modified"] == 1
    assert outcome.after["annot_recolored"] == 1
    assert [float(item) for item in annot.get("/C")] == BLUE
