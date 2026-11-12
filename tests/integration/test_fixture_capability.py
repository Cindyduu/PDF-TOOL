"""阶段 B：样本库能力验证。

验证每个样本都真的具备它声称要验证的结构。如果样本本身构造错了，
后面所有针对它的测试都会失去意义，所以这里必须断言到对象级。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pikepdf
import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fixtures.make_fixtures import SAMPLE_SPECS, ensure_fixtures  # noqa: E402

FIXTURE_DIR = ensure_fixtures()


def open_pdf(name: str) -> pikepdf.Pdf:
    return pikepdf.open(FIXTURE_DIR / name)


def outline_nodes(pdf: pikepdf.Pdf) -> list:
    """按 First -> Next 链走一遍顶层书签。

    注意：直接对 Dictionary 做 for 循环拿到的是键名字符串，不是书签节点。
    """
    node = pdf.Root.Outlines.get("/First")
    nodes = []
    while node is not None:
        nodes.append(node)
        node = node.get("/Next")
    return nodes


@pytest.mark.parametrize("name", sorted(SAMPLE_SPECS))
def test_fixture_file_exists_and_nonempty(name: str) -> None:
    path = FIXTURE_DIR / name
    assert path.is_file(), f"缺少样本 {name}"
    assert path.stat().st_size > 0, f"样本 {name} 为空"


@pytest.mark.parametrize(
    "name",
    [n for n in sorted(SAMPLE_SPECS) if n not in {"encrypted.pdf", "corrupt.pdf"}],
)
def test_fixture_opens_with_pymupdf(name: str) -> None:
    doc = pymupdf.open(FIXTURE_DIR / name)
    try:
        assert doc.page_count >= 1
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 书签 / 链接
# --------------------------------------------------------------------------- #


def test_zoom_explicit_has_non_inherit_zoom() -> None:
    pdf = open_pdf("zoom_explicit.pdf")
    with pdf:
        zooms = [float(item.Dest[4]) for item in outline_nodes(pdf)]
        assert zooms == [2.0, 1.5, 0.75]
        links = pdf.pages[0].Annots
        link_zooms = [float(a.Dest[4]) for a in links if a.Subtype == "/Link"]
        assert link_zooms == [3.0, 1.25]


def test_zoom_inherit_is_already_compliant() -> None:
    pdf = open_pdf("zoom_inherit.pdf")
    with pdf:
        for item in outline_nodes(pdf):
            assert item.Dest[4] is None, "承前缩放样本的 zoom 必须是 null"
        link = pdf.pages[0].Annots[0]
        assert link.Dest[4] is None


def test_named_dest_shared_uses_name_tree() -> None:
    pdf = open_pdf("named_dest_shared.pdf")
    with pdf:
        names = pdf.Root.Names.Dests.Names
        keys = [str(names[i]) for i in range(0, len(names), 2)]
        assert "sharedTarget" in keys
        assert len(outline_nodes(pdf)) == 3


def test_named_dest_shared_is_really_shared() -> None:
    """样本必须让书签与豁免页链接指向**同一个**间接目标对象。

    若两者各持一份副本，"共享目标不得跨范围误改"这条验收就无从谈起。
    """
    pdf = open_pdf("named_dest_shared.pdf")
    with pdf:
        names = pdf.Root.Names.Dests.Names
        targets = {
            str(names[i]): names[i + 1] for i in range(0, len(names), 2)
        }
        shared_target = targets["sharedTarget"]
        assert shared_target.is_indirect, "共享目标必须是间接对象"
        assert [float(v) for v in shared_target[2:]] == [0.0, 800.0, 2.5]

        named = [n for n in outline_nodes(pdf) if str(n.Title) == "Named shared"]
        assert len(named) == 1
        assert str(named[0].Dest) == "sharedTarget", "书签应通过名字引用目标"

        links = [a for a in pdf.pages[3].Annots if a.Subtype == "/Link"]
        assert len(links) == 1
        assert str(links[0].Dest) == "sharedTarget", "豁免页链接应复用同一个命名目标"


def test_links_mixed_has_three_kinds() -> None:
    pdf = open_pdf("links_mixed.pdf")
    with pdf:
        annots = [a for a in pdf.pages[0].Annots if a.Subtype == "/Link"]
        assert len(annots) == 3
        kinds = set()
        for annot in annots:
            if "/A" in annot:
                kinds.add(str(annot.A.S))
            elif "/Dest" in annot:
                kinds.add("Dest")
        assert kinds == {"/URI", "Dest"}


# --------------------------------------------------------------------------- #
# 注释
# --------------------------------------------------------------------------- #


def test_annots_mixed_contains_expected_subtypes() -> None:
    pdf = open_pdf("annots_mixed.pdf")
    with pdf:
        subtypes = {str(a.Subtype) for a in pdf.pages[0].Annots}
        assert {"/Highlight", "/FreeText", "/Stamp", "/Link", "/Popup", "/Widget"} <= subtypes
        assert "/Square" in subtypes
        # 无外观注释
        squares = [a for a in pdf.pages[0].Annots if a.Subtype == "/Square"]
        assert any("/AP" not in a for a in squares), "缺少无外观注释样本"
        # 隐藏注释
        assert any(int(a.get("/F", 0)) & 2 for a in squares), "缺少隐藏注释样本"
        # 旋转页
        assert int(pdf.pages[1].obj.Rotate) == 90
        assert "/AcroForm" in pdf.Root


def test_exempt_mix_layout() -> None:
    pdf = open_pdf("exempt_mix.pdf")
    with pdf:
        assert len(pdf.pages) == 5
        titles = [str(item.Title) for item in outline_nodes(pdf)]
        assert titles == ["Ch1", "Ch2", "Ref 2.7.5", "Ref 3.3"]
        # 第 2 页（索引 1）与第 4 页（索引 3）有注释
        assert len(pdf.pages[1].Annots) == 2
        assert len(pdf.pages[3].Annots) == 2
        assert "/Annots" not in pdf.pages[0]


def test_smoke_links_font_declares_widths() -> None:
    """冒烟样本的字体必须带 `/Widths`。

    标准 14 字体省略 `/Widths` 是合法的，但省略之后「一行里第 2 个 `Tj` 画在
    哪儿」就推算不出来，`content_color` 只能放弃着色并记一条警告。R35 改的是
    文字填充色——样本里推算不出位置，冒烟那条「文字变蓝」的断言就是空断言。
    """
    pdf = open_pdf("smoke_links.pdf")
    with pdf:
        font = pdf.pages[1].Resources.Font.F1
        assert "/Widths" in font, "冒烟样本的 Helvetica 缺少 /Widths"
        assert int(font.FirstChar) == 32
        assert len(font.Widths) == int(font.LastChar) - int(font.FirstChar) + 1


def test_smoke_links_rects_cover_text() -> None:
    """每个内链的矩形都必须真的压在文字上，否则没有文字可改色。

    `exempt_mix.pdf` 就是反例：文字基线 760、链接矩形 600–620，两者不相交，
    哪怕字体带 `/Widths` 也一个字符都改不到。
    """
    doc = pymupdf.open(FIXTURE_DIR / "smoke_links.pdf")
    try:
        links = [link for page in doc for link in page.get_links()]
        assert len(links) == 2, "冒烟样本应有 2 条内链"
        for link in links:
            page = doc[link["page"]]
            rect = pymupdf.Rect(link["from"])
            covered = [
                span
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
                if pymupdf.Rect(span["bbox"]).intersects(rect)
            ]
            assert covered, f"第 {link['page'] + 1} 页的链接矩形没压到任何文字"
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #


def test_meta_full_has_docinfo_and_xmp() -> None:
    pdf = open_pdf("meta_full.pdf")
    with pdf:
        assert "/Info" in pdf.trailer
        assert str(pdf.docinfo.get("/Title", "")) == "Quarterly Report"
        assert "/Metadata" in pdf.Root
        xmp = bytes(pdf.Root.Metadata.read_bytes())
        assert b"dc:title" in xmp


def test_meta_clean_is_empty() -> None:
    pdf = open_pdf("meta_clean.pdf")
    with pdf:
        info = pdf.trailer.get("/Info")
        if info is not None:
            meaningful = {
                k: v
                for k, v in info.items()
                if str(k) not in {"/Producer", "/Creator"} and str(v).strip()
            }
            assert not meaningful


# --------------------------------------------------------------------------- #
# 标签
# --------------------------------------------------------------------------- #


def _mcid_count(pdf: pikepdf.Pdf, page) -> int:
    contents = page.Contents
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    data = b"".join(stream.read_bytes() for stream in streams)
    return data.count(b"/MCID")


def test_tagged_real_has_true_structure() -> None:
    pdf = open_pdf("tagged_real.pdf")
    with pdf:
        assert "/StructTreeRoot" in pdf.Root
        root = pdf.Root.StructTreeRoot
        assert "/ParentTree" in root
        assert root.ParentTree.Nums[0] == 0
        assert int(pdf.pages[0].obj.StructParents) == 0
        assert _mcid_count(pdf, pdf.pages[0].obj) == 2
        assert bool(pdf.Root.MarkInfo.Marked) is True
        doc_elem = root.K[0]
        assert str(doc_elem.S) == "/Document"
        assert len(doc_elem.K) == 2


def test_markinfo_only_has_no_structure_tree() -> None:
    pdf = open_pdf("markinfo_only.pdf")
    with pdf:
        assert "/MarkInfo" in pdf.Root
        assert bool(pdf.Root.MarkInfo.Marked) is True
        assert "/StructTreeRoot" not in pdf.Root


def test_untagged_has_neither() -> None:
    pdf = open_pdf("untagged.pdf")
    with pdf:
        assert "/StructTreeRoot" not in pdf.Root
        assert "/MarkInfo" not in pdf.Root


def test_broken_struct_is_inconsistent() -> None:
    pdf = open_pdf("broken_struct.pdf")
    with pdf:
        assert "/StructTreeRoot" in pdf.Root
        nums = pdf.Root.StructTreeRoot.ParentTree.Nums
        assert len(nums) % 2 == 1, "ParentTree.Nums 应为奇数长度（键值不成对）"


def test_text_blocks_has_extractable_text() -> None:
    doc = pymupdf.open(FIXTURE_DIR / "text_blocks.pdf")
    try:
        assert doc.page_count == 2
        text = doc[0].get_text()
        assert "Study Report" in text
        assert "Introduction" in text
        # 必须有多个文本块可供推断结构
        blocks = [b for b in doc[0].get_text("dict")["blocks"] if b.get("type") == 0]
        assert len(blocks) >= 5
    finally:
        doc.close()


def test_scanned_like_has_no_text() -> None:
    doc = pymupdf.open(FIXTURE_DIR / "scanned_like.pdf")
    try:
        assert doc[0].get_text().strip() == ""
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 异常样本
# --------------------------------------------------------------------------- #


def test_encrypted_requires_password() -> None:
    doc = pymupdf.open(FIXTURE_DIR / "encrypted.pdf")
    try:
        # pymupdf 这里返回 int，用真值判断而不是 is True
        assert int(doc.needs_pass) == 1
        assert doc.page_count >= 0  # 允许打开但不解密
    finally:
        doc.close()
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(FIXTURE_DIR / "encrypted.pdf")


def test_signed_has_signature_field() -> None:
    pdf = open_pdf("signed.pdf")
    with pdf:
        assert "/AcroForm" in pdf.Root
        fields = pdf.Root.AcroForm.Fields
        assert len(fields) == 1
        field = fields[0]
        assert str(field.FT) == "/Sig"
        assert "/V" in field
        assert str(field.V.Type) == "/Sig"


def test_corrupt_cannot_open() -> None:
    assert (FIXTURE_DIR / "corrupt.pdf").read_bytes().startswith(b"%PDF-")
    with pytest.raises(Exception):
        doc = pymupdf.open(FIXTURE_DIR / "corrupt.pdf")
        if doc.page_count == 0:
            raise ValueError("no pages")
        doc.close()


# --------------------------------------------------------------------------- #
# 初始视图
# --------------------------------------------------------------------------- #


def test_initial_view_samples() -> None:
    pdf = open_pdf("annots_mixed.pdf")
    with pdf:
        assert str(pdf.Root.PageLayout) == "/TwoColumnLeft"
        assert str(pdf.Root.PageMode) == "/UseNone"
        assert isinstance(pdf.Root.OpenAction, pikepdf.Array)


def test_linearized_sample() -> None:
    raw = (FIXTURE_DIR / "linearized.pdf").read_bytes()
    head = raw[:1024]
    assert b"/Linearized" in head, "样本未真正线性化"
