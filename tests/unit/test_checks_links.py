"""R16 外部链接校验。

**不自动删除**（§1.3 第 9 条）——外链可能是文档故意留的参考资料。

三个容易写歪的地方：

1. 内部跳转（`/GoTo`）不是外链。把它算进来，每个带目录的文件都会告警，
   用户很快就不看报告了（AC-R16-N2 的同类要求）。
2. URI 要截断（AC-R16-N1）。有的参考文献链接长到三百多个字符，
   一条就能把报告版面撑爆。
3. `/Annots` 结构坏了要**跳过该页并记明细**，不能让整份文档的链接结论
   变成"未知"（AC-R16-E1）。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import external_links as links


# --------------------------------------------------------------------------- #
# 协议分类
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://example.com/a", ("https", "网页链接")),
        ("http://example.com", ("http", "网页链接")),
        ("HTTPS://EXAMPLE.COM", ("https", "网页链接")),
        ("mailto:a@example.com", ("mailto", "邮件链接")),
        ("file:///C:/x.pdf", ("file", "本地文件")),
        ("ftp://host/x", ("ftp", "FTP 链接")),
        ("ftps://host/x", ("ftps", "FTP 链接")),
    ],
)
def test_known_schemes_are_classified(uri, expected):
    assert links.classify_scheme(uri) == expected


@pytest.mark.parametrize(
    "uri",
    ["tel:12345", "urn:isbn:123", "javascript:alert(1)", "weird:thing"],
)
def test_unknown_scheme_falls_back_to_other(uri):
    scheme, group = links.classify_scheme(uri)
    assert group == "其他协议"
    assert scheme  # 仍要报出协议名，用户才知道点下去会发生什么


@pytest.mark.parametrize("uri", ["", "C:\\docs\\a.pdf", "no-scheme-at-all", "//host/x"])
def test_uri_without_scheme_is_other(uri):
    assert links.classify_scheme(uri) == ("", "其他协议")


# --------------------------------------------------------------------------- #
# 真实样本
# --------------------------------------------------------------------------- #


def test_http_and_mailto_links_warn(check_ctx):
    result = links.run(check_ctx("check_links_http.pdf"))
    assert result.status == "warning"
    assert result.summary == "发现 3 个外部链接（网页链接 2 个、邮件链接 1 个）"


def test_each_external_link_gets_its_own_finding(check_ctx):
    result = links.run(check_ctx("check_links_http.pdf"))
    assert len(result.findings) == 3
    assert {item.level for item in result.findings} == {"warning"}
    assert all(item.page for item in result.findings)
    assert all(item.detail.startswith("类别：") for item in result.findings)


def test_internal_goto_links_are_not_external(check_ctx):
    # 只有 /GoTo，没有 /URI
    result = links.run(check_ctx("check_links_internal.pdf"))
    assert result.status == "pass"
    assert result.summary == "未发现外部链接"


def test_clean_document_passes(check_ctx):
    result = links.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.findings[0].level == "pass"


def test_long_uri_is_truncated_in_the_finding(check_ctx):
    result = links.run(check_ctx("check_links_http.pdf"))
    for item in result.findings:
        # 前缀「第 N 页：」之外的部分不超过 120 字符
        body = item.message.split("：", 1)[1]
        assert len(body) <= 120


# --------------------------------------------------------------------------- #
# AC-R16-E1：结构损坏只影响那一页
# --------------------------------------------------------------------------- #


def test_broken_annots_page_is_reported_without_killing_the_check(check_ctx):
    # 第 1 页的 `/Annots` 里是悬空引用（pikepdf 读成 None），第 2 页有正常外链
    result = links.run(check_ctx("check_links_broken_annots.pdf"))
    assert result.status == "warning"
    assert any("第 1 页链接读取失败" in item.message for item in result.findings)
    # 坏页不影响其余页：第 2 页的外链照样被捞出来
    assert any("example.com" in item.message for item in result.findings)
    assert result.summary == "发现 1 个外部链接（网页链接 1 个），1 页链接读取失败"


def test_broken_page_alone_still_produces_a_warning(tmp_path, ctx_factory):
    from tests.fixtures.make_fixtures import raw_pdf, raw_stream

    page = b"BT /F1 12 Tf 72 700 Td (only broken page) Tj ET"
    path = tmp_path / "only_broken.pdf"
    path.write_bytes(
        raw_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R "
                b"/Annots [6 0 R] >>",
                raw_stream(page),
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            ]
        )
    )

    result = links.run(ctx_factory(path))
    assert result.status == "warning"
    assert "1 页链接读取失败" in result.summary
    # 有页读不出来时不能说"没有外链"——那几页根本没看过
    assert "未发现可读取的外部链接" in result.summary


def test_non_dictionary_annots_item_marks_the_page_broken(tmp_path, ctx_factory):
    # 数组里混进一个数字：iter_links 会静默跳过它，但这一页必须被点名
    from tests.fixtures.make_fixtures import raw_pdf, raw_stream

    page = b"BT /F1 12 Tf 72 700 Td (numeric annot) Tj ET"
    path = tmp_path / "numeric_annot.pdf"
    path.write_bytes(
        raw_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R "
                b"/Annots [42] >>",
                raw_stream(page),
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            ]
        )
    )

    result = links.run(ctx_factory(path))
    assert result.status == "warning"
    assert "第 1 页链接读取失败" in result.findings[0].message
