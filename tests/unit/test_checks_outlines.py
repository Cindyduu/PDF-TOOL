"""R22 书签存在性 + R37 承前缩放校验。

三个判定要分清楚，混了就给出错误建议：

- 页数 ≤5 → skipped（几页的文件有没有书签都能翻完，报 FAIL 只是噪音）；
- 有 `/Outlines` 但 `get_toc()` 为空 → 「存在空书签树」。这和"没有书签树"
  是两回事：前者说明有人动过书签，只是把它清空了；
- 压根没有 `/Outlines` → 报页数并要求补书签。

v4.2 起这一项还查缩放（R37）：存在性缺陷（没书签）判 `fail`，缩放不合规判
`warning`——因为处理项 ① 能把缩放改对，`fail` 会让人以为必须手工重做书签。

本工具**不自动生成书签**（§1.3 第 2 条），所以文案里不能出现"将自动补全"。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name, String

from app.pdf.checks import outlines
from tests.fixtures.make_fixtures import (
    _add_pages,
    _page_labels,
    build_outline,
    dest_xyz,
    new_pdf,
    save,
)


def test_clean_document_passes(check_ctx):
    """AC-R22-N1 / AC-R37-N1：书签齐全且全是承前缩放 → PASS。"""
    result = outlines.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.summary == "3 个书签，全部为承前缩放"


def test_missing_outlines_warns(check_ctx):
    """AC-R40-N5：页数 >5 且无书签 → WARNING，明细写「N 页文档无书签」。

    v4.3（D-18）由 FAIL 下调为 WARNING：缺书签是内容缺失、工具补不了，
    但也不该和「书签树损坏」同级。
    """
    result = outlines.run(check_ctx("check_no_outline.pdf"))
    assert result.status == "warning"
    assert result.summary == "8 页文档无书签"


def test_missing_outlines_says_it_will_not_generate_any(check_ctx):
    """§1.3 第 2 条：不自动生成书签，文案必须说清，否则用户会等一个不会发生的结果。"""
    result = outlines.run(check_ctx("check_no_outline.pdf"))
    assert "不自动生成书签" in result.findings[0].detail


def test_empty_outline_tree_is_reported_separately(check_ctx):
    """PRD 明确要求：空书签树 ≠ 没有书签树。

    两者都算「无可用书签」，但 summary 必须能区分——空树说明有人动过书签
    又清空了，是编辑痕迹。v4.3 起状态统一为 WARNING（>5 页）。
    """
    result = outlines.run(check_ctx("check_empty_outline.pdf"))
    assert result.status == "warning"
    assert result.summary == "存在空书签树"
    assert result.findings[0].detail == "/Outlines 存在但没有任何书签条目（共 8 页）"


def test_short_document_is_skipped(check_ctx):
    """AC-R40-N4：3 页且无书签 → SKIPPED。"""
    result = outlines.run(check_ctx("check_short.pdf"))
    assert result.status == "skipped"
    assert result.summary == "页数 ≤5 且无书签，跳过书签校验"


def test_skipped_finding_explains_the_page_count(check_ctx):
    result = outlines.run(check_ctx("check_short.pdf"))
    assert "共 3 页" in result.findings[0].detail


def test_five_pages_is_still_skipped(tmp_path, ctx_factory):
    """边界：`<= 5` 跳过，5 页也算短文档。"""
    pdf = new_pdf()
    _add_pages(pdf, 5)
    _page_labels(pdf, 1)
    path = save(pdf, tmp_path / "five.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "skipped"


def test_six_pages_without_bookmarks_warns(tmp_path, ctx_factory):
    """边界另一侧：6 页无书签 → WARNING「6 页文档无书签」。"""
    pdf = new_pdf()
    _add_pages(pdf, 6)
    _page_labels(pdf, 1)
    path = save(pdf, tmp_path / "six.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "6 页文档无书签"


def test_outlines_pointing_at_a_non_dictionary_fails(tmp_path, ctx_factory):
    """AC-R22-E1：`/Outlines` 指向非法对象 → 该项 fail「书签树读取失败」，不整体失败。"""
    pdf = new_pdf()
    _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    pdf.Root.Outlines = 42
    path = save(pdf, tmp_path / "bad_outlines.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "书签树读取失败"
    assert "/Outlines 指向的不是字典对象" in result.findings[0].detail


def test_outlines_empty_dictionary_counts_as_empty_tree(tmp_path, ctx_factory):
    """`/Outlines` 是空字典（没有 First/Last）也是空书签树 → WARNING。"""
    pdf = new_pdf()
    _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    pdf.Root.Outlines = pdf.make_indirect(Dictionary(Type=Name("/Outlines")))
    path = save(pdf, tmp_path / "empty_dict.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "存在空书签树"


def test_page_count_is_read_from_context_not_the_document(tmp_path, ctx_factory):
    """页数取 `ctx.page_count`：调用方已经算过一次，不该在这里重复算。"""
    pdf = new_pdf()
    _add_pages(pdf, 8)
    _page_labels(pdf, 1)
    path = save(pdf, tmp_path / "count.pdf")

    # 谎报成 3 页 → 应当走 skipped 分支
    result = outlines.run(ctx_factory(path, page_count=3))
    assert result.status == "skipped"


def test_min_pages_threshold_is_five():
    assert outlines.MIN_PAGES == 5


@pytest.mark.parametrize("name", ["check_clean.pdf", "check_no_outline.pdf"])
def test_no_verdict_promises_automatic_generation(name, check_ctx):
    result = outlines.run(check_ctx(name))
    blob = result.summary + " ".join(item.message + (item.detail or "") for item in result.findings)
    assert "自动补全" not in blob
    assert "已生成书签" not in blob


# --------------------------------------------------------------------------- #
# R37 承前缩放（v4.2）
# --------------------------------------------------------------------------- #

PAGES = 8


def _inherit_pdf(tmp_path, name: str, specs: list[dict]):
    """8 页文档 + 指定书签，落盘后返回路径。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    build_outline(pdf, specs(pages) if callable(specs) else specs)
    return save(pdf, tmp_path / name)


