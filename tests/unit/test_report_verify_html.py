"""M5-1/M5-5 验收：校验报告 HTML 渲染（R23）。

这里不点按钮、不发请求，只对着渲染函数断言「导出的文件必须长成什么样」。
四条硬要求各自对应一组用例：

1. **全部转义**（AC-R23-E1）：文件名可以是 `<script>alert(1)</script>`，
   渲染结果里不能出现可执行的脚本标签；
2. **可离线**（AC-R23-N4）：单文件、内联 CSS、无任何外链；
3. **结构完整**（R23 报告内容）：抬头、汇总、逐文件区块、十二项明细、页脚；
4. **不做结论**（AC-R23-E2）：除免责声明那句原文外，不出现结论性表述。
"""

from __future__ import annotations

import re

import pytest

from app.config import APP_NAME, APP_VERSION
from app.report.base import REPORT_DISCLAIMER
from app.report.verify_html import render_batch_report, render_file_report
from app.schemas import (
    VERIFY_KEYS,
    VERIFY_STATUS_LABELS,
    VERIFY_TITLES,
    summarize_verify_items,
)

GENERATED_AT = "2026-03-01T08:00:00.000Z"
BATCH_ID = "bat-0000000000000001"


# --------------------------------------------------------------------------- #
# 夹具构造
# --------------------------------------------------------------------------- #


def make_item(
    key: str,
    status: str = "pass",
    summary: str = "符合要求",
    findings: list[dict] | None = None,
    duration_ms: int = 12,
) -> dict:
    return {
        "key": key,
        "status": status,
        "summary": summary,
        "findings": findings or [],
        "duration_ms": duration_ms,
    }


def make_result(
    *,
    name: str = "示例文档.pdf",
    job_id: str = "job-0000000000000001",
    phase: str = "precheck",
    statuses: dict[str, str] | None = None,
    page_count: int = 3,
    size_bytes: int = 2048,
) -> dict:
    picked = statuses or {}
    items = [make_item(key, picked.get(key, "pass")) for key in VERIFY_KEYS]
    return {
        "job_id": job_id,
        "phase": phase,
        "generated_at": GENERATED_AT,
        "file": {"name": name, "size_bytes": size_bytes, "page_count": page_count},
        "items": items,
        "summary": summarize_verify_items(items),
    }


def make_batch(*results: dict, phase: str = "precheck") -> dict:
    files = list(results)
    summary: dict[str, int] = {"files": len(files)}
    for result in files:
        for status, count in result["summary"].items():
            summary[status] = summary.get(status, 0) + count
    return {
        "batch_id": BATCH_ID,
        "phase": phase,
        "generated_at": GENERATED_AT,
        "files": files,
        "summary": summary,
    }


def body_without_disclaimer(document: str) -> str:
    """去掉免责声明那句原文，用来验证「其余内容不得下结论」。"""
    return document.replace(REPORT_DISCLAIMER, "")


# --------------------------------------------------------------------------- #
# 1. 全部转义：AC-R23-E1
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "<script>alert(1)</script>",
        "报告 <b>粗体</b>.pdf",
        'quote"and&and.pdf',
        "a<b>c&d\"e'f.pdf",
    ],
)
def test_filename_is_escaped_everywhere(raw: str) -> None:
    result = make_result(name=raw)
    document = render_file_report(result)

    assert "<script" not in document.lower(), "文件名里的脚本标签没有被转义"
    assert raw not in document, "原始字符串原样出现在 HTML 里"
    # 转义之后仍然要能看出这是哪个文件（不是被整段吃掉）
    assert "&lt;" in document or "&amp;" in document or "&quot;" in document


def test_script_tag_in_findings_is_escaped() -> None:
    """明细里的 message / detail 同样来自 PDF 内部（字体名、URI），必须转义。"""
    result = make_result(
        statuses={"font_embedding": "fail"},
    )
    result["items"][6]["findings"] = [
        {
            "level": "fail",
            "message": "<img src=x onerror=alert(1)>",
            "detail": "URI javascript:alert('x')",
        }
    ]
    document = render_file_report(result)
    assert "<img" not in document.lower()
    assert "&lt;img" in document


# --------------------------------------------------------------------------- #
# 2. 可离线：AC-R23-N4
# --------------------------------------------------------------------------- #


