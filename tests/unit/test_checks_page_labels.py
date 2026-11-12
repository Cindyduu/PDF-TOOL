"""R33 页码校验（v4.1 从 R21「目录页码」拆出）。

拆分理由：目录（R32）看的是首页内链与文字颜色，页码（R33）看的是编号从 1 起、
逐页连续。两者的判定依据完全不同，混在一项里只能给出一个含糊的结论。

这一项有两条路：`/PageLabels` 优先，没有就走页脚数字兜底。测试两边都要覆盖——
页脚兜底是 v4.1 新加的，而且它是「保守判定」的典型：宁可 warning 也不能假 fail。

验收对照：

- `/PageLabels` 路 —— AC-R33-N1（从 1 开始 → PASS）、AC-R33-N2（起始为 3 → FAIL）。
  这两个判据原样继承自 v4.0 的 AC-R21-N1 / AC-R21-N2 / AC-R21-N3 / AC-R21-E1，
  R33 只把标题从「目录页码」收窄成「页码」并补了页脚兜底，旧条目仍然成立，
  所以下面这些用例同时是旧基线的回归。
- 页脚兜底路 —— AC-R33-N3（页脚 1..N → PASS）、AC-R33-N4（读不到 → WARNING）。
- AC-R33-E1 —— 措辞红线：任何结论都不得写成「页码正确」。

措辞红线：任何情况下都不许写「目录页码正确」——这一项根本没读目录。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name

from app.pdf.checks import page_labels
from tests.fixtures.make_fixtures import _add_pages, _outline, _page_labels, new_pdf, save

#: 措辞红线要扫的样本：`/PageLabels` 路与页脚路各来几个。
ALL_FIXTURES = [
    "check_clean.pdf",
    "check_labels_none.pdf",
    "check_labels_from3.pdf",
    "check_footer_ok.pdf",
    "check_footer_from3.pdf",
    "check_footer_gap.pdf",
]


# --------------------------------------------------------------------------- #
# `/PageLabels` 路
# --------------------------------------------------------------------------- #


def test_clean_document_passes(check_ctx):
    result = page_labels.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.summary == "逻辑页码从 1 开始"


def test_pass_also_carries_the_human_note(check_ctx):
    """pass 也必须带附注：工具核对的是编号，不是「每页都看得见数字」。"""
    result = page_labels.run(check_ctx("check_clean.pdf"))
    assert result.findings[0].detail == page_labels.HUMAN_NOTE


def test_missing_page_labels_warns(check_ctx):
    """既没有 `/PageLabels`，页脚也没有数字 → 只能请人工复核。"""
    result = page_labels.run(check_ctx("check_labels_none.pdf"))
    assert result.status == "warning"
    assert result.summary == "未设置逻辑页码，页脚也未发现页码，请人工复核"
    assert result.findings[0].message == "未设置逻辑页码（/PageLabels），页脚也未读到页码"


def test_starting_from_three_fails(check_ctx):
    result = page_labels.run(check_ctx("check_labels_from3.pdf"))
    assert result.status == "fail"
    assert result.summary == "逻辑页码未从 1 开始（起始为 3）"


def test_fail_points_at_the_first_page(check_ctx):
    result = page_labels.run(check_ctx("check_labels_from3.pdf"))
    finding = result.findings[0]
    assert finding.page == 1
    assert finding.detail == "第 1 段从第 1 页开始，起始编号为 3"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_never_claims_the_catalog_is_correct(name, check_ctx):
    """措辞红线：不管什么结论，都不能说目录是对的。"""
    result = page_labels.run(check_ctx(name))
    blob = result.summary + " ".join(
        item.message + (item.detail or "") for item in result.findings
    )
    assert "目录页码正确" not in blob
    assert "目录正确" not in blob


def test_pages_before_the_first_segment_warn(tmp_path, ctx_factory):
    """首段不从第 1 页开始：前几页没有逻辑页码，读者看到的就是「没有页码」。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 6)
    _outline(pdf, pages, 3)
    _page_labels(pdf, start=1, first_index=3)
    path = save(pdf, tmp_path / "offset.pdf")

    result = page_labels.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "前 3 页未设置逻辑页码"
    assert any("第 1-3 页未设置逻辑页码" in item.message for item in result.findings)


def test_missing_start_value_defaults_to_one(tmp_path, ctx_factory):
    """`/St` 缺省按 1 处理（PDF 规范默认值）。"""
    pdf = new_pdf()
    _add_pages(pdf, 2)
    pdf.Root.PageLabels = Dictionary(Nums=Array([0, Dictionary(S=Name("/D"))]))
    path = save(pdf, tmp_path / "no_st.pdf")

    result = page_labels.run(ctx_factory(path))
    assert result.status == "pass"