def _inherit_dests(pages, count: int = 12) -> list[dict]:
    """`count` 个书签，全部承前缩放（`/XYZ null`）。"""
    return [
        {"title": f"Chapter {index + 1}", "dest": dest_xyz(pages[index % PAGES], None)}
        for index in range(count)
    ]


def test_all_inherit_zoom_passes(tmp_path, ctx_factory):
    """AC-R37-N1：12 个书签全部 `/XYZ null` → PASS。"""
    path = _inherit_pdf(tmp_path, "all_inherit.pdf", _inherit_dests)
    result = outlines.run(ctx_factory(path))
    assert result.status == "pass"
    assert result.summary == "12 个书签，全部为承前缩放"


def test_some_explicit_zoom_warns_with_page_numbers(tmp_path, ctx_factory):
    """AC-R37-N2：12 个里 3 个 `/FitH` → WARNING，明细 3 条且页号正确。"""

    def specs(pages):
        items = _inherit_dests(pages)
        for index in (2, 5, 9):
            page = pages[index % PAGES]
            items[index]["dest"] = Array([page, Name("/FitH"), 800])
        return items

    path = _inherit_pdf(tmp_path, "some_fit.pdf", specs)
    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "12 个书签，其中 3 个未设为承前缩放"
    assert len(result.findings) == 3
    # 索引 2/5/9 落在第 3/6/2 页（1 基）
    assert {item.page for item in result.findings} == {3, 6, 2}
    assert all(item.level == "warning" for item in result.findings)
    assert "应为 承前缩放（/XYZ null）" in result.findings[0].detail


def test_all_explicit_zoom_warns(tmp_path, ctx_factory):
    """AC-R37-N3：全部 `/Fit` → WARNING，summary 说「均未设为承前缩放」。

    `/Fit` 本身没有缩放位，但在 Acrobat 里显示「适合页面」而不是「默认」，
    所以不算承前缩放——这一条与处理侧改写规则同源。
    """

    def specs(pages):
        return [
            {"title": f"Chapter {index + 1}", "dest": Array([pages[index % PAGES], Name("/Fit")])}
            for index in range(12)
        ]

    path = _inherit_pdf(tmp_path, "all_fit.pdf", specs)
    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "12 个书签均未设为承前缩放"
    assert len(result.findings) == 12


