"""R1 书签承前缩放。

四条硬约束：
1. 显式缩放必须被改成 `null`；
2. 豁免按**目标页**判定，不是按书签挂在哪；
3. 共享目标只能复制后再改，原件一个字节都不许动；
4. `/Fit` 之类没有缩放位的目的地必须**换类型**改成 `/XYZ` + null，
   不能像第一版那样整批跳过。
"""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from app.pdf import bookmarks
from app.pdf.destinations import PageResolver, collect_named_dests
from tests.fixtures.make_fixtures import (
    add_page,
    attach_annots,
    build_outline,
    dest_xyz,
    make_annot,
    new_pdf,
)
from tests.pdf_ops import opened, outline_titles


def test_explicit_zoom_is_replaced_with_inherit(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_explicit.pdf") as (pdf, ctx):
        assert outline_titles(pdf) == ["Chapter 1", "Chapter 2", "Chapter 3"]
        outcome = bookmarks.process(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "explicit_zoom_removed"
        assert outcome.counts == {
            "checked": 3,
            "modified": 3,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 0,
            "failed": 0,
        }
        assert outcome.before == {
            "bookmarks": 3,
            "explicit_zoom": 3,
            "fit_breakdown": {"/XYZ": 3},
        }
        assert outcome.after == {
            "bookmarks": 3,
            "explicit_zoom": 0,
            "fit_breakdown": {"/XYZ": 3},
        }
        assert outcome.changed is True

        # 落地效果：每一项的第 5 项必须是 null，且目标页没变
        targets = []
        for item in bookmarks.iter_outline_items(pdf):
            slot, key = bookmarks.dest_slot(item)
            assert slot is not None
            array = slot[key]
            assert array[4] is None
            targets.append(ctx.resolver.page_number(array[0]))
        assert targets == [1, 2, 3]


def test_inherit_zoom_documents_are_unchanged(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_inherit.pdf") as (pdf, ctx):
        outcome = bookmarks.process(pdf, ctx)

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "already_inherit"
        assert outcome.counts["compliant"] == 2
        assert outcome.counts["modified"] == 0
        assert outcome.changed is False


def test_document_without_outline_is_not_applicable(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = bookmarks.process(pdf, ctx)

        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "no_outline"
        assert outcome.counts["checked"] == 0


def test_exempt_is_decided_by_target_page(fixture_pdfs):
    """Ref 2.7.5 挂在书签树里，但它指向第 4 页——按目标页豁免。"""
    with opened(fixture_pdfs / "exempt_mix.pdf", exempt=(4,)) as (pdf, ctx):
        outcome = bookmarks.process(pdf, ctx)

        assert outcome.counts["checked"] == 4
        assert outcome.counts["modified"] == 3
        assert outcome.counts["exempt"] == 1
        assert outcome.status == "adjusted"

        zoom = {}
        for item in bookmarks.iter_outline_items(pdf):
            slot, key = bookmarks.dest_slot(item)
            zoom[str(item.get("/Title"))] = slot[key][4]
        assert zoom["Ref 2.7.5"] == 2.0, "豁免页的书签必须原样保留"
        assert zoom["Ch1"] is None and zoom["Ch2"] is None and zoom["Ref 3.3"] is None


def test_shared_named_dest_is_copied_not_mutated(fixture_pdfs):
    """命名目标被书签与豁免页链接共用：改书签不能连累那条链接。"""
    with opened(fixture_pdfs / "named_dest_shared.pdf", exempt=(4,)) as (pdf, ctx):
        shared_before = ctx.named["sharedTarget"]
        assert float(shared_before[4]) == 2.5

        outcome = bookmarks.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 3,
            "modified": 2,
            "compliant": 0,
            "exempt": 1,
            "not_applicable": 0,
            "failed": 0,
        }
        # 原件仍在命名树里，缩放因子一个字节没变
        assert float(ctx.named["sharedTarget"][4]) == 2.5
        assert any("共用同一个目标" in warning for warning in outcome.warnings)

        # 书签自己换成了复制品
        target = next(
            item
            for item in bookmarks.iter_outline_items(pdf)
            if str(item.get("/Title")) == "Named shared"
        )
        slot, key = bookmarks.dest_slot(target)
        assert slot[key][4] is None
        assert slot[key].objgen != shared_before.objgen

        # 豁免页那条链接仍然指向同一个名字，解析出来还是 2.5
        annot = pdf.pages[3].obj.Annots[0]
        assert str(annot.Dest) == "sharedTarget"


def test_shared_named_dest_is_not_touched_when_only_exempt_refs():
    """全是豁免引用时，共享目标必须一动不动。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    shared = pdf.make_indirect(dest_xyz(p2, 2.5))
    pdf.Root.Names = Dictionary(
        Dests=Dictionary(Names=Array([String("shared"), shared]))
    )
    build_outline(pdf, [{"title": "Only exempt", "dest": String("shared")}])

    ctx_named = collect_named_dests(pdf)
    ctx = _ctx(pdf, exempt=(2,))
    outcome = bookmarks.process(pdf, ctx)

    assert outcome.counts["exempt"] == 1
    assert outcome.counts["modified"] == 0
    assert outcome.reason_code == "all_exempt"
    assert float(ctx_named["shared"][4]) == 2.5
    assert float(pdf.Root.Names.Dests.Names[1][4]) == 2.5
    assert p1 is not None


def test_fit_destination_is_converted_to_inherit():
    """`/Fit` 没有缩放位，必须换类型改写成 `[页 /XYZ null null null]`。

    第一版把这类目标记成「不适用」跳过，用户要求「全部承前缩放」却一条没改；
    这里钉住的是改写结果，不只是计数。
    """
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    build_outline(
        pdf,
        [
            {"title": "Fit", "dest": Array([p2, Name("/Fit")])},
            {"title": "XYZ", "dest": dest_xyz(p1, 2.0)},
        ],
    )
    outcome = bookmarks.process(pdf, _ctx(pdf))

    assert outcome.counts["not_applicable"] == 0
    assert outcome.counts["modified"] == 2
    assert outcome.reason_code == "fit_converted"
    assert outcome.before["fit_breakdown"] == {"/Fit": 1, "/XYZ": 1}
    assert outcome.after["fit_breakdown"] == {"/XYZ": 2}

    fit_item = next(
        item
        for item in bookmarks.iter_outline_items(pdf)
        if str(item.get("/Title")) == "Fit"
    )
    assert str(fit_item.Dest[1]) == "/XYZ"
    assert len(fit_item.Dest) == 5, "/Fit 必须补齐成 5 项 /XYZ 目标"
    assert fit_item.Dest[2] is None and fit_item.Dest[3] is None
    assert fit_item.Dest[4] is None


def test_goto_action_destination_is_handled():
    """`/A /GoTo /D` 与 `/Dest` 是同一件事，两条路径都要走通。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    build_outline(
        pdf,
        [
            {
                "title": "Via action",
                "action": Dictionary(S=Name("/GoTo"), D=dest_xyz(p2, 1.5)),
            }
        ],
    )
    outcome = bookmarks.process(pdf, _ctx(pdf))

    assert outcome.counts["modified"] == 1
    item = next(iter(bookmarks.iter_outline_items(pdf)))
    slot, key = bookmarks.dest_slot(item)
    assert key == "/D"
    assert slot[key][4] is None


def test_dest_slot_rejects_non_goto_action():
    item = Dictionary(
        Title=String("External"),
        A=Dictionary(S=Name("/URI"), URI=String("https://example.com")),
    )
    assert bookmarks.dest_slot(item) == (None, None)

    plain = Dictionary(Title=String("No dest"))
    assert bookmarks.dest_slot(plain) == (None, None)


def test_bookmark_without_destination_is_not_applicable():
    pdf = new_pdf()
    add_page(pdf)
    add_page(pdf)
    build_outline(pdf, [{"title": "No dest"}])
    outcome = bookmarks.process(pdf, _ctx(pdf))

    assert outcome.counts["checked"] == 1
    assert outcome.counts["not_applicable"] == 1
    assert outcome.status == "not_applicable"


def test_unresolvable_named_dest_is_reported_as_failed():
    pdf = new_pdf()
    add_page(pdf)
    build_outline(pdf, [{"title": "Dangling", "dest": String("missingTarget")}])
    outcome = bookmarks.process(pdf, _ctx(pdf))

    assert outcome.counts["failed"] == 1
    assert outcome.status == "failed"
    assert outcome.reason_code == "bookmark_zoom_failed"
    assert outcome.next_action
    assert any("missingTarget" in w or "无法解析" in w for w in outcome.warnings)


def test_resolver_maps_page_objects_to_one_based_numbers(fixture_pdfs):
    with opened(fixture_pdfs / "exempt_mix.pdf") as (pdf, _ctx_unused):
        resolver = PageResolver(pdf)
        assert resolver.page_count == 5
        for index, page in enumerate(pdf.pages, start=1):
            assert resolver.page_number(page.obj) == index
        assert resolver.page_number(None) is None


def test_link_annotations_do_not_confuse_bookmark_walker():
    """书签遍历只看 `/Outlines`，不能被页面注释带偏。"""
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": dest_xyz(p2, 2.0), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [link])

    outcome = bookmarks.process(pdf, _ctx(pdf))
    assert outcome.reason_code == "no_outline"
    assert float(link.Dest[4]) == 2.0


def _ctx(pdf: pikepdf.Pdf, exempt: tuple[int, ...] = ()):
    from app.pdf.context import OpContext

    return OpContext.build(pdf, exempt)
