"""R6 清除文档描述。

「清过了」不算数，所以这里既断言处理器的返回值，也用 `verify.metadata_problems`
从文件结构侧独立复核一遍——两处都过才算真的清干净。
"""

from __future__ import annotations

from pikepdf import Dictionary, Name, Stream, String

from app.pdf import metadata
from app.pdf.context import OpContext
from app.pdf.verify import metadata_problems
from tests.fixtures.make_fixtures import add_page, new_pdf
from tests.pdf_ops import opened


def test_docinfo_and_xmp_are_cleared(fixture_pdfs):
    with opened(fixture_pdfs / "meta_full.pdf") as (pdf, ctx):
        before = metadata.describe(pdf)
        assert before["info_fields"], "样本必须真的带 DocInfo"
        assert before["has_xmp"] is True

        outcome = metadata.process(pdf, ctx)

        assert outcome.status == "adjusted"
        assert outcome.reason_code == "metadata_cleared"
        assert outcome.counts == {
            "checked": 1,
            "modified": 1,
            "compliant": 0,
            "exempt": 0,
            "not_applicable": 0,
            "failed": 0,
        }
        assert outcome.after == {
            "info_fields": [],
            "has_xmp": False,
            "pages_with_xmp": 0,
        }
        assert outcome.before["info_fields"] == [
            "Author",
            "Creator",
            "Keywords",
            "Producer",
            "Subject",
            "Title",
        ]
        # 结构侧独立复核
        assert metadata_problems(pdf) == []


def test_message_mentions_removed_parts(fixture_pdfs):
    with opened(fixture_pdfs / "meta_full.pdf") as (pdf, ctx):
        outcome = metadata.process(pdf, ctx)
        assert "DocInfo" in outcome.message
        assert "XMP" in outcome.message


def test_clean_document_is_compliant(fixture_pdfs):
    with opened(fixture_pdfs / "meta_clean.pdf") as (pdf, ctx):
        outcome = metadata.process(pdf, ctx)

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "already_clean"
        assert outcome.counts["compliant"] == 1
        assert outcome.changed is False


def test_document_without_any_metadata_is_compliant(fixture_pdfs):
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        assert metadata.describe(pdf) == {
            "info_fields": [],
            "has_xmp": False,
            "pages_with_xmp": 0,
        }
        outcome = metadata.process(pdf, ctx)
        assert outcome.reason_code == "already_clean"


def test_xmp_only_document():
    pdf = new_pdf()
    add_page(pdf)
    pdf.Root.Metadata = Stream(pdf, b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")

    outcome = metadata.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert outcome.before["has_xmp"] is True
    assert outcome.after["has_xmp"] is False
    assert "XMP" in outcome.message
    assert metadata_problems(pdf) == []


def test_page_level_metadata_is_cleared():
    pdf = new_pdf()
    page = add_page(pdf)
    page.Metadata = Stream(pdf, b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")

    assert metadata.describe(pdf)["pages_with_xmp"] == 1
    outcome = metadata.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert outcome.after["pages_with_xmp"] == 0
    assert "页面级" in outcome.message
    assert page.get("/Metadata") is None


def test_docinfo_only_document():
    pdf = new_pdf()
    add_page(pdf)
    pdf.docinfo = pdf.make_indirect(
        Dictionary(Title=String("Title"), Author=String("Someone"))
    )

    outcome = metadata.process(pdf, OpContext.build(pdf))

    assert outcome.counts["modified"] == 1
    assert outcome.before["info_fields"] == ["Author", "Title"]
    assert outcome.after["info_fields"] == []
    assert "DocInfo" in outcome.message
    assert "XMP" not in outcome.message
    assert metadata_problems(pdf) == []


def test_empty_info_dictionary_counts_as_clean():
    pdf = new_pdf()
    add_page(pdf)
    pdf.docinfo = pdf.make_indirect(Dictionary())

    outcome = metadata.process(pdf, OpContext.build(pdf))
    assert outcome.reason_code == "already_clean"


def test_malformed_info_is_reported_by_verify():
    """`/Info` 不是字典时不能崩，也不能当作「已经干净」。"""
    pdf = new_pdf()
    add_page(pdf)
    pdf.trailer["/Info"] = String("not a dictionary")

    problems = metadata_problems(pdf)
    assert problems == ["trailer /Info 不是字典"]

    # 处理器不硬删畸形对象，但也不会崩溃
    outcome = metadata.process(pdf, OpContext.build(pdf))
    assert outcome.status in {"adjusted", "unchanged", "not_applicable"}


def test_describe_is_read_only():
    pdf = new_pdf()
    add_page(pdf)
    pdf.docinfo = pdf.make_indirect(Dictionary(Title=String("Keep")))
    snapshot = metadata.describe(pdf)
    assert snapshot["info_fields"] == ["Title"]
    assert pdf.trailer.Info.Title == "Keep"


def test_metadata_key_removal_is_idempotent(fixture_pdfs):
    with opened(fixture_pdfs / "meta_full.pdf") as (pdf, ctx):
        metadata.process(pdf, ctx)
        second = metadata.process(pdf, ctx)
        assert second.reason_code == "already_clean"
        assert second.changed is False


def test_language_and_markinfo_are_not_touched(fixture_pdfs):
    """R6 只清描述信息，`/Lang` 与 `/MarkInfo` 属于无障碍结构，不能顺手删。"""
    with opened(fixture_pdfs / "tagged_real.pdf") as (pdf, ctx):
        assert pdf.Root.get("/Lang") is not None
        assert isinstance(pdf.Root.get("/MarkInfo"), Dictionary)
        metadata.process(pdf, ctx)
        assert pdf.Root.get("/Lang") is not None
        assert isinstance(pdf.Root.get("/MarkInfo"), Dictionary)
        assert str(pdf.Root.Lang) == "en-US"
