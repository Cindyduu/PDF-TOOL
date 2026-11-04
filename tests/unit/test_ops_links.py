"""R9 链接承前缩放。

与书签共用同一套目的地解析，但有两处关键差异，必须钉死：
- 豁免按**链接所在页**判定（书签按目标页）；
- URI 链接没有页面缩放语义，必须原样保留且仍然可点击。
"""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from app.pdf import links
from app.pdf.context import OpContext
from tests.fixtures.make_fixtures import (
    add_page,
    attach_annots,
    dest_xyz,
    make_annot,
    new_pdf,
)
from tests.pdf_ops import opened


def test_explicit_zoom_links_are_adjusted(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_explicit.pdf") as (pdf, ctx):
        assert len(list(links.iter_links(pdf))) == 2
        outcome = links.process(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "explicit_zoom_removed"
        assert outcome.counts == {
            "checked": 2,
            "modified": 2,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 0,
            "failed": 0,
        }
        assert outcome.before["explicit_zoom"] == 2
        assert outcome.after["explicit_zoom"] == 0

        for _page, annot in links.iter_links(pdf):
            slot, key = links.link_dest_slot(annot)
            assert slot[key][4] is None


def test_uri_and_named_and_internal_links_are_separated(fixture_pdfs):
    """URI 不适用、命名目标要复制后改、内部目标直接改。"""
    with opened(fixture_pdfs / "links_mixed.pdf") as (pdf, ctx):
        named_before = ctx.named["goNamed"]
        assert float(named_before[4]) == 1.8

        outcome = links.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 3,
            "modified": 2,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 1,
            "failed": 0,
        }
        assert outcome.before == {"links": 3, "internal": 2, "explicit_zoom": 2}
        assert outcome.after == {"links": 3, "internal": 2, "explicit_zoom": 0}

        # URI 链接的动作原样保留
        uri_annot = next(
            annot
            for _page, annot in links.iter_links(pdf)
            if isinstance(annot.get("/A"), Dictionary)
            and str(annot.A.get("/S")) == "/URI"
        )
        assert str(uri_annot.A.URI) == "https://example.com"

        # 命名目标原件不动，链接拿到的是改过的复制品（按 Rect 定位那条命名链接）。
        # 这里的目标数组是直接对象，objgen 都是 (0,0)，所以只能按「值」判断：
        # 原件仍是 1.8，链接自己已经是 null。
        assert float(ctx.named["goNamed"][4]) == 1.8
        named_annot = next(
            annot
            for _page, annot in links.iter_links(pdf)
            if [float(v) for v in annot.Rect] == [72, 560, 300, 580]
        )
        assert named_annot.Dest[4] is None
        assert any("共用同一个目标" in w for w in outcome.warnings)


def test_inherit_links_are_unchanged(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_inherit.pdf") as (pdf, ctx):
        outcome = links.process(pdf, ctx)

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "already_inherit"
        assert outcome.counts["compliant"] == 1
        assert outcome.counts["modified"] == 0


def test_document_without_links(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = links.process(pdf, ctx)

        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "no_link"
        assert outcome.counts["checked"] == 0


def test_only_uri_links_report_no_internal_link():
    pdf = new_pdf()
    page = add_page(pdf)
    uri = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/URI"), URI=String("https://example.com")),
            "Border": Array([0, 0, 0]),
        },
    )
    attach_annots(pdf, page, [uri])

    outcome = links.process(pdf, OpContext.build(pdf))

    assert outcome.reason_code == "no_internal_link"
    assert outcome.counts["not_applicable"] == 1
    assert outcome.status == "not_applicable"


def test_exempt_is_decided_by_page_holding_the_link(fixture_pdfs):
    """第 4 页上的链接整条豁免；第 2 页上的照常处理。"""
    with opened(fixture_pdfs / "exempt_mix.pdf", exempt=(4,)) as (pdf, ctx):
        outcome = links.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 2,
            "modified": 1,
            "compliant": 0,
            "exempt": 1,
            "not_applicable": 0,
            "failed": 0,
        }
        exempt_link = pdf.pages[3].obj.Annots[1]
        assert float(exempt_link.Dest[4]) == 3.0, "豁免页链接的缩放必须原样保留"
        assert pdf.pages[1].obj.Annots[1].Dest[4] is None


def test_exempt_page_link_sharing_named_target_leaves_target_intact(fixture_pdfs):
    """豁免页链接用的命名目标，不能被任何处理顺带改掉。"""
    with opened(fixture_pdfs / "named_dest_shared.pdf", exempt=(4,)) as (pdf, ctx):
        before = float(ctx.named["sharedTarget"][4])
        outcome = links.process(pdf, ctx)

        assert outcome.counts["checked"] == 1
        assert outcome.counts["exempt"] == 1
        assert outcome.reason_code == "all_exempt"
        assert float(ctx.named["sharedTarget"][4]) == before


def test_link_dest_slot_shapes():
    via_dest = Dictionary(Dest=Array([]))
    assert links.link_dest_slot(via_dest) == (via_dest, "/Dest")

    action = Dictionary(S=Name("/GoTo"), D=Array([]))
    via_action = Dictionary(A=action)
    assert links.link_dest_slot(via_action) == (action, "/D")

    uri = Dictionary(A=Dictionary(S=Name("/URI"), URI=String("https://example.com")))
    assert links.link_dest_slot(uri) == (None, None)


def test_goto_action_link_is_adjusted():
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={
            "A": Dictionary(S=Name("/GoTo"), D=dest_xyz(p2, 2.0)),
            "Border": Array([0, 0, 0]),
        },
    )
    attach_annots(pdf, p1, [link])

    outcome = links.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert link.A.D[4] is None


def test_link_count_is_preserved_after_processing(fixture_pdfs):
    """改缩放不能顺手删链接——链接数量必须前后一致。"""
    with opened(fixture_pdfs / "links_mixed.pdf") as (pdf, ctx):
        before = len(list(links.iter_links(pdf)))
        links.process(pdf, ctx)
        after = len(list(links.iter_links(pdf)))
        assert before == after == 3


def test_fit_link_destination_is_not_applicable():
    pdf = new_pdf()
    p1 = add_page(pdf)
    p2 = add_page(pdf)
    link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": Array([p2, Name("/FitH"), 700]), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [link])

    outcome = links.process(pdf, OpContext.build(pdf))

    assert outcome.counts["not_applicable"] == 1
    assert len(link.Dest) == 3


def test_dangling_named_target_is_failed():
    pdf = new_pdf()
    p1 = add_page(pdf)
    link = make_annot(
        pdf,
        "Link",
        (72, 600, 300, 620),
        appearance=None,
        extra={"Dest": String("ghost"), "Border": Array([0, 0, 0])},
    )
    attach_annots(pdf, p1, [link])

    outcome = links.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert outcome.status == "failed"
    assert outcome.reason_code == "link_zoom_failed"
    assert outcome.next_action


def test_non_dictionary_annots_are_skipped():
    pdf = new_pdf()
    page = add_page(pdf)
    page.Annots = Array([pikepdf.String("not an annot")])

    outcome = links.process(pdf, OpContext.build(pdf))
    assert outcome.reason_code == "no_link"
