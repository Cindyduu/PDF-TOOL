"""阶段 E 集成验收：八项非标签处理器串成流水线。

这里测的不是「处理器说自己做了什么」，而是**磁盘上那个文件**变成了什么样：

- 豁免对象逐对象字节不变；
- 共享命名目标不被跨范围误改；
- 链接仍然可点击；
- 旋转页展平前后像素几乎不变（说明是「画上去」而不是「删掉」）；
- 元数据在最终文件（含线性化之后）仍然为空；
- 线性化失败时其他成果全部保留；
- 每项 counts 六元组自洽，状态与计数一致。
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pymupdf
import pytest

from app.pdf import linearize
from app.pdf.outcome import decide_status
from app.pdf.pipeline import PipelineError, run_pipeline
from app.pdf.verify import verify_pdf_file
from app.schemas import COUNT_KEYS, OPERATION_KEYS, validate_operation_result
from tests.pdf_ops import annots_of, object_signature, page_content_bytes

BUCKETS = ("modified", "compliant", "exempt", "not_applicable", "failed")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def object_bytes(pdf: pikepdf.Pdf, obj) -> object:
    """一个对象的**内容**签名（不含对象编号，见 `tests.pdf_ops.object_signature`）。

    线性化会重排对象编号，所以「豁免对象零变化」必须比内容，不能比 `unparse()`。
    """
    return object_signature(pdf, obj)


def exempt_fingerprint(pdf: pikepdf.Pdf, index: int) -> dict:
    """豁免页及其注释的逐对象内容指纹。

    刻意不含 objgen：那是文件内的位置编号，会被线性化重排，
    与「对象有没有被改动」无关。注释数量由 `annots` 列表长度覆盖。
    """
    page = pdf.pages[index]
    annots = annots_of(page)
    return {
        "page": object_bytes(pdf, page.obj),
        "annots": [object_bytes(pdf, annot) for annot in annots],
        "contents": page_content_bytes(page),
    }


def named_dest(pdf: pikepdf.Pdf, name: str):
    pairs = pdf.Root.Names.Dests.Names
    for index in range(0, len(pairs) - 1, 2):
        if str(pairs[index]) == name:
            return pairs[index + 1]
    raise AssertionError(f"找不到命名目标 {name}")


def render_samples(path, index: int, zoom: float = 2.0) -> bytes:
    with pymupdf.open(str(path)) as doc:
        pixmap = doc[index].get_pixmap(
            matrix=pymupdf.Matrix(zoom, zoom), alpha=False
        )
        return pixmap.samples


def byte_diff(left: bytes, right: bytes) -> int:
    """逐字节差异计数。numpy 不可用，纯 Python 也要能算。"""
    if len(left) != len(right):
        raise AssertionError(f"渲染尺寸不一致：{len(left)} vs {len(right)}")
    if left == right:
        return 0
    return sum(1 for a, b in zip(left, right) if a != b)


def non_white(left: bytes) -> int:
    return sum(1 for value in left if value != 255)


def link_count(path, index: int) -> int:
    with pymupdf.open(str(path)) as doc:
        return len(doc[index].get_links())


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #


def test_all_nine_results_present_in_order(fixture_pdfs, tmp_path):
    result = run_pipeline(fixture_pdfs / "annots_mixed.pdf", tmp_path / "out.pdf")

    assert [item["key"] for item in result.results] == list(OPERATION_KEYS)
    for item in result.results:
        validate_operation_result(item)
        assert item["started_at"] and item["finished_at"]
    assert result.page_count == 2
    assert result.output.is_file()


def test_counts_are_self_consistent(fixture_pdfs, tmp_path):
    """六元组必须自洽：五个归宿加起来等于检查数，状态由计数推出。"""
    result = run_pipeline(fixture_pdfs / "annots_mixed.pdf", tmp_path / "out.pdf")

    for item in result.results:
        counts = item["counts"]
        assert tuple(counts) == COUNT_KEYS
        assert sum(counts[bucket] for bucket in BUCKETS) == counts["checked"], (
            f"{item['key']} 的计数不自洽：{counts}"
        )
        assert item["status"] == decide_status(counts), (
            f"{item['key']} 的状态与计数不符：{item['status']} / {counts}"
        )


def test_overall_status_reflects_failures(fixture_pdfs, tmp_path):
    result = run_pipeline(fixture_pdfs / "annots_mixed.pdf", tmp_path / "out.pdf")

    assert result.failed_keys == ("flatten_annotations",)
    assert result.overall_status == "completed_with_warnings"
    assert any("注释" in warning for warning in result.warnings)


def test_clean_document_completes_without_warnings(fixture_pdfs, tmp_path):
    result = run_pipeline(fixture_pdfs / "zoom_explicit.pdf", tmp_path / "out.pdf")

    assert result.overall_status == "completed"
    assert result.failed_keys == ()
    assert "bookmarks_inherit_zoom" in result.changed_operations
    assert "links_inherit_zoom" in result.changed_operations
    assert "fast_web_view" in result.changed_operations


def test_stage_callback_walks_the_whole_pipeline(fixture_pdfs, tmp_path):
    stages: list[str] = []
    run_pipeline(
        fixture_pdfs / "untagged.pdf",
        tmp_path / "out.pdf",
        stage_callback=stages.append,
    )

    assert stages[0] == "preparing"
    assert stages[-1] == "publishing"
    assert "verifying" in stages
    assert stages.index("verifying") < stages.index("publishing")
    assert list(OPERATION_KEYS) == [s for s in stages if s in OPERATION_KEYS]


def test_tagged_pdf_without_choice_does_not_pretend(fixture_pdfs, tmp_path):
    """用户没选标签处理方式时，既不假装做过、也不擅自生成。

    阶段 F 之前这里是占位（`tag_not_available` + 全零 counts）。真实现接上之后，
    语义变得更严格：必须真的"检查过"，并明确报告"尚未选择处理方式"。
    """

    output = tmp_path / "out.pdf"
    result = run_pipeline(fixture_pdfs / "untagged.pdf", output)
    item = result.result_for("tagged_pdf")

    assert item["status"] == "not_applicable"
    assert item["reason_code"] == "tag_choice_missing"
    assert item["counts"] == {**dict.fromkeys(COUNT_KEYS, 0), "checked": 1, "not_applicable": 1}
    assert "选择「生成标签」" in item["next_action"]
    assert "tagged_pdf" not in result.changed_operations

    # 没选择就绝不能留下标签结构：既不能有空树，也不能有"假装加过标签"的 MarkInfo
    with pikepdf.open(output) as pdf:
        assert "/StructTreeRoot" not in pdf.Root
        assert "/MarkInfo" not in pdf.Root

    skipped = run_pipeline(
        fixture_pdfs / "untagged.pdf", tmp_path / "skip.pdf", tag_choice="skip"
    )
    assert skipped.result_for("tagged_pdf")["reason_code"] == "tag_skipped"


# --------------------------------------------------------------------------- #
# 豁免对象零变化
# --------------------------------------------------------------------------- #


def test_exempt_page_objects_are_byte_identical(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "exempt_mix.pdf"
    output = tmp_path / "out.pdf"

    with pikepdf.open(source) as pdf:
        before = exempt_fingerprint(pdf, 3)
    assert before["annots"], "样本第 4 页必须真有注释"

    result = run_pipeline(source, output, exempt_pages=(4,))

    with pikepdf.open(output) as pdf:
        after = exempt_fingerprint(pdf, 3)

    assert after == before, "豁免页的页面字典、注释、内容流必须逐字节不变"
    assert result.result_for("flatten_annotations")["counts"]["exempt"] == 2
    assert result.result_for("links_inherit_zoom")["counts"]["exempt"] == 1


def test_exempt_objects_change_when_not_exempt(fixture_pdfs, tmp_path):
    """对照组：不设豁免时同一页确实会被改，证明上面的「不变」有意义。"""
    source = fixture_pdfs / "exempt_mix.pdf"
    output = tmp_path / "out.pdf"

    with pikepdf.open(source) as pdf:
        before = exempt_fingerprint(pdf, 3)

    run_pipeline(source, output)

    with pikepdf.open(output) as pdf:
        after = exempt_fingerprint(pdf, 3)

    assert after != before


def test_exempt_bookmark_target_array_is_untouched(fixture_pdfs, tmp_path):
    """豁免按目标页判定：指向豁免页的书签，目的地数组一个字节都不许变。"""
    source = fixture_pdfs / "exempt_mix.pdf"
    output = tmp_path / "out.pdf"

    with pikepdf.open(source) as pdf:
        before = {}
        node = pdf.Root.Outlines.First
        while node is not None:
            before[str(node.Title)] = object_bytes(pdf, node.Dest)
            node = node.get("/Next")

    run_pipeline(source, output, exempt_pages=(4,))

    with pikepdf.open(output) as pdf:
        node = pdf.Root.Outlines.First
        after = {}
        while node is not None:
            after[str(node.Title)] = object_bytes(pdf, node.Dest)
            node = node.get("/Next")

    assert after["Ref 2.7.5"] == before["Ref 2.7.5"]
    assert after["Ch1"] != before["Ch1"]
    assert after["Ch2"] != before["Ch2"]


# --------------------------------------------------------------------------- #
# 共享命名目标
# --------------------------------------------------------------------------- #


def test_shared_named_dest_is_not_cross_modified(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "named_dest_shared.pdf"
    output = tmp_path / "out.pdf"

    with pikepdf.open(source) as pdf:
        before = object_bytes(pdf, named_dest(pdf, "sharedTarget"))

    result = run_pipeline(source, output, exempt_pages=(4,))

    with pikepdf.open(output) as pdf:
        after = object_bytes(pdf, named_dest(pdf, "sharedTarget"))
        assert after == before, "共享目标原件被就地改写，等于偷偷改了豁免对象"
        assert float(named_dest(pdf, "sharedTarget")[4]) == 2.5

        # 豁免页那条链接仍然解析到原目标
        annot = pdf.pages[3].obj.Annots[0]
        assert str(annot.Dest) == "sharedTarget"

    # 非豁免书签自己换成了复制品，缩放已承前
    with pikepdf.open(output) as pdf:
        node = pdf.Root.Outlines.First
        while str(node.Title) != "Named shared":
            node = node.get("/Next")
        assert node.Dest[4] is None

    assert result.result_for("bookmarks_inherit_zoom")["counts"]["modified"] == 2
    assert any(
        "共用同一个目标" in warning
        for warning in result.result_for("bookmarks_inherit_zoom")["warnings"]
    )


# --------------------------------------------------------------------------- #
# 链接仍可点击
# --------------------------------------------------------------------------- #


def test_links_remain_clickable(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "links_mixed.pdf"
    output = tmp_path / "out.pdf"
    before = link_count(source, 0)
    assert before == 3

    run_pipeline(source, output)

    assert link_count(output, 0) == before

    with pymupdf.open(str(output)) as doc:
        kinds = sorted(link["kind"] for link in doc[0].get_links())
    assert kinds == sorted([pymupdf.LINK_GOTO, pymupdf.LINK_URI, pymupdf.LINK_GOTO])


def test_links_survive_annotation_flattening(fixture_pdfs, tmp_path):
    """展平注释不能顺手把链接一起处理掉。"""
    source = fixture_pdfs / "annots_mixed.pdf"
    output = tmp_path / "out.pdf"
    before = link_count(source, 0)

    result = run_pipeline(source, output)

    assert link_count(output, 0) == before
    assert result.result_for("flatten_annotations")["counts"]["not_applicable"] == 1


# --------------------------------------------------------------------------- #
# 像素：展平必须是「画上去」而不是「删掉」
# --------------------------------------------------------------------------- #


def test_rotated_page_flatten_keeps_pixels(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "annots_mixed.pdf"
    output = tmp_path / "out.pdf"

    run_pipeline(source, output)

    before = render_samples(source, 1)
    after = render_samples(output, 1)
    diff = byte_diff(before, after)
    total = len(before)

    # 注释外观被画进内容流，渲染结果应当与原来一致；
    # 阈值留一点点余量给坐标取整，实测为 0。
    assert diff / total < 0.001, f"旋转页像素差异过大：{diff}/{total}（{diff / total:.4%}）"
    assert non_white(after) > 1000, "页面必须有实际内容，否则对比没有意义"


def test_flatten_reduces_annotations_not_content(fixture_pdfs, tmp_path):
    """对照组：注释数确实变少了，像素却没塌——说明是「画上去再摘掉」。"""
    source = fixture_pdfs / "annots_mixed.pdf"
    output = tmp_path / "out.pdf"

    with pikepdf.open(source) as pdf:
        before_annots = sum(len(annots_of(page)) for page in pdf.pages)
    run_pipeline(source, output)
    with pikepdf.open(output) as pdf:
        after_annots = sum(len(annots_of(page)) for page in pdf.pages)

    assert before_annots == 9
    assert after_annots == 3

    before = render_samples(source, 0)
    after = render_samples(output, 0)
    assert byte_diff(before, after) / len(before) < 0.001


def test_appearance_actually_drawn_when_present(fixture_pdfs, tmp_path):
    """外观真的进了内容流：把注释当普通内容渲染时也要看得见。"""
    source = fixture_pdfs / "annots_mixed.pdf"
    output = tmp_path / "out.pdf"
    run_pipeline(source, output)

    with pikepdf.open(output) as pdf:
        content = page_content_bytes(pdf.pages[0])
    assert b"/FlatAnnot1 Do" in content


# --------------------------------------------------------------------------- #
# 元数据在最终文件里仍然为空
# --------------------------------------------------------------------------- #


def test_metadata_stays_empty_after_linearize(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "meta_full.pdf"
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output)

    report = verify_pdf_file(output, expected_pages=1, require_no_metadata=True)
    assert report.ok, report.problems

    with pikepdf.open(output) as pdf:
        assert pdf.trailer.get("/Info") is None
        assert pdf.Root.get("/Metadata") is None
        assert pdf.is_linearized, "最终文件必须仍然是线性化的"

    assert result.result_for("metadata")["status"] == "adjusted"
    assert result.result_for("fast_web_view")["status"] == "adjusted"


def test_page_level_metadata_removed_end_to_end(tmp_path):
    from pikepdf import Stream

    from tests.fixtures.make_fixtures import add_page, new_pdf

    source = tmp_path / "src.pdf"
    pdf = new_pdf()
    page = add_page(pdf)
    page.Metadata = Stream(pdf, b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    pdf.docinfo = pdf.make_indirect(
        pikepdf.Dictionary(Title=pikepdf.String("Draft"))
    )
    pdf.save(source)
    pdf.close()

    output = tmp_path / "out.pdf"
    run_pipeline(source, output)

    with pikepdf.open(output) as reopened:
        assert reopened.pages[0].obj.get("/Metadata") is None
        assert reopened.trailer.get("/Info") is None


# --------------------------------------------------------------------------- #
# 线性化失败时的降级
# --------------------------------------------------------------------------- #


def test_linearize_failure_keeps_other_results(fixture_pdfs, tmp_path, monkeypatch):
    source = fixture_pdfs / "meta_full.pdf"
    output = tmp_path / "out.pdf"

    monkeypatch.setattr(
        linearize, "check", lambda path: (False, ["人为制造的线性化失败"])
    )

    result = run_pipeline(source, output)

    item = result.result_for("fast_web_view")
    assert item["status"] == "failed"
    assert item["counts"]["failed"] == 1
    assert item["counts"]["modified"] == 0
    assert item["next_action"]
    assert any("人为制造" in warning for warning in item["warnings"])

    # 其他八项结果全部保留，且没有跟着变失败
    others = [r for r in result.results if r["key"] != "fast_web_view"]
    assert len(others) == 8
    assert all(r["status"] != "failed" for r in others)
    assert result.result_for("metadata")["status"] == "adjusted"

    # 输出仍然可打开，元数据清空的成果还在
    report = verify_pdf_file(output, expected_pages=1, require_no_metadata=True)
    assert report.ok, report.problems
    with pikepdf.open(output) as pdf:
        assert pdf.is_linearized is False
        assert pdf.trailer.get("/Info") is None

    assert result.overall_status == "completed_with_warnings"
    assert "fast_web_view" in result.failed_keys


def test_verification_failure_rolls_back_single_operation(fixture_pdfs, tmp_path, monkeypatch):
    """单项结果验证不通过时，该项作废、其余照常。"""
    from app.pdf import pipeline as pipeline_module

    original = pipeline_module.verify_pdf_file
    injected: list[str] = []

    def flaky(path, *, expected_pages=None, require_no_metadata=False):
        report = original(
            path, expected_pages=expected_pages, require_no_metadata=require_no_metadata
        )
        # 只对「注释展平产出的候选文件」注入失败。
        # 流水线第一件事是验证**源文件**，若按调用次数注入会打偏目标。
        if Path(path).name == ".candidate-flatten_annotations.pdf":
            from app.pdf.verify import VerifyReport

            injected.append(str(path))
            return VerifyReport(ok=False, problems=("注入的验证失败",))
        return report

    monkeypatch.setattr(pipeline_module, "verify_pdf_file", flaky)

    # 样本同时有注释与书签，才能同时验证「本项作废」与「其余照常」
    result = run_pipeline(fixture_pdfs / "exempt_mix.pdf", tmp_path / "out.pdf")

    assert injected, "注入点必须命中注释展平的候选文件"

    item = result.result_for("flatten_annotations")
    assert item["status"] == "failed"
    assert item["reason_code"] == "output_verification_failed"
    assert item["counts"]["modified"] == 0
    assert item["counts"]["failed"] == item["counts"]["checked"] == 4
    assert item["next_action"]
    # 其余项仍然成功
    assert result.result_for("bookmarks_inherit_zoom")["status"] == "adjusted"
    assert result.output.is_file()
    # 展平被作废：注释一个都没少
    with pikepdf.open(result.output) as pdf:
        assert sum(len(annots_of(page)) for page in pdf.pages) == 4


# --------------------------------------------------------------------------- #
# 源文件异常
# --------------------------------------------------------------------------- #


def test_missing_source_raises(tmp_path):
    with pytest.raises(PipelineError) as excinfo:
        run_pipeline(tmp_path / "nope.pdf", tmp_path / "out.pdf")
    assert excinfo.value.reason_code == "source_missing"


def test_corrupt_source_raises(fixture_pdfs, tmp_path):
    with pytest.raises(PipelineError) as excinfo:
        run_pipeline(fixture_pdfs / "corrupt.pdf", tmp_path / "out.pdf")
    assert excinfo.value.reason_code == "source_unreadable"
    assert excinfo.value.problems


def test_encrypted_source_raises(fixture_pdfs, tmp_path):
    with pytest.raises(PipelineError) as excinfo:
        run_pipeline(fixture_pdfs / "encrypted.pdf", tmp_path / "out.pdf")
    assert excinfo.value.reason_code == "source_unreadable"


def test_output_is_atomic_and_workdir_cleaned(fixture_pdfs, tmp_path):
    output = tmp_path / "sub" / "out.pdf"
    run_pipeline(fixture_pdfs / "zoom_explicit.pdf", output)

    assert output.is_file()
    leftovers = [p.name for p in output.parent.iterdir() if p.name.startswith(".pipeline-")]
    assert leftovers == [], "临时工作目录必须清理干净"


def test_page_count_is_preserved_for_every_sample(fixture_pdfs, tmp_path):
    for name in ("zoom_explicit.pdf", "annots_mixed.pdf", "exempt_mix.pdf", "meta_full.pdf"):
        with pikepdf.open(fixture_pdfs / name) as pdf:
            pages = len(pdf.pages)
        output = tmp_path / f"out-{name}"
        result = run_pipeline(fixture_pdfs / name, output)
        assert result.page_count == pages
        report = verify_pdf_file(output, expected_pages=pages)
        assert report.ok, report.problems
