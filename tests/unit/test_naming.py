"""阶段 C：文件名安全化与下载名。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.naming import (
    build_output_name,
    content_disposition,
    ensure_pdf_suffix,
    html_content_disposition,
    preferred_download_name,
    sanitize_filename,
    timestamp_slug,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\a.pdf", "a.pdf"),
        ("C:\\x\\y.pdf", "y.pdf"),
        ("/tmp/a.pdf", "a.pdf"),
        ("报告 第1版.pdf", "报告 第1版.pdf"),
        ("a<b>c:d.pdf", "a_b_c_d.pdf"),
        ("  trailing .pdf  ", "trailing.pdf"),
        ("", "document.pdf"),
        (None, "document.pdf"),
        ("...", "document.pdf"),
        ("CON.pdf", "_CON.pdf"),
    ],
)
def test_sanitize_filename(raw, expected) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_rejects_control_chars() -> None:
    assert sanitize_filename("a\x00b\x1fc.pdf") == "abc.pdf"


def test_sanitize_limits_length() -> None:
    name = "长" * 400 + ".pdf"
    result = sanitize_filename(name)
    assert len(result) <= 160
    assert result.endswith(".pdf")


def test_ensure_pdf_suffix() -> None:
    assert ensure_pdf_suffix("report") == "report.pdf"
    assert ensure_pdf_suffix("report.PDF") == "report.PDF"
    assert ensure_pdf_suffix("a.b.pdf") == "a.b.pdf"


def test_build_output_name_contains_marker_and_short_id() -> None:
    moment = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    name = build_output_name("季度报告.pdf", "job-0123456789abcdef", moment)
    assert name.startswith("季度报告_已处理_")
    assert "20260304-050607" in name
    assert name.endswith("abcdef.pdf")


def test_build_output_name_two_jobs_differ() -> None:
    a = build_output_name("a.pdf", "job-aaaaaaaaaaaa1111")
    b = build_output_name("a.pdf", "job-bbbbbbbbbbbb2222")
    assert a != b


def test_same_original_name_does_not_collide(tmp_path) -> None:
    from app.core.atomic import unique_path

    name = build_output_name("同名.pdf", "job-aaaaaaaaaaaa1111")
    first = unique_path(tmp_path, name)
    first.write_bytes(b"x")
    second = unique_path(tmp_path, name)
    assert first != second


def test_content_disposition_has_both_forms() -> None:
    header = content_disposition("报告 第1版.pdf", "inline")
    assert header.startswith("inline; ")
    assert "filename=" in header
    assert "filename*=UTF-8''" in header
    # 目录不能出现在下载名里
    assert "/" not in header.split("filename*=UTF-8''")[1]


def test_content_disposition_rejects_unknown_disposition() -> None:
    assert content_disposition("a.pdf", "evil").startswith("attachment; ")


def test_content_disposition_ascii_fallback_nonempty() -> None:
    header = content_disposition("纯中文名.pdf")
    fallback = header.split('filename="')[1].split('"')[0]
    assert fallback
    assert fallback.lower().endswith(".pdf")


def test_html_content_disposition_falls_back_to_html_not_pdf() -> None:
    """回退名的扩展名必须跟着真实类型走。

    纯中文的 `.html` 名如果退回成 `document.pdf`，Windows 会拿 PDF 阅读器
    去打开一个网页，用户看到空白页还以为导出坏了。
    """
    header = html_content_disposition("批次校验报告.html")
    fallback = header.split('filename="')[1].split('"')[0]
    assert fallback.lower().endswith(".html")
    assert not fallback.lower().endswith(".pdf")
    assert "filename*=UTF-8''" in header


def test_html_content_disposition_keeps_ascii_name_and_rejects_unknown_disposition() -> None:
    header = html_content_disposition("verify-report_bat-1.html", "evil")
    assert header.startswith("attachment; ")
    assert 'filename="verify-report_bat-1.html"' in header
    assert header.endswith("filename*=UTF-8''verify-report_bat-1.html")


def test_timestamp_slug_format() -> None:
    slug = timestamp_slug(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert slug == "20260102-030405"


# --------------------------------------------------------------------------- #
# 下载时用户自报的名字
# --------------------------------------------------------------------------- #

DEFAULT = "报告_已处理_20260102-030405_1aba21.pdf"


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("测试改名.pdf", "测试改名.pdf"),
        ("测试改名", "测试改名.pdf"),
        ("  my report  ", "my report.pdf"),
        ("ab:c*d?.pdf", "ab_c_d_.pdf"),
        # 只给了目录：剥完之后什么都不剩，不能凭空变成 document.pdf
        ("../../etc/", DEFAULT),
        ("C:\\temp\\", DEFAULT),
        ("///", DEFAULT),
        ("   ", DEFAULT),
        ("", DEFAULT),
        (None, DEFAULT),
        # 用户真想要 document 这个名字，就该给他 document.pdf，不是默认名
        ("document", "document.pdf"),
    ],
)
def test_preferred_download_name(requested, expected) -> None:
    assert preferred_download_name(requested, DEFAULT) == expected


def test_preferred_download_name_strips_control_chars() -> None:
    """换行和制表符不能跟着名字进响应头。"""
    got = preferred_download_name("evil\r\nX-Injected: 1.pdf", DEFAULT)
    assert "\r" not in got and "\n" not in got
    assert got == "evilX-Injected_ 1.pdf"
