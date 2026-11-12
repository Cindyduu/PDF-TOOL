"""R18 文本可提取性校验。

**不做 OCR**（§1.3 第 3 条）。这一项只回答一个问题：下游系统能不能读出文本。

两种不可用要分开说，因为处理方式完全不同：

- 提取结果为空 → 没有文本层，扫描件；
- 有文本层但可打印字符占比 < 30% → 编码坏了（`/ToUnicode` 缺失），
  用户看到的是乱码。

后者更危险：文件"看起来有字"，下游却读出一堆方块。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import text_layer


def test_clean_document_passes(check_ctx):
    """AC-R18-N1：正常文字版 PDF → PASS，明细写「n/n 页可提取文本」。"""
    result = text_layer.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.summary == "8/8 页可提取文本"


def test_scanned_like_fixture_fails(fixture_pdfs, ctx_factory):
    """AC-R18-N2：纯扫描件 → FAIL，明细写「0/n 页可提取文本」。"""
    result = text_layer.run(ctx_factory(fixture_pdfs / "scanned_like.pdf"))
    assert result.status == "fail"
    assert "0/" in result.summary
    assert "疑似扫描件" in result.summary


def test_fail_message_says_no_ocr_will_be_done(fixture_pdfs, ctx_factory):
    result = text_layer.run(ctx_factory(fixture_pdfs / "scanned_like.pdf"))
    blob = " ".join(item.message + (item.detail or "") for item in result.findings)
    assert "不做 OCR" in blob


def test_partially_scanned_document_warns(check_ctx):
    """AC-R18-N3：混合文档 → WARNING，明细列出不可用页号区间。"""
    result = text_layer.run(check_ctx("check_mixed_text.pdf"))
    assert result.status == "warning"
    assert result.summary == "4/6 页可提取文本，2 页不可用"


def test_partial_warning_lists_the_missing_pages(check_ctx):
    result = text_layer.run(check_ctx("check_mixed_text.pdf"))
    assert any("第 1-2 页无文本层" in item.message for item in result.findings)


def test_empty_pages_are_explained_as_possible_scans(check_ctx):
    result = text_layer.run(check_ctx("check_mixed_text.pdf"))
    detail = next(item.detail for item in result.findings if "无文本层" in item.message)
    assert "扫描图片" in detail


def test_unreadable_text_layer_is_reported_separately(tmp_path, ctx_factory):
    """有文本层但全是不可打印字符 → 与"扫描件"分开报。

    AC-R18-E1：可打印字符占比 < 30% 判为不可用，明细说明
    「文本层存在但内容不可读」，且结论不得写成「扫描件」。
    """
    from tests.fixtures.make_fixtures import raw_pdf, raw_stream

    # 内容流里塞的是控制字符：pymupdf 能提取出东西，但一个可打印字符都没有
    junk = b"BT /F1 12 Tf 72 700 Td (\\001\\002\\003\\004) Tj ET"
    path = tmp_path / "unreadable.pdf"
    path.write_bytes(
        raw_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                raw_stream(junk),
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            ]
        )
    )

    result = text_layer.run(ctx_factory(path))
    assert result.status == "fail"
    # 这种页面在报告里必须说清"不是没有字，是字读不出来"
    blob = " ".join(item.message for item in result.findings)
    assert "不可读" in blob
    # 而且不能把结论写成"扫描件"——那会让用户去跑 OCR，方向就错了
    assert "扫描件" not in result.summary
    assert "编码" in result.summary


def test_document_without_pages_is_skipped(tmp_path, ctx_factory):
    from tests.fixtures.make_fixtures import new_pdf, save

    pdf = new_pdf()
    path = save(pdf, tmp_path / "empty.pdf")
    result = text_layer.run(ctx_factory(path, page_count=0))
    assert result.status == "skipped"
    assert result.summary == "文档没有页面"


def test_printable_threshold_is_thirty_percent():
    assert text_layer.PRINTABLE_MIN == 0.30


@pytest.mark.parametrize(
    ("text", "expected_state"),
    [
        ("", text_layer.EMPTY),
        ("   \n\t ", text_layer.EMPTY),
        ("ordinary words", text_layer.OK),
        ("\x01\x02\x03\x04", text_layer.UNREADABLE),
    ],
)
def test_page_state_classification(text, expected_state):
    class FakePage:
        def get_text(self, _mode):
            return text

    state, _ratio = text_layer._page_state(FakePage())
    assert state == expected_state


def test_whitespace_only_pages_count_as_empty():
    class FakePage:
        def get_text(self, _mode):
            return "\n\n   \n"

    state, ratio = text_layer._page_state(FakePage())
    assert state == text_layer.EMPTY
    assert ratio == 0.0
