"""R34 清除高亮和批注。

这一步是**删除**，不是展平：页面上不留任何痕迹，所以不可逆。于是验收的重点
不在「注释数变少了」——随便删都能让数量变少——而在四件必须同时成立的事：

1. 该删的确实删了（`Highlight`/`Underline`/`StrikeOut`/`Squiggly`/`Text`/
   `FreeText`/`Popup`）；
2. 该留的确实还在（`Link`、`Stamp`、`Widget` 及其他类型）；
3. 链接签名与表单字段树**前后比对过**——代码里没写 `del` 不等于它们还在；
4. 结构异常的页只跳过这一页，不影响其他页。

「与扁平化同勾」那条（AC-R34-N5）在流水线上验：只有先删再压平，
页面上才不会留下那块黄色高亮。
"""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary

from app.pdf import remove_annotations
from app.pdf.checks import annotations as annotations_check
from app.pdf.pipeline import run_pipeline
from tests.fixtures.make_fixtures import HIGHLIGHT_AP, STAMP_AP
from tests.pdf_ops import (
    annot_subtypes,
    object_signature,
    opened,
    page_content_bytes,
)

#: 六元组计数的不变量：每一项检查过的注释都必须落到某一个归宿里。
#: 报告要对得上账，这条在多个用例里重复断言是有意的。
BUCKETS = ("modified", "compliant", "exempt", "not_applicable", "failed")


def _assert_counts_balanced(counts: dict[str, int]) -> None:
    assert sum(counts[key] for key in BUCKETS) == counts["checked"]


def _field_nodes(fields) -> int:
    """字段树节点数（含 `/Kids` 递归）。

    这里刻意自己数一遍，不复用 `remove_annotations._count_nodes`：
    拿被测代码的辅助函数去验证被测代码，等于自己证明自己。
    """
    total = 0
    for field in fields:
        if not isinstance(field, Dictionary):
            continue
        total += 1
        kids = field.get("/Kids")
        if isinstance(kids, Array):
            total += _field_nodes(kids)
    return total


def _xobject_blobs(page) -> bytes:
    """页面资源里所有 XObject 流的字节拼接（展平后的图形都落在这里）。"""
    xobjects = page.resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        return b""
    return b"".join(stream.read_bytes() for stream in xobjects.values())


# --------------------------------------------------------------------------- #
# AC-R34-N1
# --------------------------------------------------------------------------- #


def test_removes_only_whitelisted_annotations(fixture_pdfs):
    """3 个高亮 + 2 个图章 + 4 个内链 → 删 3，图章与内链一条不少。"""
    with opened(fixture_pdfs / "remove_annots_mixed.pdf") as (pdf, ctx):
        assert annot_subtypes(pdf.pages[0]) == [
            "Highlight",
            "Highlight",
            "Highlight",
            "Stamp",
            "Stamp",
            "Link",
            "Link",
            "Link",
            "Link",
        ]

        outcome = remove_annotations.process(pdf, ctx)

        # `compliant` 恒为 0 是 PRD §16.3 的口径：这一项只会删或留，
        # 不存在「本来就合规」的注释。
        assert outcome.counts == {
            "checked": 9,
            "modified": 3,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 6,
            "failed": 0,
        }
        _assert_counts_balanced(outcome.counts)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "annotations_removed"
        assert outcome.changed is True
        assert outcome.before == {"annotations": 9, "removable": 3, "links": 4}
        assert outcome.after == {"annotations": 6, "links": 4, "removed": 3}

        # 删的是高亮，留的是图章与链接，且顺序未被打乱
        assert annot_subtypes(pdf.pages[0]) == ["Stamp", "Stamp", "Link", "Link", "Link", "Link"]
        assert "链接与图章保持原样" in outcome.message


def test_stamp_is_kept_even_though_it_is_not_a_link(fixture_pdfs):
    """AC-R34-N1 的另一半：白名单之外一律保留，不能退化成「非 Link 全删」。

    图章常带法律意义（批准章、签名章），删掉是不可逆的越权。
    """
    with opened(fixture_pdfs / "remove_annots_mixed.pdf") as (pdf, ctx):
        remove_annotations.process(pdf, ctx)
        subtypes = annot_subtypes(pdf.pages[0])
        assert subtypes.count("Stamp") == 2
        stamps = [a for a in pdf.pages[0].obj.Annots if str(a.get("/Subtype")) == "/Stamp"]
        # 图章对象本身一个字节都没动：外观与名称都还在
        assert all("/AP" in stamp for stamp in stamps)
        assert all(str(stamp.get("/Name")) == "/Approved" for stamp in stamps)


# --------------------------------------------------------------------------- #
# AC-R34-N2
# --------------------------------------------------------------------------- #


