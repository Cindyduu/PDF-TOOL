"""R38 初始视图校验（v4.2，只读）。

三项规定值：导览标签 = 书签面板和页面、页面布局 = 默认、放大率 = 默认。

**三项都是「缺失即合规」**，这条最容易被写反：`/PageLayout` 缺失时 Acrobat
显示「默认」，多写一个 `/SinglePage` 反而是「单页」。`/PageMode` 则是例外——
缺失按规范等价 `/UseNone` = 「仅页面」，而规定值是「书签面板和页面」，
所以缺 `/PageMode` 必须判不合规。

**判 fail 不判 warning**（D-15）：这三项是 eCTD 的规定值，处理项 ③ 一定改得了；
判 warning 会让「根本没设置」被「预警」这个词稀释掉。

最后一个用例是**同口径回归钉子**：处理侧 `initial_layout` / `initial_zoom` /
`bookmark_panel` 跑完必须让这一项转 PASS。两边口径一分叉，这里就红。
"""

from __future__ import annotations

from pathlib import Path

from pikepdf import Array, Dictionary, Name, String

from app.pdf import view_labels
from app.pdf.checks import initial_view
from tests.fixtures.make_fixtures import _add_pages, _page_labels, new_pdf, save

PAGES = 8


def _view_pdf(
    tmp_path: Path,
    name: str,
    *,
    mode: str | None = None,
    layout: object = None,
    action: object = None,
) -> Path:
    """8 页文档 + 指定初始视图键；`None` 表示不写这个键。"""
    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    if mode is not None:
        pdf.Root.PageMode = Name(mode)
    if layout is not None:
        pdf.Root.PageLayout = Name(layout) if isinstance(layout, str) else layout
    if action is not None:
        pdf.Root.OpenAction = action(pages) if callable(action) else action
    return save(pdf, tmp_path / name)


def _by_name(result) -> dict[str, object]:
    """明细固定三条，按前缀取回，便于逐项断言。"""
    table = {}
    for item in result.findings:
        for prefix in ("导览标签", "页面布局", "放大率"):
            if item.message.startswith(prefix):
                table[prefix] = item
    return table


def test_compliant_document_passes(tmp_path, ctx_factory):
    """AC-R38-N1：`/UseOutlines` + 无 `/PageLayout` + 无 `/OpenAction` → PASS。"""
    path = _view_pdf(tmp_path, "ok.pdf", mode="/UseOutlines")
    result = initial_view.run(ctx_factory(path))

    assert result.key == "initial_view"
    assert result.status == "pass"
    assert result.summary == "三项均符合规定值"
    # 合规也要写三条明细：用户要能一眼看到三项各自的实际值
    assert len(result.findings) == 3
    assert all(item.level == "pass" for item in result.findings)

    table = _by_name(result)
    assert table["导览标签"].message == f"导览标签：{view_labels.TARGET_MODE_LABEL}"
    assert table["页面布局"].message == f"页面布局：{view_labels.TARGET_LAYOUT_LABEL}"
    assert table["放大率"].message == f"放大率：{view_labels.TARGET_ZOOM_LABEL}"


def test_explicit_page_layout_fails(tmp_path, ctx_factory):
    """AC-R38-N2：`/PageLayout /OneColumn` → FAIL，明细给出「单页连续 → 默认」。"""
    path = _view_pdf(tmp_path, "layout.pdf", mode="/UseOutlines", layout="/OneColumn")
    result = initial_view.run(ctx_factory(path))

    assert result.status == "fail"
    assert "页面布局" in result.summary
    finding = _by_name(result)["页面布局"]
    assert finding.level == "fail"
    assert finding.message == "页面布局：单页连续"
    assert finding.detail == f"应为「{view_labels.TARGET_LAYOUT_LABEL}」"


def test_missing_page_mode_fails(tmp_path, ctx_factory):
    """AC-R38-N3：`/PageMode` 缺失 → FAIL。

    缺失等价 `/UseNone` = 「仅页面」，不是「默认」——导览标签的下拉框里
    根本没有「默认」这一项。这一条最容易写反。
    """
    path = _view_pdf(tmp_path, "no_mode.pdf")
    result = initial_view.run(ctx_factory(path))

    assert result.status == "fail"
    assert "导览标签" in result.summary
    finding = _by_name(result)["导览标签"]
    assert finding.message == "导览标签：仅页面"
    assert view_labels.TARGET_MODE_LABEL in finding.detail
    assert view_labels.DEFAULT_MODE_LABEL in finding.detail


def test_explicit_zoom_fails(tmp_path, ctx_factory):
    """AC-R38-N4：`/OpenAction` = `[页 /XYZ 0 0 1.5]` → FAIL，明细写「150% → 默认」。"""
    path = _view_pdf(
        tmp_path,
        "zoom.pdf",
        mode="/UseOutlines",
        action=lambda pages: Array([pages[0], Name("/XYZ"), 0, 800, 1.5]),
    )
    result = initial_view.run(ctx_factory(path))

    assert result.status == "fail"
    assert "放大率" in result.summary
    finding = _by_name(result)["放大率"]
    assert finding.message == "放大率：150%"
    assert finding.detail == f"应为「{view_labels.TARGET_ZOOM_LABEL}」"


def test_non_view_open_action_is_compliant(tmp_path, ctx_factory):
    """AC-R38-N5：`/OpenAction` 是 `/S /JavaScript` → 放大率记符合，整体 PASS。

    非视图动作不改变打开时的放大率，Acrobat 仍按「默认」显示。处理侧对这种
    情况也是保留原样、记 `not_applicable`——两处必须同结论。
    """
    path = _view_pdf(
        tmp_path,
        "js.pdf",
        mode="/UseOutlines",
        action=Dictionary(S=Name("/JavaScript"), JS=String("app.alert(1)")),
    )
    result = initial_view.run(ctx_factory(path))

    assert result.status == "pass"
    finding = _by_name(result)["放大率"]
    assert finding.level == "pass"
    assert finding.message == f"放大率：{view_labels.TARGET_ZOOM_LABEL}"


def test_processing_the_three_items_turns_it_green(tmp_path, ctx_factory):
    """AC-R38-N6：处理侧三项跑完 → PASS（处理与校验同口径的回归钉子）。"""
    from app.pdf.context import OpContext
    from app.pdf.initial_view import (
        process_bookmark_panel,
        process_initial_layout,
        process_initial_zoom,
    )

    pdf = new_pdf()
    pages = _add_pages(pdf, PAGES)
    _page_labels(pdf, 1)
    pdf.Root.PageMode = Name("/UseNone")
    pdf.Root.PageLayout = Name("/TwoPageLeft")
    pdf.Root.OpenAction = Array([pages[0], Name("/XYZ"), 0, 800, 2.0])

    ctx = OpContext.build(pdf)
    for processor in (process_initial_layout, process_initial_zoom, process_bookmark_panel):
        processor(pdf, ctx)

    path = save(pdf, tmp_path / "processed.pdf")
    result = initial_view.run(ctx_factory(path))
    assert result.status == "pass", result.summary


def test_illegal_page_layout_value_does_not_raise(tmp_path, ctx_factory):
    """AC-R38-E1：`/PageLayout` 是数字 → FAIL，明细写「未知(42)」，不抛异常。"""
    path = _view_pdf(
        tmp_path,
        "bad_layout.pdf",
        mode="/UseOutlines",
        layout=42,
    )
    result = initial_view.run(ctx_factory(path))

    assert result.status == "fail"
    finding = _by_name(result)["页面布局"]
    assert finding.level == "fail"
    assert "未知(42)" in finding.message
