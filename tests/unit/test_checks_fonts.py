"""R20 / R41 字体嵌入校验。

这一项最容易出假阳性，也最容易漏报，两头都有坑：

- `/BaseFont` 是 Name 对象，`str()` 带前导斜杠。忘了剥，标准 14 字体
  全部匹配不上，干净文档也会被判"字体未嵌入"（**曾经真的踩过**）。
- `/Resources` 常写在 `/Pages` 节点上，页面自己没有。只看页对象会得出
  "全都没字体"的假结论。
- Type0 的 `/FontDescriptor` 挂在后代字体上，不在 `/Font` 本身。
- Type3 没有字体文件可嵌。

v4.3（R41）改了两条口径，这个文件跟着变了：
- 标准 14 字体**不再豁免**，未嵌入一样 `fail`（D-19）；
- 「已嵌入」不再只看 `/FontFile*` 键在不在，还要**校验字体程序结构**
  （长度 ≥ 64 字节 + 合法魔数）。所以本文件里所有"合规字体"都改用
  `embedded_helvetica()`（真 CFF 程序），不能用裸 `HELVETICA`。
"""

from __future__ import annotations

import pytest
from pikepdf import Array, Dictionary, Name, Stream

from app.pdf.checks import fonts
from tests.fixtures.make_fixtures import (
    add_page,
    embedded_helvetica,
    new_pdf,
    save,
    unembedded_font,
)


def _sfnt_program(size: int = 96) -> bytes:
    """结构合法的最小 sfnt：魔数 + `numTables=1` + 一条表目录项 + 填充。

    v4.3 起 `_sfnt_is_valid()` 会核对表目录是否装得下，随便几个字节的
    假流已经不算「嵌入」了。
    """
    header = b"\x00\x01\x00\x00" + (1).to_bytes(2, "big") + b"\x00\x00" * 3
    table = b"\x00" * 16
    body = header + table
    return body + b"\x00" * max(0, size - len(body))


def _font_descriptor(pdf, *, embedded: bool) -> Dictionary:
    fields = dict(
        Type=Name("/FontDescriptor"),
        FontName=Name("/ArialMT"),
        Flags=4,
    )
    if embedded:
        fields["FontFile2"] = Stream(pdf, _sfnt_program())
    return Dictionary(**fields)


def _type0_font(pdf, *, embedded: bool) -> Dictionary:
    """Type0 复合字体：描述符在后代字体上。"""
    descendant = Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/CIDFontType2"),
        BaseFont=Name("/ArialMT"),
        FontDescriptor=_font_descriptor(pdf, embedded=embedded),
    )
    return Dictionary(
        Type=Name("/Font"),
        Subtype=Name("/Type0"),
        BaseFont=Name("/ArialMT"),
        DescendantFonts=Array([descendant]),
    )


def test_clean_document_passes(check_ctx):
    result = fonts.run(check_ctx("check_clean.pdf"))
    assert result.status == "pass"
    assert result.summary == "全部字体均已嵌入"
    assert result.findings[0].message == "未发现未嵌入的字体"


def test_missing_font_fails(check_ctx):
    """AC-R41-N1：非标准 14 字体未嵌入 → fail（v4.3 前是 warning）。"""
    result = fonts.run(check_ctx("check_font_missing.pdf"))
    assert result.status == "fail"
    assert result.summary == "1 种字体未嵌入或嵌入无效"
    finding = result.findings[0]
    assert finding.message == "字体「Arial」未嵌入（第 1、2、3、4、5、6 页）"
    assert finding.level == "fail"


def test_missing_font_finding_points_at_the_first_page(check_ctx):
    result = fonts.run(check_ctx("check_font_missing.pdf"))
    assert result.findings[0].page == 1


