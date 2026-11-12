"""v4.2 集成闭环：处理三项之后，R37 / R38 / R39 必须转 PASS。

为什么这条闭环只能落在集成层：单元测试分别证明「处理侧改对了」（`app.pdf.bookmarks`
/ `initial_view` / `metadata`）和「校验侧判对了」（`app.pdf.checks.*`），但**两边
口径是否一致**只有在同一条流水线上、对同一个文件先处理再校验，才能证明。

PRD §17.6 风险 14 说的就是这件事：处理完仍报 WARNING，用户就不信这个工具了。
所以这里不 mock 任何一侧——真的跑 `run_pipeline`，再真的对产物跑校验。
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name, String

from app.pdf import bookmarks as bookmarks_module
from app.pdf.checks import (
    PHASE_POST,
    PHASE_PRECHECK,
    CheckContext,
    CheckResult,
    run_check,
)
from app.pdf.pipeline import run_pipeline
from tests.fixtures.make_fixtures import (
    XMP_PACKET,
    _toc_content,
    _toc_document,
    build_outline,
    dest_xyz,
    new_pdf,
    save,
)

PAGES = 8
BOOKMARK_COUNT = 6

#: 处理侧三项（+ 它们依赖的两个视图项）。顺序由 `OPERATION_KEYS` 决定，
#: 这里只负责「勾了哪几项」。
OPS = (
    "bookmarks_inherit_zoom",
    "initial_layout",
    "initial_zoom",
    "bookmark_panel",
    "metadata",
)


def build_source(path: Path) -> Path:
    """三项全坏、其余合规的源文件。

    - 6 条书签全是 `/FitH`（有顶边、无缩放位）→ R37 warning；
    - 导览标签 `/UseNone`、页面布局 `/OneColumn`、放大率 `/XYZ 1.5` → R38 fail；
    - DocInfo 三字段 + 文档级 XMP → R39 warning。

    其余骨架走 `_toc_document`，保证字体、页码、目录这些项不受牵连——
    闭环用例要盯的是这三项，别让别的项把结论搅浑。
    """
    pdf = new_pdf()
    pages = _toc_document(pdf, PAGES, _toc_content(4), 4)
    build_outline(
        pdf,
        [
            {
                "title": f"Chapter {index + 1}",
                "dest": Array([pages[index], Name("/FitH"), 800]),
            }
            for index in range(BOOKMARK_COUNT)
        ],
    )
    # `build_outline` 会把 PageMode 打回 `/UseNone`，这里显式写全三种错法。
    pdf.Root.PageMode = Name("/UseNone")
    pdf.Root.PageLayout = Name("/OneColumn")
    pdf.Root.OpenAction = dest_xyz(pages[1], 1.5)
    pdf.docinfo = pdf.make_indirect(
        Dictionary(
            Title=String("Quarterly Report"),
            Author=String("Alice"),
            Producer=String("fixture-generator"),
        )
    )
    pdf.Root.Metadata = pdf.make_stream(XMP_PACKET)
    return save(pdf, path)


def check_file(key: str, path: Path, phase: str = PHASE_PRECHECK) -> CheckResult:
    """对单个文件跑一项校验。

    这里刻意不走 HTTP 层：本文件要钉的是「处理与校验同口径」，
    掺进上传、任务状态机只会让失败定位变难。端点侧的十二项覆盖在
    `test_verify_flow.py` 里。
    """
    doc = pymupdf.open(str(path))
    try:
        pdf = pikepdf.open(path)
        try:
            ctx = CheckContext(
                path=path,
                filename=path.name,
                size_bytes=path.stat().st_size,
                page_count=doc.page_count,
                doc=doc,
                pdf=pdf,
                phase=phase,
            )
            return run_check(key, ctx)
        finally:
            pdf.close()
    finally:
        doc.close()


def test_source_fails_all_three_new_checks(tmp_path):
    """前置状态：三项各按 PRD 的判定表给出结论，不是「一律 fail」糊过去。"""
    source = build_source(tmp_path / "src.pdf")

    bookmarks = check_file("bookmarks", source)
    assert bookmarks.status == "warning"
    assert "未设为承前缩放" in bookmarks.summary
    assert len(bookmarks.findings) == BOOKMARK_COUNT

    view = check_file("initial_view", source)
    assert view.status == "fail"
    assert "3 项不符合规定值" in view.summary
    assert [item.level for item in view.findings] == ["fail", "fail", "fail"]

    meta = check_file("metadata_security", source)
    assert meta.status == "warning"
    assert "描述字段" in meta.summary
    assert "XMP" in meta.summary


def test_processing_turns_three_checks_to_pass(tmp_path):
    """闭环本体：跑完三项处理，同一份文件的三项校验全部转 PASS。

    这是 AC-R37-N6 / AC-R38-N6 / AC-R39-N6 的集成版——三条「同口径回归钉子」
    在这里被同时钉住：任何一侧改了遍历方式或判定口径，这条用例立刻红。
    """
    source = build_source(tmp_path / "src.pdf")
    output = tmp_path / "out.pdf"

    run_pipeline(source, output, operations=OPS)

    bookmarks = check_file("bookmarks", output, PHASE_POST)
    assert bookmarks.status == "pass", bookmarks.summary
    assert bookmarks.summary == f"{BOOKMARK_COUNT} 个书签，全部为承前缩放"

    view = check_file("initial_view", output, PHASE_POST)
    assert view.status == "pass", view.summary
    assert view.summary == "三项均符合规定值"
    assert [item.level for item in view.findings] == ["pass", "pass", "pass"]

    meta = check_file("metadata_security", output, PHASE_POST)
    assert meta.status == "pass", meta.summary
    assert meta.summary == "文档描述已清空，无加密设置"


def test_bookmark_check_and_processing_agree_on_count(tmp_path):
    """处理侧报「改了 6 条」，校验侧报「6 个书签」——分母必须是同一个。

    两边各自遍历大纲时最容易出现「处理只改到 5 条、校验却按 6 条判」这种
    悄悄错位：结果页说全部改好，校验页说 6 个里还有 1 个不合规。
    """
    source = build_source(tmp_path / "src.pdf")
    output = tmp_path / "out.pdf"

    result = run_pipeline(source, output, operations=OPS)
    item = result.result_for("bookmarks_inherit_zoom")
    assert item["counts"]["checked"] == BOOKMARK_COUNT
    assert item["counts"]["modified"] == BOOKMARK_COUNT
    assert item["counts"]["not_applicable"] == 0

    checked = check_file("bookmarks", output, PHASE_POST)
    assert checked.summary.startswith(f"{item['counts']['checked']} 个书签")

    # 产物里逐条确认：不是「改了个数字」，而是每条都真的变成 [页 /XYZ 左 上 null]。
    # 遍历直接用处理侧那套 `iter_outline_items` / `dest_slot`，与校验侧同源。
    with pikepdf.open(output) as pdf:
        seen = 0
        for node in bookmarks_module.iter_outline_items(pdf):
            slot, key = bookmarks_module.dest_slot(node)
            if slot is None:
                continue
            seen += 1
            assert str(slot[key][1]) == "/XYZ"
            assert slot[key][4] is None
        assert seen == BOOKMARK_COUNT


def test_partial_processing_does_not_fake_the_other_two(tmp_path):
    """只处理书签 → 书签项 PASS，另外两项必须**保持原判**。

    这条是防止「校验侧偷偷自愈」：比如某项顺手读的是处理结果缓存、或者
    某项把「文件被工具处理过」当成合规。真实状态没变，结论就不许变。
    """
    source = build_source(tmp_path / "src.pdf")
    output = tmp_path / "out.pdf"

    run_pipeline(source, output, operations=("bookmarks_inherit_zoom",))

    assert check_file("bookmarks", output, PHASE_POST).status == "pass"
    assert check_file("initial_view", output, PHASE_POST).status == "fail"
    assert check_file("metadata_security", output, PHASE_POST).status == "warning"
