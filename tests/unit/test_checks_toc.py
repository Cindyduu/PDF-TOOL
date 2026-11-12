"""R32 目录校验（v4.1 新增）。

用户要求：「每个文件的首页需要添加目录，且字体设置为蓝色。目录内链要求：
内链均为蓝色（RGB 0 0 255；比如目录，但需要注意，目录 2 字仍为黑色）」。

按 D-10 裁决，工具**不生成目录**，只校验三件事：首页有没有目录（≥2 条内链）、
目录文字是不是蓝色、每条内链能不能跳到具体页。

这个文件钉住 PRD §16.1 的验收条目 AC-R32-N1…N5、E1、E2，外加两条实现细节：

- **标题必须是黑色也不影响判定**：标题不挂在链接矩形里，校验只看得到条目。
- **措辞红线**：任何结论都不许出现「目录正确」。工具没读过正文，不知道
  「1. 引言 …… 第 5 页」这一条写得对不对。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name, String

from app.pdf.checks import toc
from tests.fixtures.make_fixtures import (
    _toc_content,
    _toc_document,
    attach_annots,
    dest_xyz,
    new_pdf,
    save,
)

#: 措辞红线要扫的样本。
ALL_FIXTURES = [
    "check_clean.pdf",
    "check_toc_ok.pdf",
    "check_toc_partial_blue.pdf",
    "check_toc_title_only.pdf",
    "check_toc_none.pdf",
    "check_toc_single_page.pdf",
    "check_toc_broken_dest.pdf",
]


# --------------------------------------------------------------------------- #
# AC-R32-N1 … N5
# --------------------------------------------------------------------------- #


def test_blue_entries_pass(check_ctx):
    """AC-R32-N1：首页 4 条蓝色内链 → PASS，明细含人工确认提示。"""
    result = toc.run(check_ctx("check_toc_ok.pdf"))
    assert result.status == "pass"
    assert result.summary == "首页 4 条内链，颜色均为蓝色"
    assert result.findings[0].detail == toc.HUMAN_NOTE


def test_partially_black_entries_warn(check_ctx):
    """AC-R32-N2：4 条内链其中 1 条黑色 → WARNING，明细含「1 条非蓝」。"""
    result = toc.run(check_ctx("check_toc_partial_blue.pdf"))
    assert result.status == "warning"
    assert result.summary == "首页 4 条内链中有 1 条非蓝"
    blob = " ".join(item.message + (item.detail or "") for item in result.findings)
    assert "1 条目录文字不是蓝色" in blob
    # 明细要指出是哪一条、实际是什么颜色
    assert "第 2 条" in blob
    assert "#000000" in blob


def test_title_without_links_warns(check_ctx):
    """AC-R32-N3：首页有目录标题但无内链 → WARNING，措辞含「无法确认目录可跳转」。"""
    result = toc.run(check_ctx("check_toc_title_only.pdf"))
    assert result.status == "warning"
    assert "无法确认目录可跳转" in result.summary
    # 关键词要报最长的那个，否则「Table of Contents」会被说成「contents」
    assert "Table of Contents" in result.summary


def test_no_toc_at_all_fails(check_ctx):
    """AC-R32-N4：首页无目录无内链 → FAIL。"""
    result = toc.run(check_ctx("check_toc_none.pdf"))
    assert result.status == "fail"
    assert result.summary == "首页未发现目录"


def test_single_page_is_skipped(check_ctx):
    """AC-R32-N5：单页文档 → SKIPPED。"""
    result = toc.run(check_ctx("check_toc_single_page.pdf"))
    assert result.status == "skipped"
    assert result.summary == "单页文档无目录"


# --------------------------------------------------------------------------- #
# AC-R32-E1 / E2
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_never_claims_the_toc_is_correct(name, check_ctx):
    """AC-R32-E1：任何结论都不得出现「目录正确」字样。

    工具只能核对「有几条内链、什么颜色、跳不跳得过去」，核对不了条目内容。
    写成「目录正确」就是把没做的事说成做过了。
    """
    result = toc.run(check_ctx(name))
    blob = result.summary + " ".join(
        item.message + (item.detail or "") for item in result.findings
    )
    assert "目录正确" not in blob
    assert "目录无误" not in blob
    assert "目录已核对" not in blob


def test_broken_dest_fails(check_ctx):
    """AC-R32-E2：首页内链目的地损坏 → FAIL，其余条目照常判定。"""
    result = toc.run(check_ctx("check_toc_broken_dest.pdf"))
    assert result.status == "fail"
    assert result.summary == "首页 4 条内链中有 1 条无法跳转"
    assert any("页内序号：3" in (item.detail or "") for item in result.findings)


def test_broken_dest_still_reports_colors(tmp_path, ctx_factory):
    """跳不过去时，颜色结论不能缺席——用户要知道「还差什么」，不只是「坏了」。"""
    pdf = new_pdf()
    _toc_document(pdf, 8, _toc_content(4, black_rows=(0,)), 4, broken=(2,))
    path = save(pdf, tmp_path / "broken_and_black.pdf")

    result = toc.run(ctx_factory(path))
    assert result.status == "fail"
    blob = " ".join(item.message + (item.detail or "") for item in result.findings)
    assert "无法跳转" in result.summary
    assert "不是蓝色" in blob


# --------------------------------------------------------------------------- #
# 颜色判定的实现细节
# --------------------------------------------------------------------------- #


def test_title_stays_black_and_is_not_judged(check_ctx):
    """「目录」二字是黑色，但它不在链接矩形里，所以不该被算成非蓝条目。

    这正是用户原话里那句「比如目录，但需要注意，目录 2 字仍为黑色」——
    要蓝的是**条目**，不是标题。
    """
    result = toc.run(check_ctx("check_toc_ok.pdf"))
    assert result.status == "pass"
    assert "非蓝" not in result.summary


def test_link_without_text_is_reported_as_unknown(tmp_path, ctx_factory):
    """链接矩形里没有可提取文字 → warning「无法核对」，不能默认判它合规。"""
    pdf = new_pdf()
    pages = []
    for _ in range(3):
        pdf.add_blank_page(page_size=(595, 842))
        pages.append(pdf.pages[-1].obj)
    # 空白页：没有内容流，也没有文字
    annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([68, 700, 300, 730]),
                P=pages[0],
                F=4,
                Dest=dest_xyz(pages[1], None),
            ),
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([68, 660, 300, 690]),
                P=pages[0],
                F=4,
                Dest=dest_xyz(pages[2], None),
            ),
        ]
    )
    attach_annots(pdf, pages[0], list(annots))
    path = save(pdf, tmp_path / "links_without_text.pdf")

    result = toc.run(ctx_factory(path))
    assert result.status == "warning"
    assert "颜色无法核对" in result.summary


def test_uri_links_are_not_counted_as_entries(tmp_path, ctx_factory):
    """首页只有外链不算目录：外链跳不出本文档，构不成目录。"""
    pdf = new_pdf()
    pages = []
    for _ in range(3):
        pdf.add_blank_page(page_size=(595, 842))
        pages.append(pdf.pages[-1].obj)
    annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([68, 700, 300, 730]),
                P=pages[0],
                F=4,
                A=Dictionary(S=Name("/URI"), URI=String("https://example.org/a")),
            )
        ]
    )
    attach_annots(pdf, pages[0], list(annots))
    path = save(pdf, tmp_path / "uri_only.pdf")

    result = toc.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "首页未发现目录"


def test_non_link_annots_are_ignored(tmp_path, ctx_factory):
    """首页的高亮、批注不是目录条目，不能把 `toc` 项算成「有目录」。"""
    pdf = new_pdf()
    pages = []
    for _ in range(3):
        pdf.add_blank_page(page_size=(595, 842))
        pages.append(pdf.pages[-1].obj)
    annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Highlight"),
                Rect=Array([68, 700, 300, 730]),
                P=pages[0],
                F=4,
            ),
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Highlight"),
                Rect=Array([68, 660, 300, 690]),
                P=pages[0],
                F=4,
            ),
        ]
    )
    attach_annots(pdf, pages[0], list(annots))
    path = save(pdf, tmp_path / "highlights_only.pdf")

    result = toc.run(ctx_factory(path))
    assert result.status == "fail"


def test_keyword_match_prefers_the_longest():
    """「Table of Contents」里也含「contents」，报短的会读起来像在说别的东西。

    而且回的是**原文大小写**，不是小写规范形——结论是给人看的。
    """
    assert toc._matched_keyword("Table of Contents") == "Table of Contents"
    assert toc._matched_keyword("TABLE OF CONTENTS") == "TABLE OF CONTENTS"
    assert toc._matched_keyword("目录") == "目录"
    assert toc._matched_keyword("no heading here") is None


def test_target_blue_is_pure_rgb_blue():
    """用户指定的是 RGB(0,0,255)，不是「某种蓝」。"""
    assert toc.BLUE == 0x0000FF


def test_one_link_is_not_enough_for_a_toc(tmp_path, ctx_factory):
    """只有 1 条内链：更可能是交叉引用，不是目录。"""
    pdf = new_pdf()
    pages = []
    for _ in range(3):
        pdf.add_blank_page(page_size=(595, 842))
        pages.append(pdf.pages[-1].obj)
    annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([68, 700, 300, 730]),
                P=pages[0],
                F=4,
                Dest=dest_xyz(pages[1], None),
            )
        ]
    )
    attach_annots(pdf, pages[0], list(annots))
    path = save(pdf, tmp_path / "one_link.pdf")

    result = toc.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "首页未发现目录"
