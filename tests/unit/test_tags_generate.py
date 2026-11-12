"""R7 生成带标签的 PDF：单元验收。

这里一律以**落盘后的文件**为准：处理器在内存里把树建好不算数，
重新打开还能对得上账才算数。断言分三层：

1. 结构层——`/StructTreeRoot`、`/StructElem`、`/ParentTree`、`/StructParents` 都在，
   而且 MCID 数 = ParentTree 引用数 = 带 MCID 的结构元素数；
2. 内容层——每个 MCID 都能在内容流里按 `BDC` 指令找到，且从 0 连续；
3. 表述层——结果只能是「待人工复核」，不许出现结论性说法。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest

from app.pdf.context import OpContext
from app.pdf.outcome import decide_status
from app.pdf.tags import content_mapping, detect, generate, verify
from app.schemas import COUNT_KEYS
from tests.pdf_ops import page_content_bytes

BUCKETS = ("modified", "compliant", "exempt", "not_applicable", "failed")
FORBIDDEN = ("完全合规", "已通过认证", "已合规")


def apply_tagging(
    source: Path,
    target: Path,
    *,
    choice: str | None = "generate",
) -> tuple[generate.OpOutcome, Path]:
    """在副本上跑一次标签生成，真的改了才落盘。"""
    shutil.copyfile(source, target)
    with pikepdf.open(target, allow_overwriting_input=True) as pdf:
        ctx = OpContext.build(pdf)
        outcome = generate.process(pdf, ctx, choice=choice)
        if outcome.changed:
            pdf.save(str(target), fix_metadata_version=False)
    return outcome, target


def bdc_depths(page: pikepdf.Page) -> tuple[int, int]:
    """返回 (最大嵌套深度, 结束时残留的未闭合数)。"""
    instructions = content_mapping.page_instructions(page)
    depth = 0
    deepest = 0
    for instruction in instructions:
        operator = str(instruction.operator)
        if operator in ("BDC", "BMC"):
            depth += 1
            deepest = max(deepest, depth)
        elif operator == "EMC":
            depth -= 1
    return deepest, depth


# --------------------------------------------------------------------------- #
# 真的建出了结构
# --------------------------------------------------------------------------- #


def test_generates_real_structure_tree(fixture_pdfs, tmp_path):
    outcome, output = apply_tagging(fixture_pdfs / "text_blocks.pdf", tmp_path / "out.pdf")

    assert outcome.status == "review_required"
    assert outcome.reason_code == "tag_generated"
    assert outcome.counts["modified"] == 1

    with pikepdf.open(output) as pdf:
        tree = pdf.Root.StructTreeRoot
        assert isinstance(tree, pikepdf.Dictionary), "必须真的写入 /StructTreeRoot"
        assert str(tree.Type) == "/StructTreeRoot"
        assert tree.get("/ParentTree") is not None, "必须有 ParentTree"

        document = tree.K[0]
        assert str(document.S) == "/Document"
        elements = list(document.K)
        assert len(elements) == 8, "3 个标题 + 5 个段落 = 8 个内容元素"
        roles = [str(element.S) for element in elements]
        assert roles.count("/P") == 5
        assert sum(1 for role in roles if role.startswith("/H")) == 3

        for element in elements:
            assert isinstance(element.K, int), "每个内容元素必须挂一个 MCID"
            assert element.get("/Pg") is not None, "结构元素必须指向所在页"

        assert pdf.Root.MarkInfo.Marked is True


def test_mcid_parent_tree_and_elements_agree(fixture_pdfs, tmp_path):
    _, output = apply_tagging(fixture_pdfs / "text_blocks.pdf", tmp_path / "out.pdf")

    report = verify.verify_tagged_file(output)
    assert report.ok, report.problems
    assert report.mcids == report.parent_tree_refs == report.content_elements == 8
    assert report.struct_elements == 9, "8 个内容元素 + 1 个 Document 容器"

    with pikepdf.open(output) as pdf:
        per_page = [verify.page_mcids(page) for page in pdf.pages]
        assert [len(mcids) for mcids in per_page] == [5, 3]
        for mcids in per_page:
            assert mcids == list(range(len(mcids))), "MCID 必须从 0 开始连续"
        assert sum(len(mcids) for mcids in per_page) == report.mcids


def test_every_mcid_is_locatable_in_content_stream(fixture_pdfs, tmp_path):
    _, output = apply_tagging(fixture_pdfs / "text_blocks.pdf", tmp_path / "out.pdf")

    with pikepdf.open(output) as pdf:
        for index, page in enumerate(pdf.pages):
            content = page_content_bytes(page)
            assert b"/MCID" in content
            assert page.obj.StructParents is not None
            key = int(page.obj.StructParents)
            nums = pdf.Root.StructTreeRoot.ParentTree.Nums
            keys = [int(nums[i]) for i in range(0, len(nums), 2)]
            assert key in keys, f"第 {index + 1} 页的 StructParents 在 ParentTree 里查不到"


def test_heading_and_paragraph_counts_match_report(fixture_pdfs, tmp_path):
    outcome, output = apply_tagging(fixture_pdfs / "text_blocks.pdf", tmp_path / "out.pdf")
    report = verify.verify_tagged_file(output)

    assert outcome.after["headings"] == report.headings == 3
    assert outcome.after["paragraphs"] == report.paragraphs == 5
    assert outcome.after["mcids"] == report.mcids
    assert outcome.after["pages_tagged"] == 2


def test_rotated_page_text_is_also_tagged(fixture_pdfs, tmp_path):
    """回归：`/Rotate 90` 的页面曾因坐标换算多做一次旋转而整页漏标。"""
    _, output = apply_tagging(fixture_pdfs / "annots_mixed.pdf", tmp_path / "out.pdf")

    with pikepdf.open(output) as pdf:
        rotated = verify.page_mcids(pdf.pages[1])
    assert rotated == [0], "旋转页上的文字同样要被标记"


# --------------------------------------------------------------------------- #
# 只设标记位不算加标签
# --------------------------------------------------------------------------- #


def test_markinfo_only_input_is_not_treated_as_tagged(fixture_pdfs):
    detection = detect.detect_tags(fixture_pdfs / "markinfo_only.pdf")
    assert detection["status"] == "markinfo_only"
    assert detection["marked"] is True
    assert detection["parent_tree_ok"] is False

    report = verify.verify_tagged_file(fixture_pdfs / "markinfo_only.pdf")
    assert report.ok is False, "只有 MarkInfo 的文档不能通过结构校验"
    assert any("StructTreeRoot" in problem for problem in report.problems)


def test_markinfo_only_input_gets_a_real_tree(fixture_pdfs, tmp_path):
    outcome, output = apply_tagging(fixture_pdfs / "markinfo_only.pdf", tmp_path / "out.pdf")

    assert outcome.status == "review_required"
    assert outcome.before["status"] == "markinfo_only"
    assert outcome.after["status"] == "tagged"
    assert verify.verify_tagged_file(output).ok is True


# --------------------------------------------------------------------------- #
# 扫描件：不生成空树
# --------------------------------------------------------------------------- #


def test_scanned_document_fails_without_empty_tree(fixture_pdfs):
    with pikepdf.open(fixture_pdfs / "scanned_like.pdf") as pdf:
        ctx = OpContext.build(pdf)
        outcome = generate.process(pdf, ctx, choice="generate")

        assert outcome.status == "failed"
        assert outcome.reason_code == "tag_no_text"
        assert outcome.changed is False
        assert outcome.counts["failed"] == 1
        assert pdf.Root.get("/StructTreeRoot") is None, "不许留下空标签树"
        assert pdf.Root.get("/MarkInfo") is None, "更不许只留标记位"
        assert any("空标签树" in warning for warning in outcome.warnings)


def test_failed_generation_leaves_file_untouched(fixture_pdfs, tmp_path):
    source = fixture_pdfs / "scanned_like.pdf"
    output = tmp_path / "out.pdf"
    shutil.copyfile(source, output)
    before = output.read_bytes()

    with pikepdf.open(output, allow_overwriting_input=True) as pdf:
        ctx = OpContext.build(pdf)
        outcome = generate.process(pdf, ctx, choice="generate")
        assert outcome.changed is False
        # 处理器说没改，流水线就不会保存——文件保持原样
    assert output.read_bytes() == before


# --------------------------------------------------------------------------- #
# 用户选择
# --------------------------------------------------------------------------- #


def test_skip_choice_does_not_touch_anything(fixture_pdfs):
    with pikepdf.open(fixture_pdfs / "text_blocks.pdf") as pdf:
        ctx = OpContext.build(pdf)
        before = page_content_bytes(pdf.pages[0])
        outcome = generate.process(pdf, ctx, choice="skip")

        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "tag_skipped"
        assert outcome.changed is False
        assert page_content_bytes(pdf.pages[0]) == before


def test_unset_choice_does_not_pretend_to_be_a_user_decision(fixture_pdfs):
    with pikepdf.open(fixture_pdfs / "untagged.pdf") as pdf:
        ctx = OpContext.build(pdf)
        outcome = generate.process(pdf, ctx, choice=None)

        assert outcome.status == "not_applicable"
        assert outcome.reason_code == "tag_choice_missing"
        assert "尚未选择" in outcome.message
        assert outcome.changed is False


def test_keep_choice_preserves_existing_structure(fixture_pdfs):
    with pikepdf.open(fixture_pdfs / "tagged_real.pdf") as pdf:
        ctx = OpContext.build(pdf)
        before = page_content_bytes(pdf.pages[0])
        outcome = generate.process(pdf, ctx, choice="keep")

        assert outcome.status == "unchanged"
        assert outcome.reason_code == "tag_kept"
        assert outcome.changed is False
        assert page_content_bytes(pdf.pages[0]) == before


def test_regenerate_replaces_instead_of_nesting(fixture_pdfs, tmp_path):
    """重新生成必须先清掉旧标记，否则 MCID 数量翻倍、ParentTree 对不上账。"""
    _, output = apply_tagging(
        fixture_pdfs / "tagged_real.pdf", tmp_path / "out.pdf", choice="regenerate"
    )

    with pikepdf.open(output) as pdf:
        mcids = verify.page_mcids(pdf.pages[0])
        assert mcids == [0, 1], "旧标记必须被替换，不是叠加"

        deepest, dangling = bdc_depths(pdf.pages[0])
        assert deepest == 1, "标记块不许嵌套"
        assert dangling == 0, "标记块必须成对闭合"

    report = verify.verify_tagged_file(output)
    assert report.ok, report.problems
    assert report.mcids == 2


# --------------------------------------------------------------------------- #
# 计数与表述
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sample,choice",
    [
        ("text_blocks.pdf", "generate"),
        ("scanned_like.pdf", "generate"),
        ("tagged_real.pdf", "keep"),
        ("untagged.pdf", None),
    ],
)
def test_counts_are_self_consistent(fixture_pdfs, tmp_path, sample, choice):
    outcome, _ = apply_tagging(fixture_pdfs / sample, tmp_path / "out.pdf", choice=choice)

    assert set(outcome.counts) == set(COUNT_KEYS)
    assert outcome.counts["checked"] == sum(outcome.counts[bucket] for bucket in BUCKETS)
    assert decide_status(outcome.counts) == outcome.status or outcome.status == "review_required"


def test_result_never_claims_full_compliance(fixture_pdfs, tmp_path):
    outcome, _ = apply_tagging(fixture_pdfs / "text_blocks.pdf", tmp_path / "out.pdf")

    texts = [outcome.message, outcome.next_action or "", *outcome.warnings]
    blob = " ".join(texts)
    for phrase in FORBIDDEN:
        assert phrase not in blob
    assert "待人工复核" in outcome.message
    assert outcome.status == "review_required", "成功也只能是「待人工复核」"


def test_structure_verifier_catches_broken_parent_tree(fixture_pdfs):
    """对照：ParentTree 引用不一致的样本必须被判不合格，证明校验不是走过场。"""
    report = verify.verify_tagged_file(fixture_pdfs / "broken_struct.pdf")

    assert report.ok is False
    assert report.problems, "必须给出具体问题，不能只说「不通过」"
