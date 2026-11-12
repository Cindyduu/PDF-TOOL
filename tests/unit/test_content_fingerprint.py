"""R36 内容一致性护栏（AC-R36-N1..N5、E1、E2）。

这一组测试要证明的是「护栏真的能抓住内容被改」，而不只是「正常处理不报错」。
所以每个用例都**故意把内容改坏**，再看护栏有没有反应：

- 少一页、改页面尺寸、换图、换字体 —— 硬门禁，必须判违规；
- 改文字、改内容流 —— 软门禁，只有在**没有对应处理项**被勾选时才判违规。

反向用例同样重要：勾了 `link_color` 时内容流本来就会变，护栏要是这时候报警，
正常处理就永远走不完。
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, Stream

from app.pdf import content_fingerprint as fp
from tests.fixtures.make_fixtures import (
    HELVETICA,
    add_page,
    new_pdf,
    save,
)

BASE_TEXT = b"""BT /F1 16 Tf 72 770 Td (Clinical Study Report) Tj ET
BT /F1 11 Tf 72 740 Td (Ordinary horizontal text line.) Tj ET
"""


# --------------------------------------------------------------------------- #
# 构造样本
# --------------------------------------------------------------------------- #


def _base(path: Path, pages: int = 3) -> Path:
    pdf = new_pdf()
    for _ in range(pages):
        add_page(pdf, BASE_TEXT)
    return save(pdf, path)


def _rewrite(src: Path, dst: Path, mutate) -> Path:
    """打开 `src`、交给 `mutate(pdf)` 改、另存为 `dst`。"""
    with pikepdf.open(src) as pdf:
        mutate(pdf)
        pdf.save(dst)
    return dst


def _fingerprints(src: Path, dst: Path):
    return fp.fingerprint_file(src), fp.fingerprint_file(dst)


# --------------------------------------------------------------------------- #
# 指纹本身
# --------------------------------------------------------------------------- #


def test_same_file_has_identical_fingerprint(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")
    before = fp.fingerprint_file(src)
    after = fp.fingerprint_file(src)

    assert before.error is None
    assert before == after
    assert before.page_count == 3
    assert len(before.pages) == 3
    assert before.pages[0].width == 595
    assert before.pages[0].height == 842


def test_fingerprint_reports_page_geometry_and_hashes(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")
    page = fp.fingerprint_file(src).pages[0]

    assert page.number == 1
    assert page.rotate == 0
    assert len(page.text_hash) == 64
    assert len(page.content_hash) == 64
    assert len(page.image_digest) == 64
    assert len(page.font_digest) == 64


def test_fingerprint_of_missing_file_is_an_error_not_a_crash(tmp_path: Path):
    result = fp.fingerprint_file(tmp_path / "nope.pdf")

    assert result.error
    assert result.page_count == 0
    assert result.pages == ()


def test_as_dict_shape_is_stable(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")
    payload = fp.fingerprint_file(src).as_dict()

    assert payload["page_count"] == 3
    assert payload["error"] is None
    assert set(payload["pages"][0]) == {
        "number",
        "width",
        "height",
        "rotate",
        "text_hash",
        "content_hash",
        "image_digest",
        "font_digest",
    }


# --------------------------------------------------------------------------- #
# 硬门禁
# --------------------------------------------------------------------------- #


def test_page_count_change_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        add_page(pdf, BASE_TEXT)

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["metadata"])

    assert report.ok is False
    assert any("页数变化" in problem for problem in report.problems)
    assert "3" in report.problems[0] and "4" in report.problems[0]


def test_page_size_change_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        pdf.pages[0].obj.MediaBox = Array([0, 0, 595, 900])

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, [])

    assert report.ok is False
    assert any("第 1 页尺寸变化" in problem for problem in report.problems)


def test_rotation_change_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        pdf.pages[1].obj.Rotate = 90

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, [])

    assert report.ok is False
    assert any("第 2 页旋转角变化" in problem for problem in report.problems)


def _image_xobject(pdf: pikepdf.Pdf) -> Stream:
    return Stream(
        pdf,
        b"\x00\x11\x22\x33" * 4,
        Type=Name("/XObject"),
        Subtype=Name("/Image"),
        Width=2,
        Height=2,
        ColorSpace=Name("/DeviceRGB"),
        BitsPerComponent=8,
    )


def test_swapping_an_image_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        page.Resources.XObject = Dictionary(Im1=_image_xobject(pdf))

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, [])

    assert report.ok is False
    assert any("图片资源变化" in problem for problem in report.problems)


def test_adding_a_font_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        page.Resources.Font.F2 = HELVETICA

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, [])

    assert report.ok is False
    assert any("字体资源变化" in problem for problem in report.problems)


def test_flatten_style_xobject_does_not_trip_the_image_gate(tmp_path: Path):
    """扁平化会往页面资源里加 `/FlatAnnot1` 这类表单 XObject。

    它既不是图片，名字也不该参与图片摘要——否则每次扁平化都会被判违规。
    """
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        form = Stream(
            pdf,
            b"1 1 0 rg 0 0 10 10 re f",
            Type=Name("/XObject"),
            Subtype=Name("/Form"),
            BBox=Array([0, 0, 10, 10]),
        )
        page.Resources.XObject = Dictionary(FlatAnnot1=form)

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)

    assert before.pages[0].image_digest == after.pages[0].image_digest
    assert before.pages[0].font_digest == after.pages[0].font_digest


# --------------------------------------------------------------------------- #
# 软门禁：内容流
# --------------------------------------------------------------------------- #


def _append_ops(pdf: pikepdf.Pdf, page_index: int, extra: bytes) -> None:
    page = pdf.pages[page_index].obj
    original = page.Contents.read_bytes()
    page.Contents = Stream(pdf, original + extra)


def test_content_change_without_matching_operation_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        _append_ops(pdf, 0, b"\nq 0 0 1 rg Q\n")

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["metadata", "fast_web_view"])

    assert report.ok is False
    assert report.changed_content_pages == (1,)
    assert any("没有任何已勾选的处理项" in problem for problem in report.problems)


def test_content_change_attributed_to_link_color(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        _append_ops(pdf, 0, b"\nq 0 0 1 rg Q\n")

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["link_color"])

    assert report.ok is True
    assert report.changed_content_pages == (1,)
    assert report.changed_text_pages == ()
    assert report.attributed_to == ("link_color",)
    assert "已归因到：link_color" in report.summary()


def test_content_change_attributed_to_flatten(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        _append_ops(pdf, 1, b"\n1 1 0 rg 0 0 10 10 re f\n")

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["flatten_annotations"])

    assert report.ok is True
    assert report.attributed_to == ("flatten_annotations",)


# --------------------------------------------------------------------------- #
# 软门禁：文本层
# --------------------------------------------------------------------------- #


def test_text_change_without_matching_operation_is_a_violation(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        page.Contents = Stream(pdf, b"BT /F1 16 Tf 72 770 Td (Tampered) Tj ET")

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["link_color"])

    assert report.ok is False
    assert report.changed_text_pages == (1,)
    assert any("文本层发生变化" in problem for problem in report.problems)


def test_text_change_attributed_to_flatten(tmp_path: Path):
    """扁平化把注释外观（含文字）画进内容流，文本层变化是可解释的。"""
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        page.Contents = Stream(
            pdf,
            BASE_TEXT + b"BT /F1 10 Tf 72 700 Td (Reviewer note) Tj ET",
        )

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["flatten_annotations"])

    assert report.ok is True
    assert report.changed_text_pages == (1,)
    assert "flatten_annotations" in report.attributed_to


# --------------------------------------------------------------------------- #
# 无变化与护栏自身故障
# --------------------------------------------------------------------------- #


def test_no_change_reports_clean(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")
    dst = _rewrite(src, tmp_path / "b.pdf", lambda pdf: None)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["metadata"])

    assert report.ok is True
    assert report.changed_content_pages == ()
    assert report.changed_text_pages == ()
    assert report.summary() == "内容与版式均未变化"


def test_metadata_only_change_keeps_content_intact(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        pdf.docinfo = pdf.make_indirect(
            Dictionary(Title=pikepdf.String("Quarterly Report"))
        )

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    report = fp.compare(before, after, ["metadata"])

    assert report.ok is True
    assert report.changed_content_pages == ()


def test_fingerprint_failure_does_not_block_processing():
    """护栏自己坏了不能成为新的故障点（AC-R36-E2）。"""
    broken = fp.DocumentFingerprint(page_count=0, pages=(), error="boom")
    good = fp.DocumentFingerprint(page_count=0, pages=())

    report = fp.compare(broken, good, ["metadata"])

    assert report.ok is True
    assert report.warning is not None
    assert "内容一致性未能核对" in report.warning
    assert "boom" in report.warning
    assert report.summary() == report.warning


def test_report_as_dict_shape(tmp_path: Path):
    src = _base(tmp_path / "a.pdf")

    def mutate(pdf: pikepdf.Pdf) -> None:
        _append_ops(pdf, 0, b"\nq 0 0 1 rg Q\n")

    dst = _rewrite(src, tmp_path / "b.pdf", mutate)
    before, after = _fingerprints(src, dst)
    payload = fp.compare(before, after, ["link_color"]).as_dict()

    assert payload["ok"] is True
    assert payload["page_count"] == {"before": 3, "after": 3}
    assert payload["changed_pages"] == {"content": [1], "text": []}
    assert payload["attributed_to"] == ["link_color"]
    assert payload["problems"] == []
    assert payload["warning"] is None


# --------------------------------------------------------------------------- #
# 归因表本身
# --------------------------------------------------------------------------- #


def test_attribution_tables_are_disjoint_from_readonly_operations():
    """只读或不改内容的处理项不该出现在归因表里。

    `metadata` / `bookmark_panel` / `initial_zoom` 这类项改的是文档级字典，
    它们让内容流变化只能说明护栏漏了——归因表里出现它们就是自欺欺人。
    """
    for key in ("metadata", "bookmark_panel", "initial_layout", "initial_zoom"):
        assert key not in fp.CONTENT_CHANGING_OPERATIONS
        assert key not in fp.TEXT_CHANGING_OPERATIONS


def test_link_color_changes_content_but_not_text():
    assert "link_color" in fp.CONTENT_CHANGING_OPERATIONS
    assert "link_color" not in fp.TEXT_CHANGING_OPERATIONS


#: 只动文本层、不动内容流的处理项。删注释只是把对象从 `/Annots` 里摘掉，
#: `/Contents` 一个字节都不写；文本层之所以登记，是因为个别解析器会把
#: `FreeText` 注释里的字算进提取结果。这是**合法例外**，不是表写错了。
TEXT_ONLY_OPERATIONS = frozenset({"remove_annotations"})


@pytest.mark.parametrize("key", sorted(fp.TEXT_CHANGING_OPERATIONS))
def test_every_text_changing_operation_is_explained(key: str):
    """会改文本的项必须能说清它改的是哪一层。

    要么写内容流（`flatten_annotations`、`link_color`），要么只摘注释对象
    （`remove_annotations`）。两边都不占，说明归因表漏登了这一项。
    """
    assert key in fp.CONTENT_CHANGING_OPERATIONS or key in TEXT_ONLY_OPERATIONS


def test_remove_annotations_touches_text_but_not_content_stream():
    """删注释不改内容流——这正是两张归因表必须分开的原因。"""
    assert "remove_annotations" in fp.TEXT_CHANGING_OPERATIONS
    assert "remove_annotations" not in fp.CONTENT_CHANGING_OPERATIONS
