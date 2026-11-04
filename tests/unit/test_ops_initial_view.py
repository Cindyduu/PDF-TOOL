"""R3 初始布局 / R4 初始缩放 / R5 书签面板。

这三项都只改文档级一个键，最容易写成「假成功」，所以每条用例都同时断言
**改完之后的真实值**，而不是只看处理器自己报的 reason_code。
"""

from __future__ import annotations

from pikepdf import Array, Dictionary, Name, String

from app.pdf import initial_view
from app.pdf.context import OpContext
from tests.fixtures.make_fixtures import add_page, build_outline, dest_xyz, new_pdf
from tests.pdf_ops import opened


# --------------------------------------------------------------------------- #
# R3 初始布局
# --------------------------------------------------------------------------- #


def test_page_layout_is_removed(fixture_pdfs):
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        assert str(pdf.Root.get("/PageLayout")) == "/TwoColumnLeft"
        outcome = initial_view.process_initial_layout(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "page_layout_removed"
        assert outcome.before == {"page_layout": "/TwoColumnLeft"}
        assert outcome.after == {"page_layout": None}
        assert outcome.counts == {
            "checked": 1,
            "modified": 1,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 0,
            "failed": 0,
        }
        assert pdf.Root.get("/PageLayout") is None


def test_missing_page_layout_is_compliant(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = initial_view.process_initial_layout(pdf, ctx)

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "no_page_layout"
        assert outcome.counts["compliant"] == 1
        assert outcome.changed is False


def test_page_layout_does_not_get_replaced_with_another_value(fixture_pdfs):
    """R3 要求是「删除」，不是换成 /SinglePage。"""
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        initial_view.process_initial_layout(pdf, ctx)
        assert "/PageLayout" not in pdf.Root.keys()


# --------------------------------------------------------------------------- #
# R4 初始缩放
# --------------------------------------------------------------------------- #


def test_destination_open_action_is_removed(fixture_pdfs):
    with opened(fixture_pdfs / "annots_mixed.pdf") as (pdf, ctx):
        assert isinstance(pdf.Root.get("/OpenAction"), Array)
        outcome = initial_view.process_initial_zoom(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "open_action_removed"
        assert outcome.before == {"open_action": "dest_array"}
        assert outcome.after == {"open_action": None}
        assert pdf.Root.get("/OpenAction") is None


def test_missing_open_action_is_compliant(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = initial_view.process_initial_zoom(pdf, ctx)

        assert outcome.reason_code == "no_open_action"
        assert outcome.counts["compliant"] == 1
        assert outcome.changed is False


def test_named_dest_open_action_is_removed():
    pdf = new_pdf()
    p1 = add_page(pdf)
    add_page(pdf)
    pdf.Root.Names = Dictionary(Dests=Dictionary(Names=Array([String("go"), dest_xyz(p1, 2.0)])))
    pdf.Root.OpenAction = String("go")

    outcome = initial_view.process_initial_zoom(pdf, OpContext.build(pdf))

    assert outcome.before == {"open_action": "named_dest"}
    assert outcome.counts["modified"] == 1
    assert pdf.Root.get("/OpenAction") is None


def test_goto_open_action_is_removed():
    pdf = new_pdf()
    p1 = add_page(pdf)
    add_page(pdf)
    pdf.Root.OpenAction = Dictionary(S=Name("/GoTo"), D=dest_xyz(p1, 2.0))

    outcome = initial_view.process_initial_zoom(pdf, OpContext.build(pdf))

    assert outcome.before == {"open_action": "goto"}
    assert outcome.counts["modified"] == 1
    assert pdf.Root.get("/OpenAction") is None


def test_javascript_open_action_is_kept():
    """R4 只处理「打开时跳转」，脚本行为绝不能误删。"""
    pdf = new_pdf()
    add_page(pdf)
    pdf.Root.OpenAction = Dictionary(
        S=Name("/JavaScript"), JS=String("app.alert('hello')")
    )

    outcome = initial_view.process_initial_zoom(pdf, OpContext.build(pdf))

    assert outcome.status == "not_applicable"
    assert outcome.reason_code == "open_action_not_view"
    assert outcome.counts["not_applicable"] == 1
    assert outcome.changed is False
    assert outcome.warnings
    assert pdf.Root.get("/OpenAction") is not None
    assert str(pdf.Root.OpenAction.S) == "/JavaScript"


# --------------------------------------------------------------------------- #
# R5 书签面板
# --------------------------------------------------------------------------- #


def test_page_mode_is_set_to_use_outlines(fixture_pdfs):
    with opened(fixture_pdfs / "zoom_explicit.pdf") as (pdf, ctx):
        assert str(pdf.Root.get("/PageMode")) == "/UseNone"
        outcome = initial_view.process_bookmark_panel(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "page_mode_set"
        assert outcome.before == {"page_mode": "/UseNone", "has_outline": True}
        assert outcome.after == {"page_mode": "/UseOutlines", "has_outline": True}
        assert str(pdf.Root.PageMode) == "/UseOutlines"
        assert outcome.warnings == []
        assert outcome.next_action is None


def test_page_mode_without_outline_warns(fixture_pdfs):
    """没有书签也要照常设置，同时如实提示面板可能是空的。"""
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = initial_view.process_bookmark_panel(pdf, ctx)

        assert outcome.counts["modified"] == 1
        assert str(pdf.Root.PageMode) == "/UseOutlines"
        assert outcome.before["has_outline"] is False
        assert any("没有书签" in warning for warning in outcome.warnings)
        assert outcome.next_action


def test_already_use_outlines_is_compliant():
    pdf = new_pdf()
    p1 = add_page(pdf)
    add_page(pdf)
    build_outline(pdf, [{"title": "One", "dest": dest_xyz(p1, 2.0)}])
    pdf.Root.PageMode = Name("/UseOutlines")

    outcome = initial_view.process_bookmark_panel(pdf, OpContext.build(pdf))

    assert outcome.status == "unchanged"
    assert outcome.reason_code == "already_use_outlines"
    assert outcome.counts["compliant"] == 1
    assert outcome.changed is False


def test_page_mode_other_value_is_overwritten():
    pdf = new_pdf()
    add_page(pdf)
    pdf.Root.PageMode = Name("/UseThumbs")

    outcome = initial_view.process_bookmark_panel(pdf, OpContext.build(pdf))

    assert outcome.before["page_mode"] == "/UseThumbs"
    assert outcome.counts["modified"] == 1
    assert str(pdf.Root.PageMode) == "/UseOutlines"
    assert any("没有书签" in warning for warning in outcome.warnings)