def test_document_has_no_external_reference() -> None:
    document = render_batch_report(make_batch(make_result()))

    assert document.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in document
    assert "<style>" in document and "</style>" in document, "样式必须内联"

    lowered = document.lower()
    for forbidden in ("<link", "<script", "src=", "@import", "http://", "https://", "url("):
        assert forbidden not in lowered, f"导出文件含外部依赖：{forbidden}"


def test_document_is_self_contained_for_a_whole_batch() -> None:
    batch = make_batch(
        make_result(name="甲.pdf", job_id="job-0000000000000001"),
        make_result(name="乙.pdf", job_id="job-0000000000000002"),
    )
    document = render_batch_report(batch)
    # 两个文件共用一个 <style>，不是每块各自带一份
    assert document.count("<style>") == 1
    assert document.count("<!DOCTYPE html>") == 1


# --------------------------------------------------------------------------- #
# 3. 结构完整：R23「报告内容」
# --------------------------------------------------------------------------- #


def test_header_carries_app_version_batch_and_file_count() -> None:
    batch = make_batch(make_result(name="甲.pdf"), make_result(name="乙.pdf"))
    document = render_batch_report(batch)

    assert APP_NAME in document
    assert APP_VERSION in document
    assert BATCH_ID in document
    assert GENERATED_AT in document
    assert "文件数" in document
    # 文件数写的是真实数量，不是「有/无」
    assert re.search(r"<dt>文件数</dt><dd>2</dd>", document)


def test_summary_counts_files_times_items() -> None:
    """汇总按「文件 × 项」计数：两个文件各有 1 项 fail → 不通过 2，不是 1。"""
    batch = make_batch(
        make_result(name="甲.pdf", statuses={"bookmarks": "fail"}),
        make_result(name="乙.pdf", statuses={"bookmarks": "fail"}),
    )
    document = render_batch_report(batch)
    totals = {
        status: batch["files"][0]["summary"].get(status, 0)
        + batch["files"][1]["summary"].get(status, 0)
        for status in ("pass", "warning", "fail", "skipped")
    }
    assert totals["fail"] == 2

    assert '<div class="tile"><b>2</b><span>不通过（项）</span></div>' in document
    for label in ("通过", "预警", "不通过", "跳过"):
        assert f"<span>{label}（项）</span>" in document


def test_each_file_block_is_collapsed_by_default() -> None:
    """AC-R23-N2：每文件区块默认折叠，展开后是十二项明细。"""
    document = render_batch_report(make_batch(make_result()))

    assert document.count('<details class="file">') == 1
    assert "<details open" not in document, "默认必须是折叠状态"
    assert document.count("</details>") == 1


def test_file_block_lists_every_item_in_declaration_order() -> None:
    document = render_file_report(make_result())
    positions = [document.index(VERIFY_TITLES[key]) for key in VERIFY_KEYS]
    assert positions == sorted(positions), "报告里的行序必须与 VERIFY_KEYS 一致"


def test_file_block_has_one_row_per_verify_item() -> None:
    """行数必须等于 `VERIFY_KEYS` 的长度，一行不多一行不少。

    挡的是「新增了键但报告没跟上」：`_item_row` 是按 `items` 列表渲染的，
    只要后端少塞一条（比如拆分 R21 时漏了新键），报告就会静默少一行，
    而上面那条顺序断言照样通过。
    """
    document = render_file_report(make_result())
    # 数行首那个序号单元格，而不是数 `<tr>`：表头也占一个 `<tr>`，直接数会把
    # 表头算进去，将来加了表尾（比如小计行）还会再偏一次。
    assert document.count('<td class="num">') == len(VERIFY_KEYS)


def test_toc_and_page_labels_are_two_separate_rows() -> None:
    """「目录」和「页码」必须是两行，各自给结论（R32 / R33）。

    v4.0 时它们挤在一行「目录页码校验」里，一个 `warning` 同时代表两件事，
    用户没法判断是目录缺了还是页码没从 1 开始。这条钉的是拆分结果：
    两行、两个标题、各自的状态标签互不影响。
    """
    document = render_file_report(
        make_result(statuses={"toc": "warning", "page_labels": "fail"})
    )

    toc_row = next(
        row for row in document.split("<tr>") if VERIFY_TITLES["toc"] in row
    )
    page_row = next(
        row for row in document.split("<tr>") if VERIFY_TITLES["page_labels"] in row
    )

    assert VERIFY_TITLES["toc"] != VERIFY_TITLES["page_labels"], "两个标题不能重名"
    assert 'class="tag warning"' in toc_row
    assert 'class="tag fail"' in page_row


