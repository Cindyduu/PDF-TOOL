"""R2 注释展平。

验收的关键不是「注释数变少了」——直接删注释也能让数量变少。
必须同时证明两件事：
1. 注释从 `/Annots` 里消失了；
2. 外观被**画进页面内容流**（内容流里出现新的 XObject 引用）。

另外三条不许破：`Link` 保留、豁免页零改动、摘不掉的表单域不硬摘。
"""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream, String

from app.pdf import annotations
from app.pdf.context import OpContext
from tests.fixtures.make_fixtures import (
    add_page,
    attach_annots,
    dest_xyz,
    form_xobject,
    make_annot,
    new_pdf,
)
from tests.pdf_ops import annot_subtypes, annots_of, opened, page_content_bytes


def test_mixed_annotations_are_flattened_and_reported(fixture_pdfs):
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        assert annotations.count_annotations(pdf) == 9
        outcome = annotations.process(pdf, ctx)

        # 9 个注释：6 个可展平、1 个链接保留、2 个不可靠
        assert outcome.counts == {
            "checked": 9,
            "modified": 6,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 1,
            "failed": 2,
        }
        assert outcome.status == "failed"
        assert outcome.reason_code == "annotation_flatten_partial"
        # `links` 是 AC-R26-N1 的实测值：处理前后必须相等，所以要能读到
        assert outcome.before == {
            "annotations": 9,
            "pages_with_annotations": 2,
            "links": 1,
        }
        assert outcome.after == {
            "annotations": 3,
            "links": 1,
            "kept_without_appearance": 1,
        }
        assert outcome.next_action

        reasons = " ".join(outcome.warnings)
        assert "没有正常外观" in reasons
        assert "隐藏" in reasons

        # 剩下三个：无外观的 Square、隐藏的 Square、URI Link
        assert annot_subtypes(pdf.pages[0]) == ["Square", "Square", "Link"]
        assert annotations.count_annotations(pdf) == 3


def test_flatten_draws_appearance_into_content_stream(fixture_pdfs):
    """只删注释是「数据丢失」，必须证明外观真的画进了页面。"""
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        before = page_content_bytes(pdf.pages[0])
        assert b"/FlatAnnot" not in before

        annotations.process(pdf, ctx)

        after = page_content_bytes(pdf.pages[0])
        assert b"/FlatAnnot1 Do" in after
        assert after.startswith(before) or before in after, "原有内容不能被覆盖"
        # 新注册的 XObject 必须在页面资源里真实存在
        xobjects = pdf.pages[0].resources.get("/XObject")
        assert isinstance(xobjects, Dictionary)
        assert "/FlatAnnot1" in xobjects


def test_links_are_kept(fixture_pdfs):
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        annotations.process(pdf, ctx)
        links = [
            annot
            for page in pdf.pages
            for annot in annots_of(page)
            if str(annot.get("/Subtype")) == "/Link"
        ]
        assert len(links) == 1
        assert str(links[0].A.URI) == "https://example.com"


def test_widget_is_removed_from_acroform(fixture_pdfs):
    """展平表单域必须同步摘除字段树，否则会留下「画面上有、表单里还在」。"""
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        assert isinstance(pdf.Root.get("/AcroForm"), Dictionary)
        assert len(pdf.Root.AcroForm.Fields) == 1

        annotations.process(pdf, ctx)

        # 字段树空了，`/AcroForm` 一并删除
        assert pdf.Root.get("/AcroForm") is None
        assert not any(
            str(annot.get("/Subtype")) == "/Widget"
            for page in pdf.pages
            for annot in annots_of(page)
        )


def test_rotated_page_annotation_is_flattened(fixture_pdfs):
    """旋转页上的注释也要展平，并且页面 /Rotate 不能被改掉。"""
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        assert int(pdf.pages[1].obj.Rotate) == 90
        annotations.process(pdf, ctx)

        assert int(pdf.pages[1].obj.Rotate) == 90
        assert annots_of(pdf.pages[1]) == []
        assert b"/FlatAnnot1 Do" in page_content_bytes(pdf.pages[1])


