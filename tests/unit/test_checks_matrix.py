"""M1/M7/M8 验收矩阵：31 个样本 × 12 项校验。

PRD §12 M1 的验收标准是两条：

1. `pytest tests/unit/test_checks_*.py` 全绿；
2. 12 个模块对同一份正常 PDF **全部返回 pass**（无假阳性）。

第 2 条是硬门槛。假阳性一旦混进去，用户第一次用就会看到满屏警告，
然后学会无视所有警告——那这套校验就等于没做。

第 1 条之外，这里把每个样本的**完整状态矩阵**也钉住：以后任何一项判定
发生漂移（比如阈值被改、文案被改），矩阵测试会立刻指出来，而不是等到
用户发现报告不对。每个样本都至少有一项非 pass，否则它就没在测东西。

v4.1 变更：第 8 项由「目录页码」拆成 `toc`（目录）与 `page_labels`（页码），
所以元组从 9 项变成 10 项。绝大多数老样本首页没有内链，`toc` 一律 fail——
这是**如实反映**，不是回归：它们在造出来的时候就不带目录。
只有 `check_clean` 和 `check_toc_ok` 首页有蓝色目录内链，`toc` 才是 pass。

v4.2 变更：元组从 10 项变成 12 项，末两列是 `initial_view`（R38）与
`metadata_security`（R39）。这两项一加，绝大多数老样本的 `initial_view`
都是 fail——它们造出来时根本没设 `/PageMode`，缺失等价「仅页面」，不是
「书签面板和页面」。同样是**如实反映**。合规骨架 `_toc_document` 已补上
`/PageMode /UseOutlines`，所以 `check_clean` / `check_toc_ok` 仍是全 pass。
另加 4 个单缺陷样本：`/FitH` 书签、初始视图三项全错、带 DocInfo+XMP、
仅权限密码，让新两项在矩阵里各自有正反两面。

v4.3 变更（§18，校验口径纠偏）：
- `bookmarks`（第 10 列）不再是「≤5 页一律 skipped」，而是**先看有没有可用书签**。
  有书签就查承前缩放（合规 pass、不合规 warning），无书签才看页数
  （≤5 页 skipped，>5 页 warning）。所以 `check_links_*` / `check_rotated_text` /
  `check_landscape` 这几行从 skipped 变成 pass，`check_no_outline` /
  `check_empty_outline` 从 fail 变成 warning。
- `font_embedding`（第 7 列）取消标准 14 豁免，并校验字体程序本身有效。
  `check_font_missing` 从 warning 变 fail；手写样本 `check_links_broken_annots`
  里的裸 `/Helvetica` 也从 pass 变 fail（这是**真阳性**，不是回归）。
  新增 `check_font_standard_unembedded`（只有裸 Helvetica）与
  `check_font_placeholder`（26 字节假 `/FontFile2`）两个负样本。
"""

from __future__ import annotations

import pytest

from app.pdf.checks import VERIFY_KEYS, run_checks