def test_links_only_document_is_untouched(fixture_pdfs):
    """只有 Link 注释 → `not_applicable`，文件一个字节都不该变。"""
    with opened(fixture_pdfs / "remove_annots_links_only.pdf") as (pdf, ctx):
        content_before = page_content_bytes(pdf.pages[0])
        signature_before = object_signature(pdf, pdf.pages[0].obj)

        outcome = remove_annotations.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 2,
            "modified": 0,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 2,
            "failed": 0,
        }
        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "nothing_removable"
        assert outcome.changed is False
        assert outcome.after["removed"] == 0

        # 「文件不变」要真的比对过：内容流与整个页对象都不许变
        assert page_content_bytes(pdf.pages[0]) == content_before
        assert object_signature(pdf, pdf.pages[0].obj) == signature_before
        assert annot_subtypes(pdf.pages[0]) == ["Link", "Link"]


# --------------------------------------------------------------------------- #
# AC-R34-N3
# --------------------------------------------------------------------------- #


def test_exempt_page_annotations_are_kept(fixture_pdfs):
    """豁免页上的高亮不删，计 `exempt`；非豁免页照常删。"""
    with opened(fixture_pdfs / "remove_annots_exempt.pdf", exempt=(4,)) as (pdf, ctx):
        assert annot_subtypes(pdf.pages[0]) == ["Highlight", "Link"]
        assert annot_subtypes(pdf.pages[3]) == ["Highlight", "Link"]

        outcome = remove_annotations.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 4,
            "modified": 1,
            "compliant": 0,
            "exempt": 2,
            "not_applicable": 1,
            "failed": 0,
        }
        _assert_counts_balanced(outcome.counts)
        assert outcome.status == "adjusted"

        assert annot_subtypes(pdf.pages[0]) == ["Link"]
        # 豁免页整页跳过：高亮与链接都原样保留
        assert annot_subtypes(pdf.pages[3]) == ["Highlight", "Link"]


def test_all_annotations_on_exempt_pages_reports_all_exempt(fixture_pdfs):
    """全都落在豁免页上 → `all_exempt`，不要谎称「没有可清除的」。"""
    with opened(fixture_pdfs / "remove_annots_exempt.pdf", exempt=(1, 4)) as (pdf, ctx):
        outcome = remove_annotations.process(pdf, ctx)
        assert outcome.counts["exempt"] == 4
        assert outcome.counts["modified"] == 0
        assert outcome.reason_code == "all_exempt"
        assert outcome.changed is False
        assert annot_subtypes(pdf.pages[0]) == ["Highlight", "Link"]


# --------------------------------------------------------------------------- #
# AC-R34-N4
# --------------------------------------------------------------------------- #


def test_annotation_check_passes_after_removal(fixture_pdfs, tmp_path, ctx_factory):
    """删干净之后，后置的注释校验（R19）必须是 PASS，不能留 warning。

    `check_highlights.pdf` 只有 3 个高亮、没有链接也没有图章，
    所以「全删掉」和「注释校验 PASS」这两件事可以同时成立。
    """
    out = tmp_path / "after_removal.pdf"
    with opened(fixture_pdfs / "check_highlights.pdf") as (pdf, ctx):
        outcome = remove_annotations.process(pdf, ctx)
        assert outcome.counts["modified"] == 3
        pdf.save(out)

    result = annotations_check.run(ctx_factory(out))
    assert result.status == "pass"
    assert result.summary == "未发现批注或高亮"


# --------------------------------------------------------------------------- #
# AC-R34-N5
# --------------------------------------------------------------------------- #


def test_removal_before_flatten_leaves_no_highlight_mark(fixture_pdfs, tmp_path):
    """与扁平化同勾 → 高亮彻底消失（页面上也没有色块），图章仍在。

    这条只能整条流水线一起验：单独跑处理器证明不了「谁先谁后」。
    判据用两种外观里各自独有的填充色标记——
    若顺序反了，高亮的黄色会被先画进内容流，删注释也去不掉。
    """
    out = tmp_path / "removal_then_flatten.pdf"
    result = run_pipeline(
        fixture_pdfs / "remove_annots_mixed.pdf",
        out,
        operations=["remove_annotations", "flatten_annotations"],
    )

    assert [item["key"] for item in result.results] == [
        "remove_annotations",
        "flatten_annotations",
    ]
    removal = result.result_for("remove_annotations")
    assert removal["counts"]["modified"] == 3
    # 顺序必须写进结果页（PRD §16.3 行为约定），否则用户不知道高亮去哪了
    assert any("先清除再压平" in warning for warning in removal["warnings"])

    with pikepdf.open(out) as pdf:
        page = pdf.pages[0]
        # 高亮被删、图章被压平，只剩下 4 个链接
        assert annot_subtypes(page) == ["Link"] * 4

        blobs = _xobject_blobs(page)
        assert HIGHLIGHT_AP not in blobs, "高亮的黄色色块不该出现在页面上"
        assert STAMP_AP in blobs, "图章必须仍然画在页面上"
        # 两个图章各一次 `Do`，一次不多一次不少
        assert page_content_bytes(page).count(b" Do") == 2


# --------------------------------------------------------------------------- #
# AC-R34-E1
# --------------------------------------------------------------------------- #


