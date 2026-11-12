"""R19 批注与高亮校验。

这一项只报告现状、不做处理。最容易写错的两点：

- `/Link` 不是批注（AC-R19-N2）。算进来会让每个带目录链接的文件都告警，
  用户很快就不看了。
- 无批注时那句「若此前做过扁平化，这是预期结果」必须无条件出现——
  工具分不清「本来没有」和「刚被扁平化掉」（AC-R19-E1）。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name

from app.pdf.checks import annotations
from tests.fixtures.make_fixtures import add_page, attach_annots, new_pdf, save


def _annot(page, subtype: str, rect: tuple[int, int, int, int] = (72, 600, 300, 620)):
    return Dictionary(
        Type=Name("/Annot"),
        Subtype=Name("/" + subtype),
        Rect=Array(list(rect)),
        P=page,
        F=4,
    )


def test_clean_document_passes(check_ctx):
    result = annotations.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.summary == "未发现批注或高亮"


def test_clean_document_explains_possible_flattening(check_ctx):
    """AC-R19-E1：没有批注时也要说明"可能是扁平化过的"，让用户自己判断。"""
    result = annotations.run(check_ctx("check_clean.pdf"))
    detail = next(item.detail for item in result.findings)
    assert detail == annotations.FLATTEN_NOTE
    assert "扁平化" in detail


def test_highlights_are_counted_per_page(check_ctx):
    result = annotations.run(check_ctx("check_highlights.pdf"))
    assert result.status == "warning"
    assert result.summary == "发现 3 处批注（高亮 3 处）"
    assert any("高亮 3 处（第 2、5、7 页）" in item.message for item in result.findings)


def test_highlight_finding_is_a_warning(check_ctx):
    result = annotations.run(check_ctx("check_highlights.pdf"))
    assert [item.level for item in result.findings] == ["warning"]


def test_links_are_not_counted_as_annotations(check_ctx):
    """AC-R19-N2：`check_links_http.pdf` 有 3 个 `/Link`，本项必须 pass。"""
    result = annotations.run(check_ctx("check_links_http.pdf"))
    assert result.status == "pass"


def test_unknown_subtype_falls_into_other(tmp_path, ctx_factory):
    pdf = new_pdf()
    page = add_page(pdf)
    attach_annots(pdf, page, [_annot(page, "Square")])
    path = save(pdf, tmp_path / "square.pdf")

    result = annotations.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "发现 1 处批注（其他 1 处）"


def test_groups_are_listed_in_report_order(tmp_path, ctx_factory):
    """报告顺序固定为 高亮 → 文本注释 → 图章 → 文本框 → 手绘 → 其他。"""
    pdf = new_pdf()
    page = add_page(pdf)
    attach_annots(
        pdf,
        page,
        [
            _annot(page, "Square"),  # 其他
            _annot(page, "Ink"),  # 手绘
            _annot(page, "FreeText"),  # 文本框
            _annot(page, "Stamp"),  # 图章
            _annot(page, "Highlight"),  # 高亮
        ],
    )
    path = save(pdf, tmp_path / "ordered.pdf")

    result = annotations.run(ctx_factory(path))
    assert result.summary == "发现 5 处批注（高亮 1 处、图章 1 处、文本框 1 处、手绘 1 处、其他 1 处）"


def test_same_page_multiple_annots_share_one_page_number(tmp_path, ctx_factory):
    """同页两条：条数算 2，页号只记一次。"""
    pdf = new_pdf()
    page = add_page(pdf)
    attach_annots(
        pdf,
        page,
        [_annot(page, "Highlight", (72, 600, 300, 620)), _annot(page, "Highlight", (72, 500, 300, 520))],
    )
    path = save(pdf, tmp_path / "same_page.pdf")

    result = annotations.run(ctx_factory(path))
    finding = result.findings[0]
    assert finding.message == "高亮 2 处（第 1 页）"
    assert finding.detail == "共 2 处，分布在 1 页"


def test_pages_are_deduplicated_but_counted(tmp_path, ctx_factory):
    """两页各两条：页号去重，条数仍是 4。"""
    pdf = new_pdf()
    pages = [add_page(pdf), add_page(pdf)]
    for page in pages:
        attach_annots(pdf, page, [_annot(page, "Text"), _annot(page, "Text", (72, 500, 300, 520))])
    path = save(pdf, tmp_path / "two_pages.pdf")

    result = annotations.run(ctx_factory(path))
    finding = result.findings[0]
    assert finding.message == "文本注释 4 处（第 1-2 页）"
    assert finding.detail == "共 4 处，分布在 2 页"


def test_annotation_without_subtype_is_counted_as_other(tmp_path, ctx_factory):
    pdf = new_pdf()
    page = add_page(pdf)
    attach_annots(pdf, page, [Dictionary(Type=Name("/Annot"), Rect=Array([72, 600, 300, 620]))])
    path = save(pdf, tmp_path / "no_subtype.pdf")

    result = annotations.run(ctx_factory(path))
    assert result.summary == "发现 1 处批注（其他 1 处）"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Name("/Highlight"), "Highlight"),
        (Name("/Link"), "Link"),
        (None, ""),
    ],
)
def test_subtype_reader_strips_the_name_slash(value, expected):
    annot = Dictionary()
    if value is not None:
        annot[Name("/Subtype")] = value
    assert annotations._subtype(annot) == expected


def test_every_grouped_subtype_maps_back_to_its_group():
    for name, subtypes in annotations.GROUPS:
        for subtype in subtypes:
            assert annotations._GROUP_OF[subtype] == name


def test_other_group_is_not_listed_in_groups():
    """`其他` 是兜底，不该出现在固定分组里，否则会被算两次。"""
    assert annotations.OTHER not in [name for name, _ in annotations.GROUPS]


# --------------------------------------------------------------------------- #
# `/Annots` 结构损坏
# --------------------------------------------------------------------------- #


def test_broken_annots_page_is_reported_not_crashed(check_ctx):
    """回归：悬空引用读成 `Array([None])`，`None.get()` 直接抛 AttributeError。

    修前结果是 `fail | 检查异常：AttributeError`——既不可读，也不准确：
    文件可能真没有批注，只是这一页没看过。
    """
    result = annotations.run(check_ctx("check_links_broken_annots.pdf"))
    assert result.status == "warning"
    assert "AttributeError" not in result.summary


def test_broken_annots_page_cannot_be_claimed_as_clean(check_ctx):
    """读不出来的页不能说"没有批注"，只能说"没读到"。"""
    result = annotations.run(check_ctx("check_links_broken_annots.pdf"))
    assert result.summary == "未发现可读取的批注，1 页批注读取失败"
    assert any("第 1 页批注读取失败" in item.message for item in result.findings)


def test_healthy_pages_are_still_counted_next_to_a_broken_one(tmp_path, ctx_factory):
    """一页坏、一页有高亮：高亮照常统计，坏页单独记一条。"""
    from tests.fixtures.make_fixtures import raw_pdf, raw_stream

    page1 = b"BT /F1 12 Tf 72 700 Td (Broken annots) Tj ET"
    page2 = b"BT /F1 12 Tf 72 700 Td (Highlighted page) Tj ET"
    resources = b"<< /Font << /F1 7 0 R >> >>"
    path = tmp_path / "broken_plus_highlight.pdf"
    path.write_bytes(
        raw_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources " + resources + b" /Contents 4 0 R /Annots [9 0 R] >>",
                raw_stream(page1),
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources " + resources + b" /Contents 6 0 R /Annots [8 0 R] >>",
                raw_stream(page2),
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                b"<< /Type /Annot /Subtype /Highlight /Rect [72 690 320 710] /Border [0 0 0] >>",
            ]
        )
    )

    result = annotations.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "发现 1 处批注（高亮 1 处），1 页批注读取失败"
    assert any("第 2 页" in item.message for item in result.findings)
    assert any("第 1 页批注读取失败" in item.message for item in result.findings)
