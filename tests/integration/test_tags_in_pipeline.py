"""R7 在流水线里的行为：标签失败不得阻断其他八项。

流水线里最容易出错的地方不是「标签没生成」，而是**标签出问题把整条链带崩**。
这里逐条锁死：

- 扫描件标签失败时，其余八项照常完成，输出仍是有效 PDF；
- 成功时整体是「完成但有警告」，标签项是「待人工复核」；
- 标签结构能扛住最后一步的线性化（顺序错了就会被打散）；
- 用户选择「跳过」时标签项如实显示「不需要修改」。
"""

from __future__ import annotations

import pikepdf
import pytest

from app.pdf.pipeline import run_pipeline
from app.pdf.tags import verify
from app.pdf.verify import verify_pdf_file
from app.schemas import COUNT_KEYS, OPERATION_KEYS, validate_operation_result

BUCKETS = ("modified", "compliant", "exempt", "not_applicable", "failed")


def test_successful_tagging_is_review_required(fixture_pdfs, tmp_path):
    result = run_pipeline(
        fixture_pdfs / "text_blocks.pdf",
        tmp_path / "out.pdf",
        tag_choice="generate",
    )

    item = result.result_for("tagged_pdf")
    assert item["status"] == "review_required"
    assert item["reason_code"] == "tag_generated"
    assert "待人工复核" in item["message"]
    assert result.overall_status == "completed_with_warnings"
    assert "tagged_pdf" in result.changed_operations


def test_output_file_really_contains_tags(fixture_pdfs, tmp_path):
    output = tmp_path / "out.pdf"
    run_pipeline(fixture_pdfs / "text_blocks.pdf", output, tag_choice="generate")

    report = verify.verify_tagged_file(output)
    assert report.ok, report.problems
    assert report.mcids == 8

    # 线性化（fast_web_view）在标签之后执行，结构必须还在
    with pikepdf.open(output) as pdf:
        assert pdf.Root.get("/StructTreeRoot") is not None
        assert pdf.Root.MarkInfo.Marked is True


def test_tag_failure_does_not_block_the_other_eight(fixture_pdfs, tmp_path):
    output = tmp_path / "out.pdf"
    result = run_pipeline(fixture_pdfs / "scanned_like.pdf", output, tag_choice="generate")

    tag_item = result.result_for("tagged_pdf")
    assert tag_item["status"] == "failed"
    assert tag_item["reason_code"] == "tag_no_text"

    others = [item for item in result.results if item["key"] != "tagged_pdf"]
    assert len(others) == len(OPERATION_KEYS) - 1
    assert [item["key"] for item in others] == [
        key for key in OPERATION_KEYS if key != "tagged_pdf"
    ]
    assert all(item["status"] != "failed" for item in others)

    assert result.overall_status == "completed_with_warnings"
    assert verify_pdf_file(output).ok is True, "输出必须是其他八项处理后的有效文件"

    with pikepdf.open(output) as pdf:
        assert pdf.Root.get("/StructTreeRoot") is None, "失败时不许留下空标签树"


def test_skip_choice_shows_not_applicable(fixture_pdfs, tmp_path):
    output = tmp_path / "out.pdf"
    result = run_pipeline(fixture_pdfs / "untagged.pdf", output, tag_choice="skip")

    item = result.result_for("tagged_pdf")
    assert item["status"] == "not_applicable"
    assert item["reason_code"] == "tag_skipped"
    assert "跳过" in item["message"]

    with pikepdf.open(output) as pdf:
        assert pdf.Root.get("/StructTreeRoot") is None
        assert pdf.Root.get("/MarkInfo") is None


def test_unset_tag_choice_is_reported_honestly(fixture_pdfs, tmp_path):
    """没有明确选择时不擅自生成，也不假装是用户的选择。"""
    result = run_pipeline(fixture_pdfs / "untagged.pdf", tmp_path / "out.pdf")

    item = result.result_for("tagged_pdf")
    assert item["status"] == "not_applicable"
    assert item["reason_code"] == "tag_choice_missing"
    assert result.overall_status == "completed"


@pytest.mark.parametrize(
    "sample,choice",
    [
        ("text_blocks.pdf", "generate"),
        ("scanned_like.pdf", "generate"),
        ("tagged_real.pdf", "regenerate"),
        ("untagged.pdf", "skip"),
    ],
)
def test_every_result_is_schema_valid(fixture_pdfs, tmp_path, sample, choice):
    result = run_pipeline(fixture_pdfs / sample, tmp_path / "out.pdf", tag_choice=choice)

    for item in result.results:
        validate_operation_result(item)
        assert set(item["counts"]) == set(COUNT_KEYS)
        assert item["counts"]["checked"] == sum(item["counts"][bucket] for bucket in BUCKETS)

    item = result.result_for("tagged_pdf")
    blob = " ".join([item["message"], item["next_action"] or "", *item["warnings"]])
    assert "完全合规" not in blob
    assert "已通过认证" not in blob


def test_exempt_pages_still_get_tagged(fixture_pdfs, tmp_path):
    """AC-R10-N2：豁免只影响 R1/R2/R9，标签对其他页照常处理。"""
    output = tmp_path / "out.pdf"
    result = run_pipeline(
        fixture_pdfs / "exempt_mix.pdf",
        output,
        exempt_pages=(4,),
        tag_choice="generate",
    )

    assert result.result_for("tagged_pdf")["status"] == "review_required"

    with pikepdf.open(output) as pdf:
        assert pdf.pages[3].obj.get("/StructParents") is not None
    assert verify.verify_tagged_file(output).ok is True