def test_standard_14_unembedded_is_reported(check_ctx):
    """AC-R41-N2：裸 Helvetica 未嵌入必须报出来——这正是用户真实文件的形态。

    v4.3 前这里断言的是「Helvetica 不该出现」，那是错的：阅读器自带字形
    不等于文件自带字形（D-19）。
    """
    result = fonts.run(check_ctx("check_font_standard_unembedded.pdf"))
    assert result.status == "fail"
    blob = " ".join(item.message for item in result.findings)
    assert "字体「Helvetica」未嵌入" in blob
    assert "标准 14 字体同样要求嵌入" in result.findings[0].detail


def test_placeholder_font_program_is_rejected(check_ctx):
    """AC-R41-N3：有 `/FontFile2` 键但只有 26 字节 → 「嵌入程序无效」。

    这一条钉住「键在就算嵌入」的老缺陷：只看键的话，导出工具写的占位流
    会让整个校验项形同虚设。
    """
    result = fonts.run(check_ctx("check_font_placeholder.pdf"))
    assert result.status == "fail"
    assert result.summary == "1 种字体未嵌入或嵌入无效"
    finding = result.findings[0]
    assert finding.message == "字体「ArialMT」的嵌入程序无效（第 1、2、3、4、5、6 页）"
    assert "短于最小长度 64 字节" in finding.detail


def test_placeholder_and_missing_are_told_apart(check_ctx):
    """两种成因要分开说：前者换导出工具，后者重新导出。"""
    placeholder = fonts.run(check_ctx("check_font_placeholder.pdf"))
    missing = fonts.run(check_ctx("check_font_standard_unembedded.pdf"))
    assert "嵌入程序无效" in placeholder.findings[0].message
    assert "未嵌入" in missing.findings[0].message


def test_broken_font_dictionary_fails(check_ctx):
    """AC-R20-E1：`/Font` 不是字典 → fail，且不拖垮其它校验项。"""
    result = fonts.run(check_ctx("check_font_broken.pdf"))
    assert result.status == "fail"
    assert result.summary == "字体资源读取失败"
    assert "/Font 不是字典" in result.findings[0].detail


def test_font_name_drops_the_leading_slash():
    """回归：`/BaseFont` 是 Name 对象，`str()` 出来带斜杠。"""
    assert fonts._font_name(Name("/Helvetica")) == "Helvetica"
    assert fonts._font_name("/Arial") == "Arial"
    assert fonts._font_name(None) == ""


def test_standard_font_is_recognised_after_slash_removal():
    """回归：剥掉斜杠后 Helvetica 必须命中标准 14。"""
    assert fonts._is_standard(fonts._font_name(Name("/Helvetica")))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABCDEF+Arial", "Arial"),
        ("Arial", "Arial"),
        ("ABCDEF", "ABCDEF"),  # 长度 6，第 7 位不存在，不是子集前缀
        # 子集前缀规范固定 6 位大写字母 + `+`。7 位字母的不是合法前缀，
        # 不能瞎剥——否则 `ABCDEFG+Arial` 会被截成 `Arial`，跟真子集混淆。
        ("ABCDEFG+Arial", "ABCDEFG+Arial"),
        ("+Arial", "+Arial"),  # 长度 6，不满足 > 7
        ("ABCDE+Arial", "ABCDE+Arial"),  # 前缀只有 5 位，不符合规范
    ],
)
def test_subset_prefix_is_stripped(raw, expected):
    assert fonts._strip_subset(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Helvetica", True),
        ("Helvetica-Bold", True),
        ("TimesNewRomanPSMT", True),
        ("Courier", True),
        ("Symbol", True),
        ("ZapfDingbats", True),
        ("Arial", False),
        ("ArialMT", False),
        ("SimSun", False),
        ("", False),
    ],
)
def test_standard_14_matching(name, expected):
    assert fonts._is_standard(name) is expected


