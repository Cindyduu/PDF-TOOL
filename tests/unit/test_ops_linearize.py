"""R8 Fast Web View（线性化）。

这一项的验收只能看**磁盘上的文件**：`is_linearized`、`/L` 与文件长度一致、
存在 hint 结构。三项缺一不可，所以用例都对着真实落盘的文件跑。
"""

from __future__ import annotations

import pikepdf

from app.pdf import linearize
from app.pdf.context import OpContext
from app.pdf.verify import verify_pdf_file
from tests.fixtures.make_fixtures import add_page, new_pdf
from tests.pdf_ops import opened


def _write(tmp_path, source, *, linearize_file: bool):
    target = tmp_path / ("linearized.pdf" if linearize_file else "plain.pdf")
    with pikepdf.open(source) as pdf:
        pdf.save(target, linearize=linearize_file, fix_metadata_version=False)
    return target


def test_already_linearized_is_compliant(fixture_pdfs):
    with opened(fixture_pdfs / "linearized.pdf") as (pdf, ctx):
        outcome = linearize.process(pdf, ctx)

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "already_linearized"
        assert outcome.counts == {
            "checked": 1,
            "modified": 0,
            "compliant": 1,
            "exempt": 0,
            "not_applicable": 0,
            "failed": 0,
        }
        assert outcome.changed is False


def test_not_linearized_is_marked_pending(fixture_pdfs):
    """处理器阶段只声明「要改」，实测结果由 `finalize` 回填。"""
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = linearize.process(pdf, ctx)

        assert outcome.counts["modified"] == 1
        assert outcome.reason_code == "linearize_pending"
        assert outcome.before == {"linearized": False}
        assert outcome.after is None
        assert outcome.changed is True


def test_check_accepts_real_linearized_file(fixture_pdfs, tmp_path):
    target = _write(tmp_path, fixture_pdfs / "untagged.pdf", linearize_file=True)
    ok, problems = linearize.check(target)
    assert ok is True
    assert problems == []


def test_check_rejects_plain_file(fixture_pdfs, tmp_path):
    target = _write(tmp_path, fixture_pdfs / "untagged.pdf", linearize_file=False)
    ok, problems = linearize.check(target)
    assert ok is False
    assert any("线性化" in problem for problem in problems)
    assert any("/L" in problem for problem in problems)
    assert any("/H" in problem for problem in problems)


def test_inspect_reports_declared_length_and_hint(fixture_pdfs, tmp_path):
    target = _write(tmp_path, fixture_pdfs / "untagged.pdf", linearize_file=True)
    info = linearize.inspect_linearization(target)

    assert info["linearized"] is True
    assert info["declared_length"] == info["size"]
    assert info["length_matches"] is True
    assert info["has_hint"] is True


def test_finalize_fills_measured_values(fixture_pdfs, tmp_path):
    target = _write(tmp_path, fixture_pdfs / "untagged.pdf", linearize_file=True)
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = linearize.process(pdf, ctx)

    linearize.finalize(outcome, target)

    assert outcome.status == "adjusted"
    assert outcome.reason_code == "linearized"
    assert outcome.after["linearized"] is True
    assert outcome.after["size"] == target.stat().st_size
    assert outcome.counts["modified"] == 1
    assert outcome.counts["failed"] == 0
    assert outcome.warnings == []


def test_finalize_downgrades_when_check_fails(fixture_pdfs, tmp_path):
    """没做成的线性化必须如实报失败，不能留着「已调整」的假象。"""
    target = _write(tmp_path, fixture_pdfs / "untagged.pdf", linearize_file=False)
    with opened(fixture_pdfs / "untagged.pdf") as (pdf, ctx):
        outcome = linearize.process(pdf, ctx)
    assert outcome.changed is True

    linearize.finalize(outcome, target)

    assert outcome.status == "failed"
    assert outcome.reason_code == "linearize_failed"
    assert outcome.counts["modified"] == 0
    assert outcome.counts["failed"] == 1
    assert outcome.changed is False
    assert outcome.after["linearized"] is False
    assert outcome.warnings, "必须把不通过的原因带出来"
    assert outcome.next_action


def test_linearized_output_still_passes_file_verification(fixture_pdfs, tmp_path):
    """线性化不能把文件弄坏：页数、双解析器一致性都要照常通过。"""
    target = _write(tmp_path, fixture_pdfs / "annots_mixed.pdf", linearize_file=True)
    report = verify_pdf_file(target, expected_pages=2)
    assert report.ok, report.problems
    assert report.page_count == 2


def test_linearize_does_not_add_metadata(fixture_pdfs, tmp_path):
    target = _write(tmp_path, fixture_pdfs / "meta_full.pdf", linearize_file=True)
    with pikepdf.open(target) as pdf:
        # 保存前样本本来就带元数据，这里只确认保存过程没有凭空造出 /Metadata
        assert pdf.Root.get("/Metadata") is not None


def test_inspect_tolerates_broken_file(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nnot really a pdf")
    info = linearize.inspect_linearization(broken)
    assert info["linearized"] is False
    assert info["declared_length"] is None

    ok, problems = linearize.check(broken)
    assert ok is False
    assert len(problems) == 3


def test_linearized_document_keeps_pages_and_content(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "text_blocks.pdf"
    target = _write(tmp_path, source, linearize_file=True)

    with pikepdf.open(source) as before, pikepdf.open(target) as after:
        assert len(after.pages) == len(before.pages)
        assert after.pages[0].obj.Contents.read_bytes() == (
            before.pages[0].obj.Contents.read_bytes()
        )


def test_process_on_document_that_was_saved_linearized(tmp_path):
    pdf = new_pdf()
    add_page(pdf)
    target = tmp_path / "lin.pdf"
    pdf.save(target, linearize=True, fix_metadata_version=False)
    pdf.close()

    with pikepdf.open(target) as reopened:
        outcome = linearize.process(reopened, OpContext.build(reopened))
    assert outcome.reason_code == "already_linearized"