def test_title_only_nodes_are_not_counted_in_denominator(tmp_path, ctx_factory):
    """AC-R37-N4：纯标题节点（无 `/Dest`）不计入分母，不影响结论。"""

    def specs(pages):
        items = _inherit_dests(pages)
        items.insert(0, {"title": "Part One", "dest": None})
        items.append({"title": "Appendix", "dest": None})
        return items

    path = _inherit_pdf(tmp_path, "title_only.pdf", specs)
    result = outlines.run(ctx_factory(path))
    assert result.status == "pass"
    assert "全部为承前缩放" in result.summary
    # 分母只算有页面目的地的 12 个，两个纯标题节点被排除
    assert "12 个指向本文档页面的书签均为 /XYZ null" in result.findings[0].detail


def test_short_document_with_bookmarks_still_checks_zoom(tmp_path, ctx_factory):
    """AC-R40-N3：4 页文档有书签 → 仍然要查承前缩放（**v4.3 核心变更**）。

    改之前这里第一句就 `return skipped`，短文件的书签缩放问题永远查不出来。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, 4)
    _page_labels(pdf, 1)
    build_outline(
        pdf,
        [{"title": "Chapter 1", "dest": Array([pages[0], Name("/Fit")])}],
    )
    path = save(pdf, tmp_path / "short_fit.pdf")
    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert "未设为承前缩放" in result.summary


def test_processing_then_checking_agrees(tmp_path, ctx_factory):
    """AC-R37-N6：处理侧 `bookmarks` 跑完再校验 → PASS。

    这是处理与校验同口径的回归钉子：两边若各写一套遍历，这里就会红。
    """
    from app.pdf.bookmarks import process as process_bookmarks
    from app.pdf.context import OpContext

    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    build_outline(
        pdf,
        [
            {"title": f"Chapter {index + 1}", "dest": Array([pages[index % PAGES], Name("/FitH"), 800])}
            for index in range(6)
        ],
    )
    outcome = process_bookmarks(pdf, OpContext.build(pdf))
    assert outcome.counts["checked"] == 6
    assert outcome.counts["modified"] + outcome.counts["compliant"] == 6
    path = save(pdf, tmp_path / "after_process.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "pass"
    assert result.summary == "6 个书签，全部为承前缩放"


def test_missing_named_destination_is_unresolved_not_a_failure(tmp_path, ctx_factory):
    """AC-R37-E1：命名目标不存在 → 计入 unresolved，记 WARNING，不整体失败。"""

    def specs(pages):
        items = _inherit_dests(pages, count=4)
        items.append({"title": "Broken", "dest": String("NoSuchTarget")})
        return items

    path = _inherit_pdf(tmp_path, "missing_named.pdf", specs)
    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert "目标无法解析" in result.summary
    assert any("目标无法解析" in item.message for item in result.findings)


def test_cyclic_outline_returns_without_hanging(tmp_path, ctx_factory):
    """AC-R37-E2：`/First` 成环 → 遍历有环保护，快速返回，不挂起。

    `get_toc()` 读不出成环的树（返回空），不能据此说「存在空书签树」——
    那会把「有人清空了书签」和「书签树坏了」混成一件事。用环保护遍历能走通
    就说明树里有节点，单独报「书签树结构异常」。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    node = pdf.make_indirect(Dictionary(Title=String("Loop"), Dest=dest_xyz(pages[0], None)))
    node.Next = node
    tree = pdf.make_indirect(Dictionary(Type=Name("/Outlines"), First=node, Last=node, Count=1))
    node.Parent = tree
    pdf.Root.Outlines = tree
    path = save(pdf, tmp_path / "cycle.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "书签树结构异常"
    assert "成环" in result.findings[0].detail


# --------------------------------------------------------------------------- #
# R40 页数门槛与承前缩放解耦（v4.3）
#
# 核心是「先看有没有书签，再按页数决定没有书签算不算问题」。这一组用例
# 专门钉住四种组合，防止以后有人图省事把页数判断挪回函数第一句。
# --------------------------------------------------------------------------- #


def test_three_pages_with_compliant_bookmarks_passes(tmp_path, ctx_factory):
    """AC-R40-N1（短文件版）：3 页、2 个书签全为 `/XYZ null` → PASS。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    build_outline(
        pdf,
        [
            {"title": "Chapter 1", "dest": dest_xyz(pages[0], None)},
            {"title": "Chapter 2", "dest": dest_xyz(pages[1], None)},
        ],
    )
    path = save(pdf, tmp_path / "short_inherit.pdf")
    result = outlines.run(ctx_factory(path))
    assert result.status == "pass"
    assert "全部为承前缩放" in result.summary


def test_eight_pages_all_fith_warns(tmp_path, ctx_factory):
    """AC-R40-N2：8 页、6 个书签全为 `/FitH` → WARNING，明细 6 条。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    build_outline(
        pdf,
        [
            {"title": f"Chapter {index + 1}", "dest": Array([pages[index], Name("/FitH"), 800])}
            for index in range(6)
        ],
    )
    path = save(pdf, tmp_path / "eight_fith.pdf")
    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert "6 个书签均未设为承前缩放" == result.summary
    assert len([item for item in result.findings if "未设为承前缩放" in item.message]) == 6


def test_three_pages_with_empty_outline_tree_is_skipped(tmp_path, ctx_factory):
    """AC-R40-N7：3 页 + `/Outlines` 空 → SKIPPED（短文件不要求书签）。

    空树仍然要说明白：detail 里必须写「/Outlines 存在但没有任何条目」，
    否则用户不知道文件里挂着一个空壳。
    """
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    pdf.Root.Outlines = pdf.make_indirect(Dictionary(Type=Name("/Outlines")))
    path = save(pdf, tmp_path / "short_empty_tree.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "skipped"
    assert "/Outlines 存在但没有任何条目" in result.findings[0].detail


def test_three_pages_with_cyclic_outline_still_fails(tmp_path, ctx_factory):
    """AC-R40-N9：3 页 + `/Outlines` 成环 → FAIL（**结构异常不受页数豁免**）。

    树里确实有条目，只是读不出来——页数少不代表树可以坏。
    """
    pdf = new_pdf()
    pages = _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    node = pdf.make_indirect(Dictionary(Title=String("Loop"), Dest=dest_xyz(pages[0], None)))
    node.Next = node
    tree = pdf.make_indirect(Dictionary(Type=Name("/Outlines"), First=node, Last=node, Count=1))
    node.Parent = tree
    pdf.Root.Outlines = tree
    path = save(pdf, tmp_path / "short_cycle.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "书签树结构异常"


def test_five_pages_with_bookmarks_is_checked_not_skipped(tmp_path, ctx_factory):
    """边界：正好 5 页 + 有书签 → 仍然要查（`≤5` 只决定「没有书签」怎么判）。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 5)
    _page_labels(pdf, 1)
    build_outline(
        pdf,
        [{"title": "Chapter 1", "dest": Array([pages[0], Name("/Fit")])}],
    )
    path = save(pdf, tmp_path / "five_with_bookmark.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "warning"
    assert "未设为承前缩放" in result.summary


def test_skipped_only_happens_when_there_is_no_usable_bookmark(tmp_path, ctx_factory):
    """`skipped` 的唯一入口是「短文档 + 无可用书签」，不能有第二种成因。"""
    pdf = new_pdf()
    _add_pages(pdf, 2)
    _page_labels(pdf, 1)
    path = save(pdf, tmp_path / "two_plain.pdf")

    result = outlines.run(ctx_factory(path))
    assert result.status == "skipped"
    assert "无书签" in result.summary
    assert result.findings[0].level == "skipped"