def test_type3_font_needs_no_embedding(tmp_path, ctx_factory):
    """Type3 的字形是页面里的过程，没有字体文件可嵌。"""
    pdf = new_pdf()
    page = add_page(pdf)
    page.Resources = Dictionary(
        Font=Dictionary(
            F1=embedded_helvetica(pdf),
            F3=Dictionary(Type=Name("/Font"), Subtype=Name("/Type3"), BaseFont=Name("/F3")),
        )
    )
    path = save(pdf, tmp_path / "type3.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.status == "pass"


def test_type0_descendant_fontfile_counts_as_embedded(tmp_path, ctx_factory):
    """回归：中文文档几乎都是 Type0，描述符在后代字体上。漏了会全部误判。"""
    pdf = new_pdf()
    page = add_page(pdf)
    page.Resources = Dictionary(
        Font=Dictionary(F1=embedded_helvetica(pdf), F2=_type0_font(pdf, embedded=True))
    )
    path = save(pdf, tmp_path / "type0_ok.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.status == "pass"


def test_type0_without_descendant_fontfile_fails(tmp_path, ctx_factory):
    pdf = new_pdf()
    page = add_page(pdf)
    page.Resources = Dictionary(
        Font=Dictionary(F1=embedded_helvetica(pdf), F2=_type0_font(pdf, embedded=False))
    )
    path = save(pdf, tmp_path / "type0_missing.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "1 种字体未嵌入或嵌入无效"


def test_resources_inherited_from_pages_node_are_scanned(tmp_path, ctx_factory):
    """`/Resources` 只写在 `/Pages` 上时也要能扫到，否则会误判成"没有字体"。

    注意 `add_blank_page` 会给页面塞一个**空** `/Resources`；按 PDF 规范页面
    自己的资源会覆盖继承的，所以这里必须显式删掉，才是在测继承路径。
    """
    pdf = new_pdf()
    pdf.add_blank_page(page_size=(595, 842))
    page = pdf.pages[0].obj
    del page[Name("/Resources")]
    pdf.Root.Pages.Resources = Dictionary(Font=Dictionary(F1=unembedded_font("/Arial")))
    path = save(pdf, tmp_path / "inherited.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.status == "fail"
    assert result.summary == "1 种字体未嵌入或嵌入无效"


def test_empty_page_resources_override_the_inherited_ones(tmp_path, ctx_factory):
    """页面留了个空 `/Resources`：按规范它覆盖父节点的，本项不该去猜。"""
    pdf = new_pdf()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.Root.Pages.Resources = Dictionary(Font=Dictionary(F1=unembedded_font("/Arial")))
    path = save(pdf, tmp_path / "overridden.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.status == "pass"


def test_subset_names_are_merged_into_one_font(tmp_path, ctx_factory):
    """子集前缀要去掉，否则同一字体在报告里出现好几次，每次名字都不一样。"""
    pdf = new_pdf()
    first = add_page(pdf)
    second = add_page(pdf)
    first.Resources = Dictionary(
        Font=Dictionary(F1=embedded_helvetica(pdf), F2=unembedded_font("/ABCDEF+Arial"))
    )
    second.Resources = Dictionary(
        Font=Dictionary(F1=embedded_helvetica(pdf), F2=unembedded_font("/GHIJKL+Arial"))
    )
    path = save(pdf, tmp_path / "subsets.pdf")

    result = fonts.run(ctx_factory(path))
    assert result.summary == "1 种字体未嵌入或嵌入无效"
    assert result.findings[0].message == "字体「Arial」未嵌入（第 1、2 页）"


def test_long_page_list_is_truncated(tmp_path, ctx_factory):
    """AC-R20-N2：明细里每种字体最多列 10 个页号。"""
    pdf = new_pdf()
    for _ in range(12):
        page = add_page(pdf)
        page.Resources = Dictionary(
            Font=Dictionary(F1=embedded_helvetica(pdf), F2=unembedded_font())
        )
    path = save(pdf, tmp_path / "many_pages.pdf")

    result = fonts.run(ctx_factory(path))
    finding = result.findings[0]
    # 页号串在 message 里（用户一眼要看的是"哪些页"），截断说明在 detail 里
    assert "第 1、2、3、4、5、6、7、8、9、10 页" in finding.message
    assert "、11" not in finding.message
    assert "共 12 页，仅列前 10 页" in finding.detail


def test_max_pages_listed_is_ten():
    assert fonts.MAX_PAGES_LISTED == 10


def test_standard_14_covers_the_documented_families():
    assert set(fonts.STANDARD_14) == {"Helvetica", "Times", "Courier", "Symbol", "ZapfDingbats"}


def test_fontfile_keys_cover_all_three_variants():
    assert set(fonts.FONT_FILE_KEYS) == {"/FontFile", "/FontFile2", "/FontFile3"}


def test_min_font_program_bytes_is_64():
    """AC-R41-N4：下限钉死，防止有人为了迁就某个样本把它调到 20。"""
    assert fonts.MIN_FONT_PROGRAM_BYTES == 64


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x00\x01\x00\x00" + (1).to_bytes(2, "big") + b"\x00" * 22, True),  # 表目录装得下
        (b"true" + (2).to_bytes(2, "big") + b"\x00" * 40, True),
        (b"OTTO" + (1).to_bytes(2, "big") + b"\x00" * 22, True),
        (b"ttcf" + b"\x00" * 20, True),  # 字体集合，不深究表目录
        (b"\x00\x01\x00\x00" + (0).to_bytes(2, "big") + b"\x00" * 22, False),  # numTables=0
        (b"\x00\x01\x00\x00" + (1).to_bytes(2, "big") + b"\x00" * 5, False),  # 装不下表目录
        (b"PROGRAMPROGRAM", False),  # 魔数不对
        (b"\x00\x01", False),  # 太短，连头都读不出来
    ],
)
def test_sfnt_validity(data, expected):
    assert fonts._sfnt_is_valid(data) is expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x01\x00\x04\x02\x00\x01\x01\x01", True),  # CFF1，hdrSize=4
        (b"\x02\x00\x05\x02\x00\x01\x01\x01", True),  # CFF2，hdrSize=5
        (b"\x03\x00\x04\x02\x00\x01\x01\x01", False),  # major 不认识
        (b"\x01\x00\x40\x02\x00\x01\x01\x01", False),  # hdrSize=64 越界
        (b"\x01\x00", False),  # 太短
    ],
)
def test_cff_validity(data, expected):
    assert fonts._cff_is_valid(data) is expected


def test_type1_pfb_header_is_accepted_but_pfa_also():
    """`/FontFile` 装 Type1：PFB 以 `\\x80\\x01` 开头，PFA 是 ASCII 的 `%!`。"""
    assert fonts._program_is_valid("/FontFile", b"\x80\x01" + b"\x00" * 100) is True
    assert fonts._program_is_valid("/FontFile", b"%!PS-AdobeFont" + b"\x00" * 100) is True
    assert fonts._program_is_valid("/FontFile", b"\x00" * 100) is False


def test_fontfile3_accepts_both_cff_and_opentype():
    """`/FontFile3` 的 `/Subtype` 既可能是 `Type1C`（裸 CFF）也可能是 `OpenType`。"""
    cff = b"\x01\x00\x04\x02" + b"\x00" * 100
    otf = b"OTTO" + (1).to_bytes(2, "big") + b"\x00" * 100
    assert fonts._program_is_valid("/FontFile3", cff) is True
    assert fonts._program_is_valid("/FontFile3", otf) is True
    assert fonts._program_is_valid("/FontFile3", b"garbage" * 20) is False


def test_program_shorter_than_the_floor_is_rejected_even_with_right_magic():
    """长度下限先于魔数判定：26 字节的真 sfnt 头也不行。"""
    short = b"\x00\x01\x00\x00" + (1).to_bytes(2, "big") + b"\x00" * 22
    assert len(short) < fonts.MIN_FONT_PROGRAM_BYTES
    assert fonts._program_is_valid("/FontFile2", short) is False