def test_only_links_document(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_explicit.pdf") as (pdf, ctx):
        outcome = annotations.process(pdf, ctx)

        assert outcome.reason_code == "only_links"
        assert outcome.counts["not_applicable"] == 2
        assert outcome.counts["modified"] == 0
        assert outcome.status == "not_applicable"
        assert annotations.count_annotations(pdf) == 2


def test_document_without_annotations(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = annotations.process(pdf, ctx)

        assert outcome.reason_code == "no_annotation"
        assert outcome.counts["checked"] == 0


def test_exempt_page_annotations_are_untouched(fixture_pdfs):
    """豁免页整页跳过：注释对象一个字节都不动。"""
    with opened(fixture_pdfs / "exempt_mix.pdf", exempt=(4,)) as (pdf, ctx):
        before = [annot.unparse() for annot in annots_of(pdf.pages[3])]
        assert len(before) == 2

        outcome = annotations.process(pdf, ctx)

        assert outcome.counts == {
            "checked": 4,
            "modified": 1,
            "compliant": 0,
            "exempt": 2,
            "not_applicable": 1,
            "failed": 0,
        }
        after = [annot.unparse() for annot in annots_of(pdf.pages[3])]
        assert after == before, "豁免页上的注释必须原样保留"
        # 非豁免页的高亮已展平，链接保留
        assert annot_subtypes(pdf.pages[1]) == ["Link"]


def test_all_annotations_on_exempt_pages(fixture_pdfs):
    with opened(fixture_pdfs / "exempt_mix.pdf", exempt=(2, 4)) as (pdf, ctx):
        outcome = annotations.process(pdf, ctx)

        assert outcome.reason_code == "all_exempt"
        assert outcome.counts["modified"] == 0
        assert outcome.counts["exempt"] == 4
        assert annotations.count_annotations(pdf) == 4


def test_ambiguous_appearance_state_is_reported():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660), appearance=None)
    annot.AP = Dictionary(
        N=Dictionary(
            Off=form_xobject(pdf, b"0 0 0 RG 1 w 0 0 10 10 re S"),
            On=form_xobject(pdf, b"1 0 0 rg 0 0 10 10 re f"),
        )
    )
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "多个状态" in " ".join(outcome.warnings)
    assert len(annots_of(page)) == 1, "选不出状态就必须保留原对象"


def test_appearance_state_from_as_is_used():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660), appearance=None)
    annot.AP = Dictionary(
        N=Dictionary(
            Off=form_xobject(pdf, b"0 0 0 RG 1 w 0 0 10 10 re S"),
            On=form_xobject(pdf, b"1 0 0 rg 0 0 10 10 re f"),
        )
    )
    annot.AS = Name("/On")
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert annots_of(page) == []


def test_zero_area_rect_is_reported():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 72, 600))
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "矩形" in " ".join(outcome.warnings)


def test_zero_area_bbox_is_reported():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660), appearance=None)
    annot.AP = Dictionary(N=form_xobject(pdf, b"", bbox=(0, 0, 0, 0)))
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "BBox" in " ".join(outcome.warnings)


def test_unsupported_appearance_type_is_reported():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660), appearance=None)
    annot.AP = Dictionary(N=String("not a stream"))
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "不受支持" in " ".join(outcome.warnings)


def test_direct_widget_cannot_be_untracked_safely():
    """域是直接对象时无法在字段树里定位，必须保留原对象并如实报失败。"""
    pdf = new_pdf()
    page = add_page(pdf)
    widget = make_annot(
        pdf,
        "Widget",
        (72, 380, 300, 410),
        extra={"FT": Name("/Tx"), "T": String("fieldA")},
    )
    attach_annots(pdf, page, [widget])
    pdf.Root.AcroForm = Dictionary(Fields=Array([widget]))

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "字段树" in " ".join(outcome.warnings)
    assert len(annots_of(page)) == 1
    assert isinstance(pdf.Root.get("/AcroForm"), Dictionary)


def test_widget_without_acroform_is_still_flattened():
    """没有 `/AcroForm` 就没有字段树可摘，展平本身是安全的。"""
    pdf = new_pdf()
    page = add_page(pdf)
    widget = make_annot(pdf, "Widget", (72, 380, 300, 410), extra={"FT": Name("/Tx")})
    attach_annots(pdf, page, [widget])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert annots_of(page) == []


def test_popup_reference_is_stripped_when_popup_removed():
    pdf = new_pdf()
    page = add_page(pdf)
    popup = make_annot(pdf, "Popup", (330, 560, 520, 640), appearance=None)
    note = make_annot(pdf, "Text", (72, 700, 100, 728))
    note.Popup = popup
    attach_annots(pdf, page, [note, popup])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 2
    assert annots_of(page) == []
    assert note.get("/Popup") is None, "Popup 删掉后不能留下悬空引用"


