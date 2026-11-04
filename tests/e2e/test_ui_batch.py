"""浏览器端到端：批量上传 → 批次页 → 全部开始 → 各自出结果 → 下载。

和 `test_ui_flow.py` 一样只通过界面操作完成，不看接口返回、不改任何 JSON。

三条产品承诺必须由浏览器验证，TestClient 验不了：
- 一次选多个文件时**前端不再吞掉后面的文件**（老版本只取第一个）；
- 批次页会**自己轮询**到全部结束，不需要手动刷新；
- 一个坏文件**不阻塞**同批其他文件。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

GOOD = "text_blocks.pdf"
UNTAGGED = "untagged.pdf"
BROKEN = "corrupt.pdf"

# 终态标签。`text_blocks.pdf` 实测会落到「完成但有警告」（有需要人工复核的处理项），
# 所以判定"跑完了"必须认这两种，只认「已完成」会把正常结果当失败。
TERMINAL_LABELS = ("已完成", "完成但有警告", "失败", "已中断")



# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def wait_hash(page, prefix: str, timeout: int = 30000) -> None:
    page.wait_for_function(
        "prefix => location.hash.startsWith(prefix)", arg=prefix, timeout=timeout
    )


def batch_id_from_hash(page) -> str:
    return page.evaluate("location.hash.split('/')[2]")


def pick(page, live_server, fixture_pdfs, *names: str) -> None:
    """打开首页并选中若干样本文件。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")
    page.set_input_files(
        "#file-input", [str(fixture_pdfs / name) for name in names]
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def test_multi_select_opens_batch_page_with_every_file(page, live_server, fixture_pdfs):
    """选两个文件必须两个都进批次，不能"只处理第一个"。"""
    pick(page, live_server, fixture_pdfs, GOOD, UNTAGGED)

    wait_hash(page, "#/batch/")
    page.wait_for_selector("#batch-table tbody tr")

    rows = page.query_selector_all("#batch-table tbody tr")
    assert len(rows) == 2, "多选之后批次页只出现了一个文件"

    table_text = page.inner_text("#batch-table")
    assert GOOD in table_text
    assert UNTAGGED in table_text
    assert page.inner_text("#batch-count").strip() == "2"


def test_batch_runs_every_job_to_completion_and_offers_downloads(
    page, live_server, fixture_pdfs
):
    """点一次「全部开始」，批次页自己轮询到全部完成，并且每行都能下载。"""
    pick(page, live_server, fixture_pdfs, GOOD, UNTAGGED)

    wait_hash(page, "#/batch/")
    page.wait_for_selector("#start-batch")
    page.click("#start-batch")

    # 不刷新、不轮询外部接口：完全靠批次页自己的定时器
    page.wait_for_function(
        """([labels, expected]) => {
            const rows = document.querySelectorAll('#batch-table tbody tr');
            if (rows.length !== expected) return false;
            return Array.from(rows).every((row) =>
                labels.some((label) => row.textContent.includes(label))
            );
        }""",
        arg=[list(TERMINAL_LABELS), 2],
        timeout=120000,
    )

    assert "全部结束" in page.inner_text("#batch-note")

    for row in page.query_selector_all("#batch-table tbody tr"):
        link = row.query_selector("[data-action='download']")
        assert link is not None, f"这一行没有下载入口：{row.inner_text()}"
        assert link.get_attribute("href").startswith("/api/jobs/")
        assert link.get_attribute("aria-disabled") is None


def test_one_broken_file_does_not_block_the_others(page, live_server, fixture_pdfs):
    """坏文件只影响自己那一行，好文件照常走完。"""
    pick(page, live_server, fixture_pdfs, GOOD, BROKEN)

    wait_hash(page, "#/batch/")
    page.wait_for_selector("#batch-table tbody tr")

    # 坏文件在服务端就被拒了，所以批次里只剩一个好文件
    rows = page.query_selector_all("#batch-table tbody tr")
    assert len(rows) == 1, "坏文件不应该建任务，好文件应该照常入批"
    assert GOOD in rows[0].inner_text()

    # 坏文件被拒必须让用户知道，否则"我选了 2 个怎么只有 1 个"没法解释。
    # toast 在视图容器之外，跳转后仍然存在，所以读 textContent 而不是 inner_text
    # （后者在元素隐藏时返回空串，会随 2.6 秒的淡出时机变成 flaky）。
    page.wait_for_function(
        "() => document.querySelector('#toast').textContent.includes('未通过检查')",
        timeout=10000,
    )

    page.click("#start-batch")
    page.wait_for_function(
        """([labels, expected]) => {
            const rows = document.querySelectorAll('#batch-table tbody tr');
            if (rows.length !== expected) return false;
            return Array.from(rows).every((row) =>
                labels.some((label) => row.textContent.includes(label))
            );
        }""",
        arg=[list(TERMINAL_LABELS), 1],
        timeout=120000,
    )


def test_batch_page_survives_refresh(page, live_server, fixture_pdfs):
    """刷新批次页不该丢进度——表格内容来自后端而不是内存。"""
    pick(page, live_server, fixture_pdfs, GOOD, UNTAGGED)

    wait_hash(page, "#/batch/")
    page.wait_for_selector("#batch-table tbody tr")
    batch_id = batch_id_from_hash(page)
    before = page.inner_text("#batch-table")

    page.reload()
    page.wait_for_selector("#batch-table tbody tr")

    assert batch_id_from_hash(page) == batch_id
    assert page.inner_text("#batch-table") == before


# --------------------------------------------------------------------------- #
# 上限拦截
# --------------------------------------------------------------------------- #


def test_over_limit_selection_is_refused_before_upload(page, live_server):
    """超过上限时前端直接拦下：不发请求、不建任务，并说明原因。"""
    page.goto(live_server.url_for("/"))
    page.wait_for_selector("#dropzone")

    limit = page.evaluate("() => Number(document.querySelector('#dropzone-hint').textContent.match(/最多 (\\d+)/)[1])")
    assert limit > 0

    # 用内存 payload 造出 limit+1 个文件：这里只验前端拦截，不需要真 PDF 内容
    payloads = [
        {
            "name": f"batch-{index}.pdf",
            "mimeType": "application/pdf",
            "buffer": b"%PDF-1.4\n%%EOF\n",
        }
        for index in range(limit + 1)
    ]
    page.set_input_files("#file-input", payloads)

    page.wait_for_function(
        "() => document.querySelector('#upload-error').textContent.trim().length > 0",
        timeout=10000,
    )
    message = page.inner_text("#upload-error")
    assert f"一次最多 {limit} 个文件" in message
    assert "分批上传" in message

    # 没有跳走，说明请求根本没发出去
    assert page.evaluate("() => location.hash") in ("", "#/home", "#/")