def test_non_numeric_start_falls_back_to_one(tmp_path, ctx_factory):
    """`/St` 是乱写的名字 → 回退到 1，不能抛异常。"""
    pdf = new_pdf()
    _add_pages(pdf, 2)
    pdf.Root.PageLabels = Dictionary(Nums=Array([0, Dictionary(S=Name("/D"), St=Name("/X"))]))
    path = save(pdf, tmp_path / "bad_st.pdf")

    result = page_labels.run(ctx_factory(path))
    assert result.status == "pass"


# --------------------------------------------------------------------------- #
# 页脚兜底路
# --------------------------------------------------------------------------- #


def test_footer_numbers_starting_at_one_pass(check_ctx):
    """没有 `/PageLabels`，但页脚印着 1–6 → pass。这正是绝大多数真实文件。"""
    result = page_labels.run(check_ctx("check_footer_ok.pdf"))
    assert result.status == "pass"
    assert result.summary == "页脚页码从 1 开始连续（共 6 页）"
    assert result.findings[0].detail == page_labels.HUMAN_NOTE


def test_footer_numbers_starting_at_three_fail(check_ctx):
    """6 页文档页脚印 3–8 → fail。页码整体偏移是真实缺陷，不能降级成 warning。

    这条是刻意钉住的：曾经用「数字不能超过总页数」做过滤，结果 7、8 被当成
    非页码丢掉，fail 被误降成 warning。
    """
    result = page_labels.run(check_ctx("check_footer_from3.pdf"))
    assert result.status == "fail"
    assert result.summary == "页码未从 1 开始（第 1 页为 3）"
    assert result.findings[0].detail is not None
    assert "3–8" in result.findings[0].detail


def test_missing_footer_on_one_page_warns(check_ctx):
    """有一页漏印页码 → 不敢下结论，退到 warning。"""
    result = page_labels.run(check_ctx("check_footer_gap.pdf"))
    assert result.status == "warning"
    assert result.findings[0].page == 3


def test_footer_out_of_order_warns_not_fails(tmp_path, ctx_factory):
    """页脚数字存在但不连续 → warning。读到的可能是别的东西，不能判 fail。"""
    from tests.fixtures.make_fixtures import _footer_document

    pdf = new_pdf()
    _footer_document(pdf, [1, 2, 3, 9, 5, 6])
    path = save(pdf, tmp_path / "jumpy.pdf")

    result = page_labels.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "页脚页码不连续，无法自动核对"


def test_page_number_regex_rejects_compound_footers():
    """「第 3 页 / 共 8 页」不认——多认一种写法就多一类误判。"""
    assert page_labels._PAGE_NUMBER.match("3") is not None
    assert page_labels._PAGE_NUMBER.match(" - 3 - ") is not None
    assert page_labels._PAGE_NUMBER.match("第 3 页") is None
    assert page_labels._PAGE_NUMBER.match("3 / 8") is None
    assert page_labels._PAGE_NUMBER.match("DOC-2024") is None


def test_footer_band_is_a_narrow_strip():
    """页脚带太宽会把正文最后几行捞进来。12% 是上限。"""
    assert 0 < page_labels.FOOTER_BAND <= 0.15


# --------------------------------------------------------------------------- #
# 结构解析
# --------------------------------------------------------------------------- #


def test_segments_flatten_nested_kids():
    node = Dictionary(
        Kids=Array(
            [
                Dictionary(Nums=Array([0, Dictionary(S=Name("/D"), St=1)])),
                Dictionary(Nums=Array([4, Dictionary(S=Name("/r"))])),
            ]
        )
    )
    found = page_labels._segments(node)
    assert [index for index, _ in found] == [0, 4]


def test_segments_merge_nums_and_kids():
    node = Dictionary(
        Nums=Array([0, Dictionary(S=Name("/D"), St=1)]),
        Kids=Array([Dictionary(Nums=Array([3, Dictionary(S=Name("/r"))]))]),
    )
    found = page_labels._segments(node)
    assert [index for index, _ in found] == [0, 3]


def test_odd_nums_array_drops_the_dangling_item():
    """`/Nums` 必须成对；奇数长度说明结构坏了，最后那个孤项丢掉。"""
    node = Dictionary(Nums=Array([0, Dictionary(S=Name("/D"), St=1), 5]))
    found = page_labels._segments(node)
    assert len(found) == 1


def test_non_numeric_index_is_skipped():
    node = Dictionary(Nums=Array([Name("/X"), Dictionary(S=Name("/D"), St=1)]))
    assert page_labels._segments(node) == []


@pytest.mark.parametrize("node", [None, 42, "not a dict", Array([])])
def test_non_dictionary_input_yields_nothing(node):
    assert page_labels._segments(node) == []


def test_human_note_says_manual_confirmation_is_needed():
    assert "人工确认" in page_labels.HUMAN_NOTE