def test_hidden_flag_is_reported_before_appearance_check():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660), extra={"F": 2})
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 1
    assert "隐藏" in " ".join(outcome.warnings)


def test_count_annotations_ignores_missing_annots():
    pdf = new_pdf()
    add_page(pdf)
    add_page(pdf)
    assert annotations.count_annotations(pdf) == 0

    page = pdf.pages[0]
    page.obj.Annots = Array([Dictionary(Type=Name("/Annot"), Subtype=Name("/Text"))])
    assert annotations.count_annotations(pdf) == 1


def test_xobject_names_do_not_collide():
    """同一页有多个注释时，每个都要拿到独立的 XObject 名字。"""
    pdf = new_pdf()
    page = add_page(pdf)
    annots = [
        make_annot(pdf, "Square", (72, 600 + index * 60, 300, 640 + index * 60))
        for index in range(3)
    ]
    attach_annots(pdf, page, annots)

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 3
    content = page_content_bytes(page)
    for index in (1, 2, 3):
        assert f"/FlatAnnot{index} Do".encode() in content
    xobjects = pdf.pages[0].resources.get("/XObject")
    assert sorted(str(key) for key in xobjects.keys()) == [
        "/FlatAnnot1",
        "/FlatAnnot2",
        "/FlatAnnot3",
    ]


def test_plain_stream_content_is_not_broken():
    """内容流本来就有内容时，追加不能覆盖原文。"""
    pdf = new_pdf()
    page = add_page(pdf, b"0.5 g 0 0 100 100 re f\n")
    annot = make_annot(pdf, "Square", (72, 600, 300, 660))
    attach_annots(pdf, page, [annot])

    annotations.process(pdf, OpContext.build(pdf))

    content = page_content_bytes(page)
    assert content.startswith(b"0.5 g 0 0 100 100 re f")
    assert b"/FlatAnnot1 Do" in content


def test_metadata_stream_on_annotation_does_not_break_flatten():
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(pdf, "Square", (72, 600, 300, 660))
    annot.Metadata = Stream(pdf, b"<x/>")
    attach_annots(pdf, page, [annot])

    outcome = annotations.process(pdf, OpContext.build(pdf))
    assert outcome.counts["modified"] == 1


def test_signed_document_annotations_still_flatten(fixture_pdfs):
    """签名域在流水线层会被拦下，处理器本身不做这个判断。"""
    with opened(fixture_pdfs / "signed.pdf") as (pdf, ctx):
        outcome = annotations.process(pdf, ctx)
        # 签名域有外观、有字段树，展平是可行的；是否允许处理由上层决定
        assert outcome.counts["checked"] == 1
        assert outcome.counts["modified"] + outcome.counts["failed"] == 1


def test_empty_annots_array_is_ignored():
    pdf = new_pdf()
    page = add_page(pdf)
    page.Annots = Array()
    outcome = annotations.process(pdf, OpContext.build(pdf))
    assert outcome.reason_code == "no_annotation"


def test_annotation_page_pointer_is_preserved_for_kept_annots(fixture_pdfs):
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        annotations.process(pdf, ctx)
        for annot in annots_of(pdf.pages[0]):
            assert isinstance(annot.get("/P"), pikepdf.Object)


# --------------------------------------------------------------------------- #
# AC-R26-N1：Link 保留保证（数量 + 目的地逐条比对）
# --------------------------------------------------------------------------- #


def _link_page() -> tuple[pikepdf.Pdf, list]:
    """一页三条内链 + 两个可展平注释，用来验「展平不碰链接」。"""
    pdf = new_pdf()
    page = add_page(pdf)
    page2 = add_page(pdf)
    links = [
        make_annot(
            pdf,
            "Link",
            (72, 700 - index * 20, 300, 715 - index * 20),
            appearance=None,
            extra={"Dest": dest_xyz(page2, None), "Border": Array([0, 0, 0])},
        )
        for index in range(3)
    ]
    squares = [
        make_annot(pdf, "Square", (72, 600 - index * 60, 300, 640 - index * 60))
        for index in range(2)
    ]
    attach_annots(pdf, page, links + squares)
    return pdf, links


def test_link_signature_survives_flattening():
    """AC-R26-N1：链接数与每条 `/Dest` 处理前后完全一致。"""
    pdf, _links = _link_page()
    before = annotations.link_signature(pdf)
    assert len(before) == 3

    outcome = annotations.process(pdf, OpContext.build(pdf))

    after = annotations.link_signature(pdf)
    assert after == before
    assert outcome.before["links"] == 3
    assert outcome.after["links"] == 3
    assert outcome.counts["modified"] == 2
    assert outcome.counts["not_applicable"] == 3


