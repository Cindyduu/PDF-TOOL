"""R39 元数据与安全校验（v4.2，只读）。

三个子项：DocInfo 字段残留（`warning`）、XMP 元数据流残留（`warning`）、
加密/权限设置（`fail`）。

**为什么「密码」落在这里**：需要打开密码的文件在上传阶段被 R29 拒收，
进不到校验；能进来的只有「仅权限密码」的文件——能打开、能处理，但带着加密
字典和权限限制。工具不解密，所以判 `fail` 并说清「需要人工处理」。

**为什么描述信息只判 `warning`**（D-17）：处理项 ② 一勾就清掉，属于「工具能修
的问题」。判 `fail` 会让「作者名没删」和「文件加密了」同等严重，而后者必须
人工介入——严重度不拉开，用户就没法按顺序处理。

**不挑字段**：处理侧删的是整个 `/Info`，校验也按「整个 `/Info` 存不存在」算。
只查 Author/Title 而放过 Producer，就会出现「校验说干净了、处理却还有东西可删」。
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Dictionary, String

from app.pdf.checks import metadata
from tests.fixtures.make_fixtures import _add_pages, _page_labels, new_pdf, save

XMP = b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"/><?xpacket end="w"?>'


def _doc(tmp_path: Path, name: str, **save_kwargs) -> Path:
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    return save(pdf, tmp_path / name, **save_kwargs)


def _with_info(tmp_path: Path, name: str, fields: dict[str, object]) -> Path:
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    pdf.docinfo = pdf.make_indirect(Dictionary({k: v for k, v in fields.items()}))
    return save(pdf, tmp_path / name)


def test_clean_document_passes(tmp_path, ctx_factory):
    """AC-R39-N1：无 `/Info`、无 `/Metadata`、未加密 → PASS。"""
    path = _doc(tmp_path, "clean.pdf")
    result = metadata.run(ctx_factory(path))

    assert result.key == "metadata_security"
    assert result.status == "pass"
    assert result.summary == "文档描述已清空，无加密设置"
    assert len(result.findings) == 1
    assert result.findings[0].level == "pass"


def test_docinfo_fields_warn_with_field_names(tmp_path, ctx_factory):
    """AC-R39-N2：Author/Title/Producer → WARNING，明细逐字段且含字段名。"""
    path = _with_info(
        tmp_path,
        "info.pdf",
        {
            "/Author": String("张三"),
            "/Title": String("稳定性研究报告"),
            "/Producer": String("Some PDF Writer"),
        },
    )
    result = metadata.run(ctx_factory(path))

    assert result.status == "warning"
    assert result.summary == "残留 3 个描述字段"
    assert len(result.findings) == 3
    messages = " ".join(item.message for item in result.findings)
    for name in ("Author", "Title", "Producer"):
        assert name in messages
    assert all(item.level == "warning" for item in result.findings)


def test_document_level_xmp_warns(tmp_path, ctx_factory):
    """AC-R39-N3：仅文档级 XMP → WARNING，明细写「文档级 XMP 元数据流」。"""
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    pdf.Root.Metadata = pdf.make_stream(XMP)
    path = save(pdf, tmp_path / "doc_xmp.pdf")

    result = metadata.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "残留 1 个文档级 XMP"
    assert any("文档级 XMP 元数据流" in item.message for item in result.findings)


def test_page_level_xmp_warns_with_page_count(tmp_path, ctx_factory):
    """AC-R39-N4：仅页面级 XMP（3 页）→ WARNING，明细写「（3 页）」。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    for page in pages:
        page.Metadata = pdf.make_stream(XMP)
    path = save(pdf, tmp_path / "page_xmp.pdf")

    result = metadata.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "残留 3 页页面级 XMP"
    assert any("页面级 XMP 元数据流（3 页）" in item.message for item in result.findings)


def test_owner_password_only_fails(tmp_path, ctx_factory):
    """AC-R39-N5：仅权限密码加密（能打开）→ FAIL，summary 含「加密」。

    需要打开密码的文件进不到这一步（R29 上传阶段就拒了），所以这里造的是
    `user=""`、只有 owner 密码的文件。
    """
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    path = save(
        pdf,
        tmp_path / "owner_only.pdf",
        encryption=pikepdf.Encryption(owner="owner-pass", user="", R=6),
    )

    result = metadata.run(ctx_factory(path))
    assert result.status == "fail"
    assert "加密" in result.summary
    assert any(item.level == "fail" for item in result.findings)
    # 工具不解密，文案必须说清要人工处理，否则用户会等一个不会发生的结果
    assert "不自动解密" in result.findings[0].detail


def test_processing_metadata_turns_it_green(tmp_path, ctx_factory):
    """AC-R39-N6：处理侧 `metadata` 跑完 → PASS（同口径回归钉子）。"""
    from app.pdf.context import OpContext
    from app.pdf.metadata import process

    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    pdf.docinfo = pdf.make_indirect(
        Dictionary({"/Author": String("张三"), "/Producer": String("Writer")})
    )
    pdf.Root.Metadata = pdf.make_stream(XMP)

    process(pdf, OpContext.build(pdf))
    path = save(pdf, tmp_path / "after.pdf")

    result = metadata.run(ctx_factory(path))
    assert result.status == "pass", result.summary


def test_broken_info_is_reported_not_swallowed(tmp_path, ctx_factory):
    """AC-R39-E1：`/Info` 不是字典 → WARNING「DocInfo 结构异常」，不抛异常。"""
    pdf = new_pdf()
    _add_pages(pdf, 3)
    _page_labels(pdf, 1)
    pdf.trailer.Info = 42
    path = save(pdf, tmp_path / "bad_info.pdf")

    result = metadata.run(ctx_factory(path))
    assert result.status == "warning"
    assert result.summary == "残留 DocInfo 结构异常"
    assert any("DocInfo 结构异常" in item.message for item in result.findings)


def test_long_and_control_char_values_are_flattened(tmp_path, ctx_factory):
    """AC-R39-E2：超长值截断、换行压平，报告仍是合法单行文本。"""
    long_text = "A" * 200
    path = _with_info(
        tmp_path,
        "long.pdf",
        {
            "/Subject": String(long_text),
            "/Keywords": String("line1\nline2\tline3"),
        },
    )
    result = metadata.run(ctx_factory(path))

    assert result.status == "warning"
    by_name = {item.message.split("：")[-1]: item for item in result.findings}
    subject = by_name["Subject"].detail
    assert subject.startswith("值：")
    value = subject[len("值：") :]
    assert len(value) == metadata.VALUE_LIMIT
    assert value.endswith("…")

    keywords = by_name["Keywords"].detail[len("值：") :]
    assert "\n" not in keywords and "\t" not in keywords
    assert keywords == "line1 line2 line3"