def test_broken_annots_page_is_skipped_not_fatal(fixture_pdfs):
    """`/Annots` 不是数组 → 该页跳过并记 `failed`，其余页照常处理。

    这条分支只能靠内存注入触发：qpdf 读盘时会把不合规的 `/Annots` 直接丢掉，
    磁盘上造不出这个状态。它防的是「上游给了一份结构异常的文档」，
    所以要在处理器这一层守住——真到了线上才第一次执行就晚了。
    """
    with opened(fixture_pdfs / "remove_annots_exempt.pdf") as (pdf, ctx):
        pdf.pages[0].obj["/Annots"] = 42

        outcome = remove_annotations.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 3,
            "modified": 1,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 1,
            "failed": 1,
        }
        _assert_counts_balanced(outcome.counts)
        assert outcome.status == "failed"
        assert outcome.reason_code == "annotation_removal_partial"
        assert any("第 1 页" in warning for warning in outcome.warnings)
        assert outcome.next_action

        # 坏掉的那一页原样不动
        assert pdf.pages[0].obj.get("/Annots") == 42
        # 其余页照常处理：第 4 页的高亮删掉，链接留下
        assert annot_subtypes(pdf.pages[3]) == ["Link"]
        # `before` 里的可删计数跳过坏页，只数得到第 4 页那一个
        assert outcome.before["removable"] == 1


# --------------------------------------------------------------------------- #
# AC-R34-E2
# --------------------------------------------------------------------------- #


def test_form_tree_is_untouched(fixture_pdfs):
    """删除后 `/AcroForm /Fields` 与 `Widget` 数量不变。

    样本的字段树是「父域 + 一个 Kid」：节点数 2、页面表单域 1。
    只看 `len(/Fields)` 的实现会被这个结构骗过去，所以两个口径都要数。
    """
    with opened(fixture_pdfs / "remove_annots_form.pdf") as (pdf, ctx):
        fields_before = _field_nodes(pdf.Root.AcroForm.Fields)
        assert fields_before == 2
        assert annot_subtypes(pdf.pages[0]) == ["Highlight", "Widget"]

        outcome = remove_annotations.process(pdf, ctx)

        assert outcome.counts["modified"] == 1
        assert outcome.reason_code == "annotations_removed"
        # 不是 `widget_tree_changed`——真变了就该整项判失败
        assert outcome.status == "adjusted"

        assert "/AcroForm" in pdf.Root
        assert _field_nodes(pdf.Root.AcroForm.Fields) == fields_before
        assert annot_subtypes(pdf.pages[0]) == ["Widget"]
        widget = pdf.pages[0].obj.Annots[0]
        assert str(widget.get("/T")) == "fieldA"
        assert str(widget.get("/FT")) == "/Tx"
        # 父子关系也没断
        assert str(widget.Parent.get("/T")) == "group"


# --------------------------------------------------------------------------- #
# 结构性补充：Popup 悬空引用、空 `/Annots`
# --------------------------------------------------------------------------- #


def test_popup_reference_is_cleaned_up(fixture_pdfs):
    """删掉 `Popup` 后，保留注释里指向它的 `/Popup` 必须一并消失。

    留下悬空引用，阅读器点开批注时就会指向一个不存在的对象。
    """
    with opened(fixture_pdfs / "remove_annots_popup.pdf") as (pdf, ctx):
        stamp_before = pdf.pages[0].obj.Annots[0]
        assert "/Popup" in stamp_before, "样本里图章应指向那个 Popup"

        outcome = remove_annotations.process(pdf, ctx)

        assert outcome.counts["modified"] == 3  # 第 1 页 Text + Popup，第 2 页 Highlight
        assert outcome.counts["not_applicable"] == 1  # 图章保留
        stamp = pdf.pages[0].obj.Annots[0]
        assert str(stamp.get("/Subtype")) == "/Stamp"
        assert "/Popup" not in stamp, "图章上的 /Popup 必须被摘掉"


def test_empty_annots_key_is_removed(fixture_pdfs):
    """整页注释删空 → 整键删除，不留空数组。

    留一个空 `/Annots` 数组，部分阅读器会报「注释结构异常」。
    """
    with opened(fixture_pdfs / "remove_annots_popup.pdf") as (pdf, ctx):
        assert annot_subtypes(pdf.pages[1]) == ["Highlight"]

        remove_annotations.process(pdf, ctx)

        assert "/Annots" not in pdf.pages[1].obj


def test_document_without_annotations_is_not_applicable(fixture_pdfs):
    """没有任何注释 → `no_annotation`，不是「已清除 0 个」这种含混说法。"""
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = remove_annotations.process(pdf, ctx)
        assert outcome.counts["checked"] == 0
        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "no_annotation"
        assert outcome.changed is False


# --------------------------------------------------------------------------- #
# 白名单本身
# --------------------------------------------------------------------------- #


def test_deleted_subtypes_are_exactly_the_prd_whitelist():
    """白名单是这份代码的核心约定，写多一个就是不可逆的数据丢失。"""
    assert remove_annotations.DELETED_SUBTYPES == frozenset(
        {
            "Highlight",
            "Underline",
            "StrikeOut",
            "Squiggly",
            "Text",
            "FreeText",
            "Popup",
        }
    )
    # 三种必须保留的类型，永远不许进白名单
    for keep in ("Link", "Stamp", "Widget"):
        assert keep not in remove_annotations.DELETED_SUBTYPES