def test_link_signature_reports_missing_dest_as_empty():
    """没有 `/Dest` 的链接（URI 外链）也要占一位，否则数量对不上。"""
    pdf = new_pdf()
    page = add_page(pdf)
    annot = make_annot(
        pdf,
        "Link",
        (72, 700, 300, 720),
        appearance=None,
        extra={"A": Dictionary(S=Name("/URI"), URI=String("https://example.com"))},
    )
    attach_annots(pdf, page, [annot])

    assert annotations.link_signature(pdf) == ((1, ""),)


def test_link_action_dest_is_used_when_dest_key_is_absent():
    pdf = new_pdf()
    page = add_page(pdf)
    page2 = add_page(pdf)
    annot = make_annot(
        pdf,
        "Link",
        (72, 700, 300, 720),
        appearance=None,
        extra={"A": Dictionary(S=Name("/GoTo"), D=dest_xyz(page2, None))},
    )
    attach_annots(pdf, page, [annot])

    signature = annotations.link_signature(pdf)
    assert len(signature) == 1
    assert signature[0][0] == 1
    assert "/XYZ" in signature[0][1]


def test_link_preservation_failure_is_reported_as_failed(monkeypatch):
    """如果链接真的被动过，必须整项判失败——不能悄悄放过。"""
    pdf, _links = _link_page()

    original = annotations.link_signature
    calls = {"count": 0}

    def fake_signature(document):
        calls["count"] += 1
        signature = original(document)
        # 第一次是处理前，必须给真值；第二次是处理后：模拟「少了一条链接」。
        # 两次都造假就等于前后相等，什么也验不出来。
        if calls["count"] >= 2 and len(signature) == 3:
            return signature[1:]
        return signature

    monkeypatch.setattr(annotations, "link_signature", fake_signature)

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.status == "failed"
    assert outcome.reason_code == "link_preservation_failed"
    assert "不可信" in outcome.message
    assert outcome.next_action
    assert any("链接签名" in warning for warning in outcome.warnings)
    # 处理前的值仍要如实写进明细，用户才能看出是哪一边少了
    assert outcome.before["links"] == 3
    assert outcome.after["links"] == 2


# --------------------------------------------------------------------------- #
# AC-R26-E2：无 `/AP` 外观的注释单独计数并写进明细
# --------------------------------------------------------------------------- #


def test_annotations_without_appearance_are_counted_separately():
    pdf = new_pdf()
    page = add_page(pdf)
    without = [
        make_annot(pdf, "Square", (72, 600 - index * 60, 300, 640 - index * 60), appearance=None)
        for index in range(2)
    ]
    flattens = [make_annot(pdf, "Highlight", (72, 400, 300, 420))]
    attach_annots(pdf, page, without + flattens)

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.counts["failed"] == 2
    assert outcome.counts["modified"] == 1
    assert outcome.after["kept_without_appearance"] == 2
    assert "2 处注释无外观，已保留原对象" in outcome.message
    assert "另有" not in outcome.message  # 两个失败全是无外观，不该再报「另有」
    assert len(without) == 2
    assert all(annot.get("/AP") is None for annot in annots_of(page)[:2])


def test_mixed_failure_reasons_are_broken_down():
    """无外观与隐藏两种原因混在一起时，两种都要说清楚。"""
    pdf = new_pdf()
    page = add_page(pdf)
    no_appearance = make_annot(pdf, "Square", (72, 600, 300, 640), appearance=None)
    hidden = make_annot(pdf, "Square", (72, 500, 300, 540), extra={"F": 2})
    attach_annots(pdf, page, [no_appearance, hidden])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.after["kept_without_appearance"] == 1
    assert "1 处注释无外观，已保留原对象" in outcome.message
    assert "另有 1 个注释无法可靠展平，已保留原样" in outcome.message


def test_no_appearance_count_is_zero_when_everything_flattens():
    pdf = new_pdf()
    page = add_page(pdf)
    attach_annots(pdf, page, [make_annot(pdf, "Square", (72, 600, 300, 640))])

    outcome = annotations.process(pdf, OpContext.build(pdf))

    assert outcome.after["kept_without_appearance"] == 0
    assert outcome.message == "已展平 1 个注释，链接注释保持原样"