#: 状态矩阵。键是样本文件名，值按 `VERIFY_KEYS` 顺序给出十二项状态：
#: 命名 / 体积 / 外链 / 方向 / 文本层 / 批注 / 字体 / 目录 / 页码 / 书签与缩放 /
#: 初始视图 / 元数据与安全
MATRIX: dict[str, tuple[str, ...]] = {
    # 合规基线：十二项全 pass
    "check_clean.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "pass", "pass", "pass",
    ),
    # R22 书签：无 /Outlines / 空书签树 / 页数 ≤5
    # v4.3（§18.1）：无可用书签只在 ≤5 页时跳过，>5 页一律 warning（不再判 fail）
    "check_no_outline.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "warning", "fail", "pass",
    ),
    "check_empty_outline.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "warning", "fail", "pass",
    ),
    "check_short.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "skipped", "fail", "pass",
    ),
    # R16 外部链接：外链告警 / 内链合规 / 结构损坏
    # v4.3（§18.1）：≤5 页但**有**书签时照样查承前缩放，合规就是 pass 而非 skipped
    "check_links_http.pdf": (
        "pass", "pass", "warning", "pass", "pass", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_links_internal.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_links_broken_annots.pdf": (
        # 手写样本：没有 /PageLabels 也没有页脚页码 → R33 只能告警（正确：无从核对）
        # v4.3（§18.2）：手写的 /Helvetica 没有 /FontDescriptor，标准 14 不再豁免 → fail
        "pass", "pass", "warning", "pass", "pass", "warning",
        "fail", "fail", "warning", "skipped", "fail", "pass",
    ),
    # R17 方向
    "check_rotated_text.pdf": (
        "pass", "pass", "pass", "warning", "pass", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_landscape.pdf": (
        "pass", "pass", "pass", "skipped", "pass", "pass",
        "pass", "skipped", "pass", "pass", "fail", "pass",
    ),
    # R18 文本层
    "check_mixed_text.pdf": (
        "pass", "pass", "pass", "pass", "warning", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    # R19 批注
    "check_highlights.pdf": (
        "pass", "pass", "pass", "pass", "pass", "warning",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    # R20 字体
    # v4.3（§18.2）：未嵌入 / 嵌入程序无效一律 fail，标准 14 不再豁免
    "check_font_missing.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "fail", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_font_standard_unembedded.pdf": (
        # 只有 Helvetica 且没有 /FontDescriptor——用户真实文件的典型形态
        "pass", "pass", "pass", "pass", "pass", "pass",
        "fail", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_font_placeholder.pdf": (
        # 有 /FontFile2 键，但流只有 26 字节：看着像嵌入了，其实不是
        "pass", "pass", "pass", "pass", "pass", "pass",
        "fail", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_font_broken.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "fail", "fail", "pass", "pass", "fail", "pass",
    ),
    # R33 页码（`/PageLabels` 路）
    "check_labels_none.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "warning", "pass", "fail", "pass",
    ),
    "check_labels_from3.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "fail", "pass", "fail", "pass",
    ),
    # R33 页码（页脚兜底路）：连续合规 / 整体偏移 / 漏印一页
    "check_footer_ok.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_footer_from3.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "fail", "pass", "fail", "pass",
    ),
    "check_footer_gap.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "warning", "pass", "fail", "pass",
    ),
    # R32 目录：合规 / 部分非蓝 / 只有标题 / 没有 / 单页 / 目的地损坏
    "check_toc_ok.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "pass", "pass", "pass",
    ),
    "check_toc_partial_blue.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "warning", "pass", "pass", "pass", "pass",
    ),
    "check_toc_title_only.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "warning", "pass", "pass", "pass", "pass",
    ),
    "check_toc_none.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "pass", "fail", "pass",
    ),
    "check_toc_single_page.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "skipped", "pass", "skipped", "fail", "pass",
    ),
    "check_toc_broken_dest.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "fail", "pass", "pass", "pass", "pass",
    ),
    # R37 承前缩放：书签齐全但全部 `/FitH`（有顶边、无缩放位，不算承前缩放）
    "check_bookmark_fith.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "warning", "pass", "pass",
    ),
    # R38 初始视图：三项全错（仅页面 / 单页连续 / 150%）
    "check_view_bad.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "pass", "fail", "pass",
    ),
    # R39 元数据与安全：DocInfo 三字段 + 文档级 XMP / 仅权限密码
    "check_meta_dirty.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "pass", "pass", "warning",
    ),
    "check_meta_encrypted.pdf": (
        "pass", "pass", "pass", "pass", "pass", "pass",
        "pass", "pass", "pass", "pass", "pass", "fail",
    ),
}


def _run(check_ctx, name: str):
    return run_checks(VERIFY_KEYS, check_ctx(name))


def test_matrix_covers_every_check_fixture(fixture_pdfs):
    """样本目录里每个 `check_*.pdf` 都必须在矩阵里，漏一个就是漏测。"""
    on_disk = {path.name for path in fixture_pdfs.glob("check_*.pdf")}
    assert on_disk == set(MATRIX), on_disk.symmetric_difference(set(MATRIX))


def test_matrix_rows_match_the_contract_length():
    """每行必须是 12 项。少一项意味着某个校验项在这张表里根本没有列。"""
    for name, row in MATRIX.items():
        assert len(row) == len(VERIFY_KEYS), (name, len(row))
    assert len(VERIFY_KEYS) == 12


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_status_matrix(name, check_ctx):
    results = _run(check_ctx, name)
    actual = tuple(item.status for item in results)
    assert actual == MATRIX[name], dict(zip(VERIFY_KEYS, zip(MATRIX[name], actual)))


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_results_follow_registry_order(name, check_ctx):
    results = _run(check_ctx, name)
    assert [item.key for item in results] == list(VERIFY_KEYS)


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_every_sample_actually_tests_something(name):
    """除合规基线外，每个样本至少有一项不是 pass，否则它没在测东西。"""
    if name in ("check_clean.pdf", "check_toc_ok.pdf"):
        pytest.skip("合规基线本身就是全 pass")
    assert any(status != "pass" for status in MATRIX[name])


@pytest.mark.parametrize("name", ["check_clean.pdf", "check_toc_ok.pdf"])
def test_clean_fixture_has_no_false_positives(name, check_ctx):
    """M1 硬验收：12 个模块对同一份正常 PDF 全部返回 pass。"""
    results = _run(check_ctx, name)
    offenders = [(item.key, item.status, item.summary) for item in results if item.status != "pass"]
    assert offenders == []


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_no_internal_error_leaks_into_the_report(name, check_ctx):
    """任何样本都不该让某一项以「检查异常」收场——那是未处理的结构问题。"""
    results = _run(check_ctx, name)
    for item in results:
        assert "检查异常" not in item.summary, (item.key, item.summary)
        assert "Traceback" not in item.summary


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_library_noise_is_not_exposed_to_users(name, check_ctx):
    """pymupdf 的 `MuPDF error:` 会打到 stderr，绝不能出现在给用户看的文案里。"""
    results = _run(check_ctx, name)
    for item in results:
        blob = item.summary + " ".join(f.message + (f.detail or "") for f in item.findings)
        assert "MuPDF" not in blob
        assert "qpdf" not in blob.lower()