def test_file_block_reports_size_pages_and_status_tags() -> None:
    document = render_file_report(
        make_result(
            statuses={"bookmarks": "fail", "font_embedding": "warning"},
            page_count=38,
            size_bytes=1024 * 1024,
        )
    )

    assert "页数 38" in document
    assert "1.0 MB" in document
    assert f'<span class="tag fail">{VERIFY_STATUS_LABELS["fail"]}</span>' in document
    assert f'<span class="tag warning">{VERIFY_STATUS_LABELS["warning"]}</span>' in document
    assert f'<span class="tag pass">{VERIFY_STATUS_LABELS["pass"]}</span>' in document


def test_skipped_item_is_written_as_a_word_not_a_dash() -> None:
    """`skipped` 的界面标签是破折号，但在报告表格里破折号等于空单元格。"""
    result = make_result(statuses={"filename_naming": "skipped"})
    result["items"][0]["summary"] = "工具生成名，不适用命名规范校验"
    document = render_file_report(result)

    assert '<span class="tag skipped">跳过</span>' in document
    assert "工具生成名，不适用命名规范校验" in document
    assert VERIFY_STATUS_LABELS["skipped"] == "—"


def test_findings_are_rendered_with_page_number() -> None:
    result = make_result(statuses={"external_links": "fail"})
    result["items"][2]["findings"] = [
        {"level": "fail", "message": "存在外部链接", "page": 7, "detail": "https://example.com"}
    ]
    document = render_file_report(result)
    assert "第 7 页" in document
    assert "存在外部链接" in document
    assert "https://example.com" in document


def test_empty_item_list_says_so_instead_of_rendering_a_blank_table() -> None:
    result = make_result()
    result["items"] = []
    result["summary"] = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0}
    document = render_file_report(result)
    assert "本次未执行任何校验项" in document
    assert "<table>" not in document


def test_file_report_shows_job_id_but_not_a_batch_id() -> None:
    document = render_file_report(make_result(job_id="job-0000000000000009"))
    assert "job-0000000000000009" in document
    assert "批次 ID" not in document, "单文件报告没有批次号，不该编一个出来"


def test_post_phase_is_labelled_as_post_verify() -> None:
    document = render_batch_report(make_batch(make_result(phase="post"), phase="post"))
    assert "后置校验" in document
    assert "前置校验" not in document


# --------------------------------------------------------------------------- #
# 4. 免责声明与结论性表述：AC-R23-E2
# --------------------------------------------------------------------------- #


def test_footer_carries_the_exact_disclaimer() -> None:
    document = render_batch_report(make_batch(make_result()))
    assert REPORT_DISCLAIMER == "本报告仅反映工具可自动检测的项目，不代表文件完全合规。"
    assert document.count(REPORT_DISCLAIMER) == 1


@pytest.mark.parametrize("word", ["完全合规", "已通过", "100% 合规", "符合法规"])
def test_report_makes_no_conclusion_outside_the_disclaimer(word: str) -> None:
    """免责声明原文里本来就有「完全合规」（在否定句里），所以先把它摘掉再查。"""
    document = render_batch_report(make_batch(make_result()))
    assert word not in body_without_disclaimer(document)


def test_all_pass_does_not_become_a_compliance_claim() -> None:
    """十二项全 PASS 时最容易写出「本文件已通过校验」——这正是要禁的。"""
    document = render_file_report(make_result())
    assert "已通过" not in body_without_disclaimer(document)
    assert "全部通过" not in body_without_disclaimer(document)
    # 事实性描述仍然要有：十二项各自的 PASS 标签都在
    assert document.count('<span class="tag pass">PASS</span>') == len(VERIFY_KEYS)


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #


def test_empty_batch_renders_an_empty_state() -> None:
    document = render_batch_report(
        {"batch_id": BATCH_ID, "phase": "precheck", "generated_at": GENERATED_AT, "files": [], "summary": {"files": 0}}
    )
    assert "本次没有可展示的校验结果" in document
    assert REPORT_DISCLAIMER in document


def test_render_is_pure() -> None:
    """同一个入参渲染两次必须完全一致：报告不掺时间戳、不掺随机数。"""
    batch = make_batch(make_result())
    assert render_batch_report(batch) == render_batch_report(batch)
